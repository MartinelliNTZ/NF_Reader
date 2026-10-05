# -*- coding: utf-8 -*-
"""
Gera um CSV tabulado a partir do JSON acumulador de notas fiscales
(generado por extrair_notas.py).

Uso:
    python core/generar_csv.py [--json notas_fiscales.json] [--csv notas_fiscales.csv]
                               [--incluir-ocr]

Formato de saida (validado com Excel COM nesta maquina):
    - Separador   : ',' (list separator da maquina/locale; Excel pt-BR
      reconhece a virgula ao abrir).
    - Decimais    : ponto (215380.4 vira NUMERO no Excel; com virgula o
      valor seria lido como TEXTO).
    - Numeros longos: puros digitos com >=12 caracteres ou com zero a
      esquerda saem como formula ="..." (texto fiel, sem notacao
      cientifica e sem perder zeros - ex.: a chave de 44 digitos).
    - Codificacion: UTF-8 con BOM (caracteres especiais corretos em Excel).
    - Salto       : CRLF.
    - Uma FILA por cada PRODUTO da nota (formato longo). Se a nota nao tem
      produtos, igual se emite uma fila com os campos de produto vacios.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Definicion de columnas
# ---------------------------------------------------------------------------

# Columnas a nivel de nota: (encabezado, ruta anidada no JSON "a.b.c")
COLUMNAS_NOTA = [
    ("chave_de_acesso",               "chave_de_acesso"),
    ("chave_valida",                  "chave_valida"),
    ("status_extracao",               "status_extracao"),
    ("motivo_extracao_parcial",       "motivo_extracao_parcial"),
    ("arquivo",                       "arquivo"),
    ("paginas",                       "paginas"),
    ("modelo",                        "modelo"),
    ("origem_texto",                  "origem_texto"),
    ("numero_nota",                   "numero_nota"),
    ("serie",                         "serie"),
    ("data_emissao",                  "data_emissao"),
    ("data_entrada_saida",            "data_entrada_saida"),
    ("hora_entrada_saida",            "hora_entrada_saida"),
    ("valor_da_nota",                 "valor_da_nota"),
    ("natureza_da_operacao",          "natureza_da_operacao"),
    ("protocolo_autorizacao",         "protocolo_autorizacao"),
    ("qr",                            "qr"),
    ("emitente_razao_social",         "emitente.razao_social"),
    ("emitente_cnpj_cpf",             "emitente.cnpj_cpf"),
    ("emitente_inscricao_estadual",   "emitente.inscricao_estadual"),
    ("destinatario_nome",             "destinatario.nome"),
    ("destinatario_cnpj_cpf",         "destinatario.cnpj_cpf"),
    ("destinatario_endereco",         "destinatario.endereco"),
    ("destinatario_municipio",        "destinatario.municipio"),
    ("destinatario_uf",               "destinatario.uf"),
    ("destinatario_cep",              "destinatario.cep"),
    ("destinatario_pais",             "destinatario.pais"),
    ("destinatario_telefono",         "destinatario.telefono"),
    ("destinatario_inscricao_estadual", "destinatario.inscricao_estadual"),
    ("transportadora_razao_social",   "transportadora.razao_social"),
    ("transportadora_cnpj_cpf",       "transportadora.cnpj_cpf"),
    ("transportadora_placa",          "transportadora.placa"),
    ("peso_bruto",                    "peso_bruto"),
    ("peso_liquido",                  "peso_liquido"),
    ("dados_adicionais",              "dados_adicionais"),
]

# Columnas de cada produto (dentro do arreglo "produtos").
# produto_item (indice 1..n) se genera no momento.
COLUMNAS_PRODUCTO = [
    ("produto_item",           None),
    ("produto_codigo",         "codigo"),
    ("produto_descricao",      "descricao"),
    ("produto_ncm",            "ncm"),
    ("produto_origem_cst",     "origem_cst"),
    ("produto_cfop",           "cfop"),
    ("produto_unidade",        "unidade"),
    ("produto_quantidade",     "quantidade"),
    ("produto_valor_unitario", "valor_unitario"),
    ("produto_descuento",      "desconto"),
    ("produto_valor_total",    "valor_total"),
    ("produto_bc_icms",        "bc_icms"),
    ("produto_valor_icms",     "valor_icms"),
    ("produto_valor_ipi",      "valor_ipi"),
    ("produto_alicuota",       "aliquota"),
]

# Campos marcados por padrao no grid/filtro de colunas da interface.
# O restante das colunas (nota + produto) fica desmarcado por padrao.
CAMPOS_PREDEFINIDOS = frozenset({
    "chave_de_acesso",
    "peso_bruto",
    "peso_liquido",
    "produto_item",
    "produto_codigo",
    "produto_descricao",
})


def listar_colunas():
    """Lista ordenada (nota + produto) dos campos selecionaveis do CSV.

    Usada pela interface para montar o grid de checkboxes na mesma ordem em
    que as colunas sao escritas no CSV.
    """
    return [
        titulo
        for titulo, _ruta in (COLUMNAS_NOTA + COLUMNAS_PRODUCTO)
    ]


def _obtener(diccionario, ruta):
    """Le un valor anidado com notacion 'a.b.c'."""
    actual = diccionario
    for parte in ruta.split("."):
        if not isinstance(actual, dict):
            return None
        actual = actual.get(parte)
        if actual is None:
            return None
    return actual


def _como_texto_literal(valor):
    """Envolve em formula de texto =\"...\" para preservar o valor fiel.

    Numeros longos (>=12 digitos puros) viram notacao cientifica no Excel e
    inteiros com zero a esquerda perdem os zeros - a formula resolve os
    dois casos mantendo o conteudo identico ao extraido (chave de 44
    digitos, protocolos, etc.).
    """
    s = str(valor)
    if s.isdigit() and (len(s) >= 12 or (len(s) > 1 and s.startswith("0"))):
        return '="' + s + '"'
    return s


def _celda(valor):
    """Convierte un valor del JSON a texto de celda (ver regras abaixo)."""
    if valor is None:
        return ""
    if valor is True:
        return "SI"
    if valor is False:
        return "NAO"
    if isinstance(valor, (int, float)):
        return _numero_a_texto(valor)
    # texto: chave de acesso / digitos longos preservados como ="..."
    return _como_texto_literal(valor)


def _numero_a_texto(valor):
    """Numero -> digitos sem separador de milhar e con PONTO decimal.

    Ex.: 215380.4 / 37000 - Excel parseia como NUMERO (com ',' viraria
    texto neste CSV de delimitor ',' ja validado).
    """
    f = float(valor)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.12g}"


def carregar_json(ruta):
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


def construir_filas(datos, incluir_ocr=False, colunas=None):
    """Devolve (cabeceras, filas). Uma fila por produto de cada nota.

    colunas: colecao de titulos a manter (filtro de dados da interface).
             None (por omissao) mantem TODAS as colunas.
    """
    filtro = set(colunas) if colunas is not None else None

    columnas_nota = [
        col for col in COLUMNAS_NOTA
        if filtro is None or col[0] in filtro
    ]
    columnas_producto = [
        col for col in COLUMNAS_PRODUCTO
        if filtro is None or col[0] in filtro
    ]

    cabeceras = [titulo for titulo, _ruta in columnas_nota]
    cabeceras += [titulo for titulo, _ruta in columnas_producto]
    if incluir_ocr:
        cabeceras.append("texto_ocr")

    filas = []
    for chave, nota in datos.items():
        produtos = nota.get("produtos")
        if not isinstance(produtos, list) or not produtos:
            produtos = [{}]
        for item, prod in enumerate(produtos, 1):
            fila = [_celda(_obtener(nota, ruta)) for _titulo, ruta in columnas_nota]
            for _titulo, ruta in columnas_producto:
                if ruta is None:
                    fila.append(str(item))
                else:
                    fila.append(_celda(prod.get(ruta)))
            if incluir_ocr:
                fila.append(_celda(nota.get("texto_ocr")))
            filas.append(fila)
    return cabeceras, filas


def guardar_csv(csv_path, cabeceras, filas):
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        # ',' = list separator da maquina (validado com Excel COM);
        # decimais ja chegam com ponto em _celda
        writer = csv.writer(f, delimiter=",", quoting=csv.QUOTE_MINIMAL,
                            lineterminator="\r\n")
        writer.writerow(cabeceras)
        writer.writerows(filas)


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Gera um CSV tabulado a partir do JSON de notas fiscales.")
    parser.add_argument("--json", dest="json_path", default="notas_fiscales.json",
                        help="JSON acumulador (por omissao: notas_fiscales.json).")
    parser.add_argument("--csv", dest="csv_path", default="notas_fiscales.csv",
                        help="Arquivo CSV de saida (por omissao: notas_fiscales.csv).")
    parser.add_argument("--incluir-ocr", action="store_true",
                        help="Agregar a columna 'texto_ocr' ao final do CSV.")
    args = parser.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    json_path = Path(args.json_path)
    if not json_path.is_absolute():
        json_path = base / json_path
    csv_path = Path(args.csv_path)
    if not csv_path.is_absolute():
        csv_path = base / csv_path

    print("=" * 70)
    print(" GENERACION DE CSV (JSON -> CSV tabulado)")
    print(f" JSON: {json_path}")
    print(f" CSV : {csv_path}")
    print("=" * 70)

    if not json_path.exists():
        print(f"!! NAO existe o JSON: {json_path}")
        print("   Primeiro execute: python extrair_notas.py")
        return 1

    datos = carregar_json(json_path)
    cabeceras, filas = construir_filas(datos, incluir_ocr=args.incluir_ocr)
    guardar_csv(csv_path, cabeceras, filas)

    print(f" Notas no JSON          : {len(datos)}")
    print(f" Filas generadas        : {len(filas)}")
    print(f" Columnas               : {len(cabeceras)}")
    print(f" CSV salvado en         : {csv_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())