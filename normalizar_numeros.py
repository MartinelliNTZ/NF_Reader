# -*- coding: utf-8 -*-
"""
Normalizacion de valores numericos extraidos con OCR de Notas Fiscais
brasilenas (DANFE). Convencion BR: '.' = separador de MILHAR, ',' = DECIMAL.

Objetivo (pedido del usuario):
  - Guardar SOLO el numero (int/float), sin separador de milhar ni texto
    OCR, en los campos numericos del JSON/CSV.
  - Validar la cantidad de decimales:
        * 2 digitos tras el ultimo separador  -> casi seguro DECIMAL
        * 3 digitos tras el ultimo separador  -> casi seguro MILHAR
        * 4 o mas digitos tras el ultimo      -> DECIMAL
          (cantidades con 4 decimales: "37,0000" ton -> 37,0)
  - Si el texto tiene '.' y ',' juntos, el ULTIMO es el decimal y los
    anteriores deben formar grupos de MILHAR de exactamente 3 digitos.
    Si no se cumple -> valor NO fiable -> None (queda en el reporte para
    revision manual).
  - Tambien reporta valores fuera del rango plausible por campo y
    reacomoda campos que el OCR desalineo (quantidade que quedo en
    'unidade'/'valor_unitario', precio unitario que quedo en 'desconto').
"""

import re
from datetime import datetime
from pathlib import Path

# Un "numero" dentro de texto OCR: 123 / 1.234 / 1,5 / 215.380,40 ...
REG_TOKEN_NUM = re.compile(r"\d+(?:[.,]\d+)*")

# Limites de plausabilidad por campo (unidades: R$, kg, ton, %).
# Si el valor parseado queda fuera, se guarda None y se reporta.
RANGOS_CAMPOS = {
    "valor_da_nota":  (0.0, 50_000_000.0),
    "peso_bruto":     (0.0, 10_000_000.0),
    "peso_liquido":   (0.0, 10_000_000.0),
    "quantidade":     (0.0, 1_000_000.0),
    "valor_unitario": (0.0, 10_000_000.0),
    "desconto":       (0.0, 10_000_000.0),
    "valor_total":    (0.0, 50_000_000.0),
    "bc_icms":        (0.0, 50_000_000.0),
    "valor_icms":     (0.0, 10_000_000.0),
    "valor_ipi":      (0.0, 10_000_000.0),
    "aliquota":       (0.0, 100.0),
}

# Campos numericos a nivel de nota y de producto.
CAMPOS_NOTA = ["valor_da_nota", "peso_bruto", "peso_liquido"]
CAMPOS_PRODUCTO = ["quantidade", "valor_unitario", "desconto", "valor_total",
                   "bc_icms", "valor_icms", "valor_ipi", "aliquota"]

STATUS_OK = "ok"
STATUS_LIMPIO = "limpio"
STATUS_RECUPERADO = "recuperado"
STATUS_REUBICADO = "reubicado"
STATUS_NO_PARS = "no_parseable"
STATUS_FUERA = "fuera_rango"
STATUS_VACIO = "vacio"

ESTADOS_REVISAR = {STATUS_NO_PARS, STATUS_FUERA, STATUS_RECUPERADO,
                   STATUS_REUBICADO}


# ---------------------------------------------------------------------------
# Parsing de UN numero BR
# ---------------------------------------------------------------------------

def _limpiar(s):
    """Quita moneda/porcentaje/espacios y separadores sueltos finales."""
    for ch in ("R$", "r$", "$", "%", " "):
        s = s.replace(ch, "")
    while s and s[-1] in ".,":
        s = s[:-1]
    return s


def _grupos_milhar_ok(entera, sep):
    """La parte entera debe ser digitos agrupados de 3 (el primero 1-3)."""
    if not entera or not entera[0].isdigit():
        return False
    partes = entera.split(sep)
    if not all(p.isdigit() for p in partes):
        return False
    if len(partes) == 1:
        return 1 <= len(partes[0]) <= 18
    if not (1 <= len(partes[0]) <= 3):
        return False
    return all(len(p) == 3 for p in partes[1:])


def _construir(entera, dec, sep_dec):
    """Arma int/float a partir de la parte entera y decimal ya validadas."""
    entera = entera.replace(".", "").replace(",", "")
    if not entera.isdigit() or not dec.isdigit():
        return None
    entero = int(entera)
    if not dec:
        return entero
    return float(entero) + float("0." + dec)
def _ndec_digitos(token):
    """Digitos tras el ultimo separador (0 si el caso es claramente MILHAR)."""
    if "." not in token and "," not in token:
        return 0
    i = max(token.rfind("."), token.rfind(","))
    grupo = token[i + 1:]
    n = sum(1 for c in grupo if c.isdigit())
    solo_tipo = ("." not in token) or ("," not in token)
    if solo_tipo and n == 3:  # '1.000' -> milhar
        return 0
    return n


def _parsear_una(token):
    """Parsea UN UNICO numero OCR; devuelve int/float o None si no fiable.

    Reglas:
      1) '.' y ',' juntos -> el ULTIMO separador es el decimal; los otros
         deben formar grupos de milhar de 3 digitos.
      2) Un solo tipo de separador, una sola vez:
           - ultimo grupo de 3  -> MILHAR (ej. '1.000' -> 1000)
           - ultimo grupo 1, 2 o 4+ -> DECIMAL (ej. '12,66' -> 12,66;
             '37,0000' -> 37,0)
      3) Mismo separador repetido:
           - ultimo grupo de 3  -> todos son milhar ('20.080.000' -> 20080000)
           - en otro caso el ultimo es DECIMAL y los grupos previos milhar.
    Si los grupos de milhar no son de 3 digitos -> None (no fiable).
    """
    s = _limpiar(token)
    if not s or not re.fullmatch(r"\d[\d.,]*", s):
        return None
    tiene_punto = "." in s
    tiene_coma = "," in s

    # 1) separadores mezclados
    if tiene_punto and tiene_coma:
        i = max(s.rfind("."), s.rfind(","))
        sep_dec = s[i]
        entera, dec = s[:i], s[i + 1:]
        sep_mil = "," if sep_dec == "." else "."
        if not _grupos_milhar_ok(entera, sep_mil):
            return None
        return _construir(entera, dec, sep_dec)

    # 2) sin separadores -> entero
    if not (tiene_punto or tiene_coma):
        return int(s) if s.isdigit() else None

    sep = "." if tiene_punto else ","
    n_seps = s.count(sep)
    i = s.rfind(sep)
    entera, dec = s[:i], s[i + 1:]

    # 3) separador repetido
    if n_seps >= 2:
        if len(dec) == 3:
            partes = s.split(sep)
            if (partes[0].isdigit() and 1 <= len(partes[0]) <= 3
                    and all(p.isdigit() and len(p) == 3 for p in partes[1:])):
                return int(s.replace(sep, ""))
            return None
        if not _grupos_milhar_ok(entera, sep):
            return None
        return _construir(entera, dec, sep)

    # 4) un unico separador
    if len(dec) == 3:
        if entera.isdigit() and dec.isdigit():
            return int(entera + dec)  # '1.000' -> 1000
        return None
    if not _grupos_milhar_ok(entera, sep):
        return None
    return _construir(entera, dec, sep)


def normalizar_numero(texto):
    """Convierte un texto OCR a numero BR (int/float). None si no fiable.

    Si el texto trae VARIOS numeros (OCR que junta celdas), se elige el
    ultimo con decimales (>=2 digitos); si ninguno tiene, el ultimo parseable.
    """
    if not isinstance(texto, str):
        return None
    tokens = REG_TOKEN_NUM.findall(texto)
    if not tokens:
        return None
    if len(tokens) == 1:
        return _parsear_una(tokens[0])
    elegibles = [t for t in tokens if _ndec_digitos(t) >= 2]
    if elegibles:
        return _parsear_una(elegibles[-1])
    for t in reversed(tokens):
        v = _parsear_una(t)
        if v is not None:
            return v
    return None


def primer_numero(texto):
    """Devuelve el PRIMER numero parseable del texto (para recuperar qtde)."""
    if not isinstance(texto, str):
        return None
    for t in REG_TOKEN_NUM.findall(texto):
        v = _parsear_una(t)
        if v is not None:
            return v
    return None
# ---------------------------------------------------------------------------
# Normalizacion de campos de la nota / producto + registros de revision
# ---------------------------------------------------------------------------

def _registro(archivo, chave, nivel, campo, crudo, valor, estado, motivo=""):
    return {"archivo": archivo, "chave": chave, "nivel": nivel,
            "campo": campo, "crudo": crudo, "valor": valor,
            "estado": estado, "motivo": motivo}


def normalizar_campo_valor(crudo, campo):
    """Normaliza UN valor; devuelve (numero, estado, motivo)."""
    if isinstance(crudo, (int, float)) and not isinstance(crudo, bool):
        # ya es numero (p.ej. al re-normalizar un JSON ya migrado)
        num = crudo
    elif crudo is None or (isinstance(crudo, str) and not crudo.strip()):
        return None, STATUS_VACIO, "vacio"
    else:
        num = normalizar_numero(crudo)
        if num is None:
            motivo = "no se puede interpretar de forma fiable"
            n_tok = len(REG_TOKEN_NUM.findall(str(crudo)))
            if n_tok > 1:
                motivo = "texto con varios numeros pero ninguno fiable"
            return None, STATUS_NO_PARS, motivo
    lim = RANGOS_CAMPOS.get(campo)
    if lim and not (lim[0] <= num <= lim[1]):
        return None, STATUS_FUERA, (
            f"valor {num} fuera del rango plausible [{lim[0]}, {lim[1]}] "
            f"para '{campo}'")
    if isinstance(crudo, str) and len(REG_TOKEN_NUM.findall(crudo)) > 1:
        return num, STATUS_LIMPIO, "texto con varios numeros; se tomo el relevante"
    return num, STATUS_OK, ""


def _recuperar_campos(prod, archivo, chave, idx, crudo_unit=None,
                      crudo_desc=None):
    """Recupera quantidade extraviada y precio unitario mal ubicado."""
    regs = []
    nivel = f"producto {idx}"
    qtde = prod.get("quantidade")
    if qtde in (None, 0, ""):
        v, origen = None, None
        ud = prod.get("unidade")
        if isinstance(ud, str):
            v = primer_numero(ud)          # 'TON 36,0000' -> 36.0
            origen = "unidade"
        if v is None:
            m = re.search(r"[Qq]tde[:.\s]*(\d[\d.,]*)",
                          str(prod.get("descricao") or ""))
            if m:
                v = normalizar_numero(m.group(1))
                origen = "descricao"
        if v is None and isinstance(crudo_unit, str):
            if len(REG_TOKEN_NUM.findall(crudo_unit)) >= 2:
                v = primer_numero(crudo_unit)  # '48,0000 5.907,0600' -> 48.0
                origen = "valor_unitario"
        if v is not None and 0 < v <= RANGOS_CAMPOS["quantidade"][1]:
            prod["quantidade"] = float(v)
            regs.append(_registro(archivo, chave, nivel, "quantidade",
                                  f"desde {origen}", float(v),
                                  STATUS_RECUPERADO,
                                  f"cantidad recuperada desde {origen}"))

    ud = prod.get("unidade")
    if isinstance(ud, str) and REG_TOKEN_NUM.search(ud):
        limpia = re.sub(r"\s*\d[\d.,]*\s*$", "", ud).strip()
        if limpia and limpia != ud:
            prod["unidade"] = limpia
            regs.append(_registro(archivo, chave, nivel, "unidade", ud, limpia,
                                  STATUS_LIMPIO,
                                  "cantidad separada de la unidade"))

    if prod.get("valor_unitario") in (None, 0, ""):
        desc = prod.get("desconto")
        if isinstance(desc, (int, float)) and desc >= 100.0:
            prod["valor_unitario"] = desc
            prod["desconto"] = None
            regs.append(_registro(archivo, chave, nivel, "valor_unitario",
                                  crudo_desc, desc, STATUS_REUBICADO,
                                  "precio unitario estaba en 'desconto'"))
    return regs
def _limpiar_codigos(prod, archivo, chave, idx, regs):
    """Limpia codigo/CFOP que el OCR junto con texto ('P015 DADOSADICIONAIS')."""
    nivel = f"producto {idx}"
    cod = prod.get("codigo")
    if isinstance(cod, str):
        palabras = cod.split()
        if len(palabras) > 1 and re.fullmatch(r"[A-Za-z]{1,4}\d{2,8}",
                                              palabras[0]):
            prod["codigo"] = palabras[0]
            regs.append(_registro(archivo, chave, nivel, "codigo", cod,
                                  palabras[0], STATUS_LIMPIO,
                                  "texto sobrante separado del codigo"))
    cf = prod.get("cfop")
    if isinstance(cf, str):
        m = re.search(r"^\d{4}", cf.strip())
        if m and m.group(0) != cf.strip():
            prod["cfop"] = m.group(0)
            regs.append(_registro(archivo, chave, nivel, "cfop", cf,
                                  m.group(0), STATUS_LIMPIO,
                                  "resto separado del CFOP"))


def normalizar_campos_numericos(info, archivo=None, chave=None):
    """Normaliza in-place los campos numericos de 'info' (nota + produtos).

    Devuelve la lista de registros de revision (incluye los OK).
    """
    if archivo is None:
        archivo = info.get("arquivo")
    if chave is None:
        chave = info.get("chave_de_acesso")
    registros = []

    for campo in CAMPOS_NOTA:
        crudo = info.get(campo)
        val, estado, motivo = normalizar_campo_valor(crudo, campo)
        info[campo] = val
        registros.append(_registro(archivo, chave, "nota", campo, crudo,
                                   val, estado, motivo))

    produtos = info.get("produtos")
    if isinstance(produtos, list):
        for i, prod in enumerate(produtos):
            if not isinstance(prod, dict):
                continue
            crudo_unit = prod.get("valor_unitario")
            crudo_desc = prod.get("desconto")
            for campo in CAMPOS_PRODUCTO:
                crudo = prod.get(campo)
                val, estado, motivo = normalizar_campo_valor(crudo, campo)
                prod[campo] = val
                registros.append(_registro(archivo, chave, f"producto {i+1}",
                                           campo, crudo, val, estado, motivo))
            registros.extend(_recuperar_campos(prod, archivo, chave, i + 1,
                                               crudo_unit, crudo_desc))
            _limpiar_codigos(prod, archivo, chave, i + 1, registros)
    return registros
# ---------------------------------------------------------------------------
# Reporte de normalizacion
# ---------------------------------------------------------------------------

def _celda_reporte(crudo, valor):
    c = repr(crudo) if crudo is not None else "-"
    v = repr(valor) if valor is not None else "-"
    return f"{c} -> {v}"


def generar_reporte(registros, ruta):
    """Escribe un reporte de texto con el detalle y las anomalias."""
    ruta = Path(ruta)
    por_nota = {}
    for r in registros:
        por_nota.setdefault(r["archivo"], []).append(r)

    cuenta = {}
    for r in registros:
        cuenta[r["estado"]] = cuenta.get(r["estado"], 0) + 1
    por_campo = {}
    for r in registros:
        d = por_campo.setdefault(r["campo"], {})
        d[r["estado"]] = d.get(r["estado"], 0) + 1

    lineas = []
    lineas.append("=" * 78)
    lineas.append(" REPORTE DE NORMALIZACION NUMERICA - NOTAS FISCALES BR")
    lineas.append(" Generado : " + datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    lineas.append(" Regla    : '.' milhar | ',' decimal | 2 digitos -> "
                  "decimal, 3 -> milhar")
    lineas.append("            Campos guardados SOLO como numero (sin milhar).")
    lineas.append("=" * 78)
    lineas.append("")
    lineas.append(" RESUMEN GENERAL")
    lineas.append("-" * 78)
    for st in (STATUS_OK, STATUS_LIMPIO, STATUS_RECUPERADO, STATUS_REUBICADO,
               STATUS_NO_PARS, STATUS_FUERA, STATUS_VACIO):
        lineas.append(f"   {st:<14}: {cuenta.get(st, 0):>4}")
    lineas.append("")
    lineas.append(" RESUMEN POR CAMPO (estado: cantidad)")
    lineas.append("-" * 78)
    for campo in CAMPOS_NOTA + CAMPOS_PRODUCTO + ["codigo", "cfop", "unidade"]:
        if campo in por_campo:
            detalle = ", ".join(f"{k}={v}" for k, v in
                                sorted(por_campo[campo].items()))
            lineas.append(f"   {campo:<15}: {detalle}")
    lineas.append("")
    lineas.append(" ANOMALIAS QUE REQUIEREN REVISION MANUAL")
    lineas.append("-" * 78)
    n_anom = 0
    for archivo in sorted(por_nota):
        for r in por_nota[archivo]:
            if r["estado"] in ESTADOS_REVISAR:
                n_anom += 1
                lineas.append(
                    f"   [{n_anom:>3}] {archivo} | {r['nivel']:<9} "
                    f"{r['campo']:<15} | {_celda_reporte(r['crudo'], r['valor'])}"
                    f" | {r['estado']}"
                    + (f" | {r['motivo']}" if r["motivo"] else ""))
    if n_anom == 0:
        lineas.append("   (ninguna)")
    lineas.append("")
    lineas.append(" DETALLE POR NOTA (crudo -> numero)")
    lineas.append("-" * 78)
    for archivo in sorted(por_nota):
        lineas.append(f"  == {archivo} ==")
        for r in por_nota[archivo]:
            flag = "<--" if r["estado"] in ESTADOS_REVISAR else ""
            lineas.append(f"     {r['nivel']:<9} {r['campo']:<15} | "
                          f"{_celda_reporte(r['crudo'], r['valor']):<28} "
                          f"| {r['estado']} {flag}")
    lineas.append("=" * 78)
    lineas.append(f"FIN DEL REPORTE ({len(registros)} valores procesados, "
                  f"{n_anom} anomalias)")

    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return n_anom
if __name__ == "__main__":
    # Autotest rapido con los casos observados en las notas reales
    casos = [
        ("215.380,40", 215380.40),        # BR puro -> float
        ("209.559,31", 209559.31),
        ("189.025,92", 189025.92),
        ("215.380.40", 215380.40),        # coma leida como punto
        ("12.664.37", 12664.37),
        ("2.152.94", 2152.94),
        ("5.821.0919", 5821.0919),        # 4 decimales tras el punto
        ("37.000,000", 37000.0),          # milhar + decimal en pesos
        ("37,0000", 37.0),                # cantidad con 4 decimales
        ("37.0000", 37.0),                # coma leida como punto
        ("36.0000", 36.0),
        ("32,0000", 32.0),
        ("5.821,0919", 5821.0919),
        ("5.907,0600", 5907.06),
        ("5.821,0925", 5821.0925),
        ("16.672.09", 16672.09),
        ("12.322,09", 12322.09),
        ("1.000", 1000),                  # 3 digitos -> milhar
        ("12.664", 12664),
        ("20.080.000", 20080000),
        ("0", 0),
        ("0,00", 0.0),
        ("0,00|17.000,0", 0.0),           # IPI 0 con resto OCR
        ("48,0000 5.907,0600", 5907.06),  # qtde+precio juntos -> precio
        (None, None),
        ("", None),
    ]
    fallos = 0
    print(" PRUEBA DE normalizar_numero")
    print("-" * 60)
    for txt, esperado in casos:
        got = normalizar_numero(txt)
        ok = (got == esperado) or (got is None and esperado is None)
        if not ok:
            fallos += 1
        print(f"   {'OK ' if ok else 'XX '} {str(txt):<20} -> {got!r}"
              f"  (esperado {esperado!r})")
    casos_none = ["0.0017,000.", "0,0017,000,00", "0.0017.00",
                  "0.0017.000.0", "ICMSTIPI 0.0017.000.00"]
    for txt in casos_none:
        got = normalizar_numero(txt)
        ok = got is None
        if not ok:
            fallos += 1
        print(f"   {'OK ' if ok else 'XX '} {str(txt):<25} -> {got!r}"
              f"  (esperado None)")
    print("-" * 60)
    print(f"   Total fallos: {fallos}")
    raise SystemExit(0 if fallos == 0 else 1)