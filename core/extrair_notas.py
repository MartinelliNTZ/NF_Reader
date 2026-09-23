# -*- coding: utf-8 -*-
"""
Extrai informacoes de Notas Fiscais (NF-e / DANFE) a partir de arquivos PDF
(pastas e subpastas da raiz), usando a CHAVE DE ACESSO como chave de um JSON
acumulador.

Uso:
    python core/extrair_notas.py [--raiz PASTA] [--json ARQUIVO.json] [--dpi 300]
                                 [--sem-ocr]

Requisitos:  pip install pymupdf opencv-python rapidocr-onnxruntime

Se o JSON ja existe: para cada PDF a chave ja existente e ATUALIZADA,
chaves novas sao INSERIDAS. PDFs que nao puderem ser processados sao pulados
e listados no resumo final com o motivo.

Cada registro guarda o campo "status_extracao": "completa" o "parcial".
Se a chave de acesso encontrada tem o digito verificador invalido (por
exemplo por erro de OCR), o PDF NAO e descartado: os dados possibles sao
extraidos e guardados como PARCIAIS, com o motivo em
"motivo_extracao_parcial".
"""

import argparse
import json
import os
import re
import sys
import traceback
import unicodedata
from pathlib import Path

try:
    import pymupdf  # versao nova (1.24+)
except ImportError:
    import fitz as pymupdf  # versao antiga

try:
    from .normalizar_numeros import (generar_reporte, normalizar_campos_numericos,
                                     normalizar_numero)
except ImportError:  # ejecucion directa: python core/extrair_notas.py
    from normalizar_numeros import (generar_reporte, normalizar_campos_numericos,
                                    normalizar_numero)

try:
    from .rastreio import configurar_log, log
except ImportError:  # ejecucion directa: python core/extrair_notas.py
    from rastreio import configurar_log, log

# ---------------------------------------------------------------------------
# Utilidades de normalizacao
# ---------------------------------------------------------------------------

def _sem_acentos(texto):
    s = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in s if not unicodedata.combining(c))


def normaliza(texto):
    """Lowercase sem acentos, mantendo pontuacao."""
    return _sem_acentos(str(texto)).lower()


def compacta(texto):
    """Somente letras/digitos, tudo junto - usado p/ casar rotulos."""
    t = normaliza(texto)
    return "".join(c for c in t if c.isalnum())


REG_DATA = r"\d{2}/\d{2}/\d{4}"
REG_CNPJ = r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"
REG_CPF = r"\d{3}\.\d{3}\.\d{3}-\d{2}"
REG_VALOR = r"\d{1,3}(?:\.\d{3})*,\d{2}"


def validar_chave(chave):
    """Valida chave de acesso NFe de 44 digitos (modulo 11)."""
    chave = "".join(c for c in chave if c.isdigit())
    if len(chave) != 44:
        return False
    soma = 0
    peso = 2
    for d in reversed(chave[:43]):
        soma += int(d) * peso
        peso = peso + 1 if peso < 9 else 2
    dv = 11 - (soma % 11)
    if dv >= 10:
        dv = 0
    return str(dv) == chave[43]


# ---------------------------------------------------------------------------
# Leitura do PDF: texto embutido ou OCR (RapidOCR)
# ---------------------------------------------------------------------------

ESCALA = 300 / 72.0  # converte pontos do PDF p/ mesma escala do OCR a 300dpi


def ler_paginas_texto(caminho):
    """Tenta extrair texto com coordenadas diretamente do PDF."""
    itens = []
    doc = pymupdf.open(caminho)
    try:
        for p in doc:
            palavras = p.get_text("words")  # x0,y0,x1,y1,palavra,...
            for (x0, y0, x1, y1, pal, *_resto) in palavras:
                itens.append({
                    "texto": pal,
                    "x": (x0 + x1) / 2 * ESCALA,
                    "y": (y0 + y1) / 2 * ESCALA,
                    "conf": 1.0,
                    "pagina": p.number + 1,
                })
    finally:
        doc.close()
    return itens if itens else None


_OCR_ENGINE = None


def obter_engine_ocr():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        print(">> Inicializando motor de OCR (RapidOCR)...")
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _itens_da_pagina(img_bgr, engine, pagina):
    resultado, _elapse = engine(img_bgr)
    itens = []
    if not resultado:  # quando nao detecta nada, resultado e None
        return itens
    for box, texto, conf in resultado:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        itens.append({
            "texto": texto,
            "x": (min(xs) + max(xs)) / 2,
            "y": (min(ys) + max(ys)) / 2,
            "conf": float(conf),
            "pagina": pagina,
        })
    return itens


def ler_paginas_ocr(caminho, engine, dpi):
    itens = []
    import io
    import numpy as np

    doc = pymupdf.open(caminho)
    try:
        for p in doc:
            pix = p.get_pixmap(dpi=dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n == 4:
                import cv2
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            itens_pag = _itens_da_pagina(img, engine, p.number + 1)
            itens.extend(itens_pag)
    finally:
        doc.close()
    return itens
# ---------------------------------------------------------------------------
# Helpers de extracao de campos
# ---------------------------------------------------------------------------

# rotulos que NUNCA sao "valores" (excluidos ao buscar o valor ao lado)
ROTULOS_EXCLUIR = {
    "nomerazaosocial", "razaosocial", "destinatarioremetente",
    "cnpjcpf", "cnpj", "datadeemissao", "datadeentradasaida",
    "datadeentradasaid", "dataderecebimento", "endereco", "municipio",
    "bairro", "cep", "uf", "pais", "fonefax", "fone", "inscricaoestadual",
    "inscestadualsubtributaria", "serie", "valordanota",
    "naturezadaoperacao", "naturaleza", "protdeautorizacao", "protocolo",
    "chavedeacesso", "formadepagamento", "formapagamento", "valortroco",
    "calculoimposto", "valordofrete", "valordoseguro", "desconto",
    "valortotaldosprodutos", "valordoicms", "basedecalculodoicms",
    "valordoicmsdesubstituicao", "basedecalculodoicmsdesubstituicao",
    "valortotaldanota", "vlraproxdostributos", "codigoannt",
    "placadoveiculo", "placadovehiculo", "pesobruto", "pesoliquido",
    "numeracao", "marca", "especie", "quantidade",
    "dadosdoprodutoservicos", "dadosdoprodutoservicios",
    "dadosadicionais", "datosadicionais", "informacoescomplementarias",
    "reservadoaofisco", "identificadaeassinatura",
    "osprodutosconstantesn", "transportadorvolumestransportados",
    "transportador", "datadeemisao", "crtcodigoregime", "crtcodigo",
    "3regimennormal", "normal", "folha", "consultaautenticidadenoportalnacionaldanfe",
    "wwwnfefazendagovbrportal", "ounositedasafautorizadora",
    "documentoauxiliarda", "notafiscaleletronica", "fretes",
}


def achar_rotulo(itens, rotulos, y_min=0, y_max=1e18, pagina=None):
    """Faz match por substring do compacto de cada token de texto."""
    alvos = [compacta(r) for r in rotulos]
    for e in itens:
        if not (y_min <= e["y"] <= y_max):
            continue
        if pagina is not None and e["pagina"] != pagina:
            continue
        ct = compacta(e["texto"])
        for alvo in alvos:
            if alvo and alvo in ct:
                return e
    return None


def valor_ao_lado(itens, rotulo, padrao, margem=45, abre_lado=False,
                  y_min=None, y_max=None, pagina=None, lado="direita"):
    """Procura o valor na mesma linha (ao lado) ou na linha de abaixo."""
    x0 = rotulo["x"]
    y0 = rotulo["y"]
    cand = []
    for e in itens:
        if pagina is not None and e["pagina"] != pagina:
            continue
        if y_min is not None and e["y"] < y_min:
            continue
        if y_max is not None and e["y"] > y_max:
            continue
        dx = e["x"] - x0
        dy = abs(e["y"] - y0)
        if dy <= margem + 25 and abs(dx) <= 60:
            # mesma linha ou por debaixo, mesma columna
            cand.append((dy + 1000, dx, e))
        elif dy <= margem:
            # mesma linha, ao lado (segundo "lado")
            if lado == "ambos":
                ok = -300 <= dx <= 900
            elif lado == "esquerda":
                ok = -300 <= dx <= -5
            else:
                ok = (0 if abre_lado else 10) <= dx <= 900
            if ok:
                cand.append((dy, dx, e))
    cand.sort(key=lambda t: (abs(t[2]["x"] - x0) + 20 * abs(t[2]["y"] - y0),
                             abs(t[2]["x"] - x0)))
    for _dy, _dx, e in cand:
        ct = compacta(e["texto"])
        if any(ct.startswith(r) for r in ROTULOS_EXCLUIR):
            continue
        m = re.search(padrao, e["texto"])
        if m:
            if m.lastindex:
                return m.group(1).strip()
            return m.group(0).strip()
    return None


def pegar_valor(itens, rotulos, padrao, margem=45, y_min=0, y_max=1e18,
                pagina=None, lado="direita"):
    e = achar_rotulo(itens, rotulos, y_min, y_max, pagina)
    if not e:
        return None
    return valor_ao_lado(itens, e, padrao, margem, y_min=y_min,
                         y_max=y_max, pagina=pagina, lado=lado)


def _janela_valida(digitos, limite=44):
    """Busca uma janela de 44 digitos com DV valido dentro de 'digitos'."""
    for i in range(len(digitos) - limite + 1):
        k = digitos[i:i + limite]
        if validar_chave(k):
            return k
    return None


def extrair_chave_acesso(itens):
    """Localiza a chave de acesso (44 digitos) junto ao rotulo CHAVE DE ACESSO.

    Procura um token com "CHAVE" (variante OCR) e une os digitos das filas
    proximas. Como padrao se exige uma janela de 44 digitos com DV valido
    nessa zona. Somente como ultimo recurso varre fila por fila (nunca une
    digitos de filas distintas, para evitar chaves falsas).
    """
    rotulos_achados = []
    for e in itens:
        ct = compacta(e["texto"])
        if "chavedeacesso" in ct or "chavedacesso" in ct or ct.startswith("chav"):
            rotulos_achados.append(e)

    # 1) junto ao rotulo (mesma fila e filas adjacentes)
    for e in rotulos_achados:
        vizinhos = [t for t in itens
                    if abs(t["y"] - e["y"]) <= 50 and abs(t["x"] - e["x"]) <= 850]
        dig = "".join(re.findall(r"\d", "".join(t["texto"] for t in vizinhos)))
        janela = _janela_valida(dig)
        if janela:
            return janela, True

    # 2) plano B: fila por fila
    if not rotulos_achados:
        filas = []
        for t in sorted(itens, key=lambda t: (t["y"], t["x"])):
            agreg = False
            for f in filas:
                if abs(f[0]["y"] - t["y"]) <= 12:
                    f.append(t)
                    agreg = True
                    break
            if not agreg:
                filas.append([t])
        for f in filas:
            f.sort(key=lambda t: t["x"])
        for f in filas:
            dig = "".join(re.findall(r"\d", "".join(t["texto"] for t in f)))
            janela = _janela_valida(dig)
            if janela:
                return janela, True

    # 3) rotulo presente sem chave valida -> devolve a secuencia como invalida
    for e in rotulos_achados:
        vizinhos = [t for t in itens
                    if abs(t["y"] - e["y"]) <= 50 and abs(t["x"] - e["x"]) <= 850]
        dig = "".join(re.findall(r"\d", "".join(t["texto"] for t in vizinhos)))
        for i in range(len(dig) - 43):
            k = dig[i:i + 44]
            if len(k) == 44 and not validar_chave(k):
                return k, False
    return None, False


def extrair_produtos(itens, y_ini, y_fim, pagina):
    """Tabela de produtos entre DADOS DO PRODUTO e DADOS ADICIONAIS."""
    regiao = [e for e in itens
              if y_ini <= e["y"] <= y_fim and e["pagina"] == pagina]
    if not regiao:
        return []

    # agrupa por linha (tolerancia do dobre de linha a 300dpi)
    linhas = []
    for e in sorted(regiao, key=lambda t: (t["y"], t["x"])):
        encaixado = False
        for l in linhas:
            if abs(l[0]["y"] - e["y"]) <= 28:
                l.append(e)
                encaixado = True
                break
        if not encaixado:
            linhas.append([e])
    for l in linhas:
        l.sort(key=lambda t: t["x"])
    linhas.sort(key=lambda l: l[0]["y"])

    # linha do cabecalho da tabela (com fallbacks p/ variantes de DANFE/OCR)
    # 'CODIGO' ou 'CODPRODUTO' (o OCR costuma ler assim em DANFEs de calcario)
    def _tem_codigo(txt):
        return "CODIGO" in txt or "CODPRODUTO" in txt or "CODPROD" in txt

    cab = None
    cab_crit = None
    for l in linhas:
        txt = " ".join(e["texto"] for e in l).upper()
        if _tem_codigo(txt) and "CFOP" in txt and "VLR UNIT" in txt.replace("VLRUNIT", "VLR UNIT"):
            cab, cab_crit = l, "1. CODIGO+CFOP+VLR UNIT"
            break
    if cab is None:
        for l in linhas:
            txt = " ".join(e["texto"] for e in l).upper()
            if _tem_codigo(txt) and "CFOP" in txt:
                cab, cab_crit = l, "2. CODIGO+CFOP"
                break
    if cab is None:
        # alguns emissores nao imprimem a coluna CFOP no cabecalho; outros PDFs
        # tem o cabecalho espalhado por varias linhas pelo OCR. O que nunca
        # falta e CODIGO + DESCRICAO (ou so CODIGO) na tabela de produtos.
        for l in linhas:
            txt = " ".join(e["texto"] for e in l).upper()
            if _tem_codigo(txt) and ("DESCRICAO" in txt or "DESCRIPCION" in txt):
                cab, cab_crit = l, "3. CODIGO+DESCRICAO (sem CFOP no cab)"
                break
    if cab is None:
        for l in linhas:
            txt = " ".join(e["texto"] for e in l).upper()
            if _tem_codigo(txt):
                cab, cab_crit = l, "4. CODIGO somente (fallback final)"
                break
    if cab is None:
        log.warning("produtos: cabecalho da tabela NAO encontrado "
                    "(regiao y=%.0f..%.0f p%s, itens=%d)",
                    y_ini, y_fim, pagina, len(regiao))
        return []
    log.info("produtos: cabecalho detectado pelo criterio '%s'", cab_crit)

    NOMES = {
        "codigo": "codigo", "codproduto": "codigo",
        "descricao do produto": "descricao",
        "descricao do": "descricao",
        "descricao": "descricao",
        "ncmsh": "ncm", "ncm": "ncm", "nomsi": "ncm",
        "orig/cst": "origem_cst", "origicst": "origem_cst", "cst": "origem_cst",
        "cfop": "cfop",
        "unid": "unidade", "unidade": "unidade", "unti": "unidade",
        "qtde": "quantidade", "otde": "quantidade", "qtdb": "quantidade",
        "vlr unit": "valor_unitario", "vlrunit": "valor_unitario",
        "vlrunt": "valor_unitario", "valorunitario": "valor_unitario",
        "desc": "desconto", "desu": "desconto",
        "vlr total": "valor_total", "vlrtotal": "valor_total",
        "valortotal": "valor_total",
        "bc icms": "bc_icms", "basecalc": "bc_icms",
        "vlr icms": "valor_icms", "vlricms": "valor_icms",
        "valoricms": "valor_icms", "gcicms": "valor_icms",
        "vlr ipi": "valor_ipi", "vlripi": "valor_ipi", "vlrtpi": "valor_ipi",
        "valoripi": "valor_ipi",
        "aliquot": "aliquota", "aliouota": "aliquota", "alicuota": "aliquota",
    }
    # Coleta as colunas de TODA a regiao (nao so da linha do cabecalho):
    # o OCR as vezes espalha o cabecalho por varias linhas entrelacadas com a
    # primeira linha de dados. Tokens de dados reais (numeros, nomes) nao
    # casam com estes rotulos, entao nao ha risco de pegar valor como coluna.
    colunas = []
    titulos = set()   # objetos que sao rotulos de coluna (nunca valores)
    nomes_vistos = set()
    for e in sorted(regiao, key=lambda t: (t["y"], t["x"])):
        ct = compacta(e["texto"])
        # um rotulo de coluna nao tem digitos: com isso, textos como
        # 'Lote:IMP197022-Qtde:37' nao viram uma coluna 'quantidade' fantasma
        if any(c.isdigit() for c in ct):
            continue
        for k, v in NOMES.items():
            if k and compacta(k) in ct:
                if v not in nomes_vistos:
                    colunas.append({"x": e["x"], "nome": v})
                    nomes_vistos.add(v)
                titulos.add(id(e))
                break

    def coluna_para(cx):
        melhor = None
        for c in colunas:
            dist = abs(cx - c["x"])
            if melhor is None or dist < melhor[0]:
                melhor = (dist, c)
        return melhor[1]["nome"] if melhor and melhor[0] <= 320 else None

    log.info("produtos: colunas mapeadas (%d): %s", len(colunas),
             [c["nome"] for c in colunas])

    produtos = []
    atual = None
    notas_vistas = False
    for l in linhas:
        if l is cab:
            continue
        linha = {}
        for e in l:
            if id(e) in titulos:
                continue  # rotulo de coluna, nao e valor
            nome = coluna_para(e["x"])
            if nome:
                linha.setdefault(nome, []).append(e["texto"])
        celulas = {k: " ".join(v) for k, v in linha.items()}
        # inicio de produto: CODIGO + DESCRICAO juntos. Em layouts/OCR que
        # separam o codigo em outra banda de y (ex.: calcario), abre-se tambem
        # com DESCRICAO + QUANTIDADE, mas SEMPRE que ainda nao haja um produto
        # em construcao - senao a continuacao do nome ('Granulado' em nota de 2
        # linhas) viraria um segundo produto fantasma.
        eh_dado = (
            ("codigo" in celulas and "descricao" in celulas)
            or (atual is None
                and "descricao" in celulas and "quantidade" in celulas)
        )
        if eh_dado:
            if atual is not None:
                produtos.append(atual)
            atual = celulas
            notas_vistas = False
            log.debug("produtos: novo item -> %s",
                      {k: v for k, v in celulas.items()})
        elif atual is not None:
            if not notas_vistas:
                es_nota = any(k == "descricao" and v.lower().startswith("obs:")
                              for k, v in celulas.items())
                if es_nota:
                    notas_vistas = True
                    log.debug("produtos: linha de 'obs' vista; valores "
                              "nao-descricao aproveitados -> %s",
                              {k: v for k, v in celulas.items()
                               if k != "descricao"})
                    # a obs nao entra na descricao, mas pode estar na MESMA
                    # linha que valores numericos (OCR agrupou tudo) - entao
                    # aproveitamos apenas as demais colunas da linha
                    for k, v in celulas.items():
                        if k == "descricao":
                            continue
                        atual[k] = (atual.get(k, "") + " " + v).strip()
                else:
                    for k, v in celulas.items():
                        atual[k] = (atual.get(k, "") + " " + v).strip()
            else:
                log.debug("produtos: linha descartada apos obs -> %s",
                          {k: v for k, v in celulas.items()})
    if atual is not None:
        produtos.append(atual)

    # ---- rastreio do resultado ----
    log.info("produtos: %d item(ns) extraido(s)", len(produtos))
    for i, p in enumerate(produtos, 1):
        desc = str(p.get("descricao") or "").strip()
        if not desc:
            log.warning("produtos: item %d SEM DESCRICAO "
                        "(codigo=%r qtde=%r)", i, p.get("codigo"),
                        p.get("quantidade"))
        else:
            log.info("produtos: item %d codigo=%r descricao=%r "
                     "ncm=%r und=%r qtde=%r vlr_unit=%r vlr_total=%r",
                     i, p.get("codigo"), desc[:90], p.get("ncm"),
                     p.get("unidade"), p.get("quantidade"),
                     p.get("valor_unitario"), p.get("valor_total"))
    return produtos


def extrair_infos(itens, arquivo_rel):
    """Construi um dict completo com os dados da nota."""
    chave, chave_valida = extrair_chave_acesso(itens)
    paginas = sorted({e["pagina"] for e in itens})
    modelo = "desconhecido"
    if any("danfe" in compacta(e["texto"]) for e in itens):
        modelo = "NF-e (DANFE)"
    elif any("nfe" in compacta(e["texto"]) for e in itens):
        modelo = "NF-e"

    compacto_total = "".join(compacta(e["texto"]) for e in itens)

    info = {
        "arquivo": arquivo_rel.replace("\\", "/"),
        "paginas": len(paginas),
        "chave_de_acesso": chave,
        "chave_valida": chave_valida,
        "modelo": modelo,
    }

    # ---- numero de nota ----
    numero = None
    for e in itens:
        if re.fullmatch(r"[Nn][°ºoO0]?\.?|no\.?", e["texto"].strip()) and \
                len(e["texto"].strip()) <= 3:
            v = valor_ao_lado(itens, e, r"(\d{3,12})")
            if v:
                numero = v
                break
    if not numero:
        m = re.search(r"n(\d{3,9})", compacto_total)
        if m and 3 <= len(m.group(1)) <= 9:
            numero = m.group(1)
    info["numero_nota"] = numero

    serie = pegar_valor(itens, ["serie"], r"([A-Za-z0-9\-]{1,6})", y_max=900)
    if not serie:
        for e in itens:
            ct = compacta(e["texto"])
            if ct.startswith("serie") and len(ct) > 5:
                serie = e["texto"][5:].strip()
                break
    info["serie"] = serie

    info["data_emissao"] = pegar_valor(itens, ["datadeemissao", "datadeemission"],
                                       REG_DATA)
    info["data_entrada_saida"] = pegar_valor(
        itens, ["datadeentradasaid", "datadeentradasaida", "datadeentrada"],
        REG_DATA)
    info["hora_entrada_saida"] = pegar_valor(
        itens, ["horadeentrada/said", "horadeentrada"], r"\d{2}:\d{2}")
    info["valor_da_nota"] = pegar_valor(itens, ["valordanota", "valordalanota"],
                                        REG_VALOR)
    naturaleza = pegar_valor(
        itens, ["natureza daoperacao", "natureza"],
        r"(.{10,90})", margem=70, y_max=900)
    if naturaleza:
        # recorta tudo o que vem depois de um "rotulo" de outra linha
        naturaleza = re.split(r"\s+[A-ZÀ-Ú]{2,}[\s(]", naturaleza)[0].strip()
    info["natureza_da_operacao"] = naturaleza

    prot = pegar_valor(itens, ["protdeautorizacao", "prot.deautorizacao",
                               "protocolo"], r"(\d{10,20})", y_max=900)
    info["protocolo_autorizacao"] = prot

    info["qr"] = next((e["texto"] for e in itens
                       if "http" in e["texto"].lower()), None)

    # ---- emitente ----
    emitente = {}
    cn = next((e for e in itens
               if re.search(REG_CNPJ, e["texto"])), None)
    if cn:
        cnpj = re.search(REG_CNPJ, cn["texto"])
        if cnpj:
            emitente["cnpj_cpf"] = cnpj.group(0)
    # razao social: bloco com "S.A." no quadrante superior esquerdo do emitente
    cand = [e for e in itens
            if e["y"] < 800 and e["x"] < 1100
            and re.fullmatch(r"[A-Za-zÀ-ú0-9.\- ]+", e["texto"])
            and re.search(r"S\.?\s*A\.?$|L\.?\s*T\.?\s*D\.?\s*A\b|S\.R\.L",
                          e["texto"], re.I)
            and not re.search(r"\d", e["texto"])]
    if cand:
        cand.sort(key=lambda e: e["y"])
        emitente["razao_social"] = cand[0]["texto"].strip(" .-–—,/")
    ie_em = pegar_valor(itens, ["inscricaoestadual"], r"(\d{6,12})",
                        y_min=0, y_max=900)
    emitente["inscricao_estadual"] = ie_em
    info["emitente"] = emitente
    # ---- destinatario ----
    Y_DEST = (915, 1230)
    dest = {}
    dest["nome"] = pegar_valor(
        itens, ["nomerazaosocial"], r"(.{2,60})", y_min=Y_DEST[0],
        y_max=Y_DEST[1], margem=60)
    dest["cnpj_cpf"] = pegar_valor(
        itens, ["cnpjcpf"], REG_CNPJ + r"|" + REG_CPF,
        y_min=Y_DEST[0], y_max=Y_DEST[1], margem=60)
    if not dest["cnpj_cpf"]:
        m = re.search(REG_CNPJ + r"|" + REG_CPF, compacto_total)
        dest["cnpj_cpf"] = m.group(0) if m else None
    dest["endereco"] = pegar_valor(
        itens, ["endereco"], r"(.{5,90})", y_min=Y_DEST[0],
        y_max=Y_DEST[1], margem=60)
    if not dest["endereco"]:
        m = re.search(r"endereco\s*([A-Z0-9.,À-ú\- ]{5,90})", compacto_total)
        dest["endereco"] = m.group(1) if m else None
    dest["municipio"] = pegar_valor(
        itens, ["municipio"], r"([A-Za-zÀ-ú]{3,30})", y_min=Y_DEST[0],
        y_max=Y_DEST[1])
    dest["uf"] = pegar_valor(
        itens, ["uf"], r"([A-Za-zÀ-ú]{2,6})", y_min=Y_DEST[0],
        y_max=Y_DEST[1])
    if isinstance(dest["uf"], str) and len(dest["uf"]) >= 2:
        # "MTBrasil" ou "MTB" -> "MT"
        dest["uf"] = re.sub(r"^([A-Z]{2}).*", r"\1", dest["uf"])
    dest["cep"] = pegar_valor(
        itens, ["cep"], r"(\d{2,3}[\.\-]?\d{3}[\.\-]?\d{3})",
        y_min=Y_DEST[0], y_max=Y_DEST[1])
    if not dest["cep"]:
        m = re.search(r"cep[\.:]?\s*(\d{5,8})", compacto_total)
        dest["cep"] = m.group(1) if m else None
    dest["pais"] = pegar_valor(
        itens, ["pais"], r"([A-Za-zÀ-ú]{4,15})", y_min=Y_DEST[0],
        y_max=Y_DEST[1])
    if isinstance(dest["pais"], str):
        # "MTBrasil" -> "Brasil" (estado+pais colados)
        dest["pais"] = re.sub(r"^[A-Z]{2}(?=[A-ZÀ-Ú])", "", dest["pais"])
    dest["telefono"] = pegar_valor(
        itens, ["fone/fax", "fone"], r"([(]?\d{2,5}[)]?[\d\- ]{5,18})",
        y_min=Y_DEST[0], y_max=Y_DEST[1])
    dest["inscricao_estadual"] = pegar_valor(
        itens, ["inscricaoestadual"], r"(\d{6,12})", y_min=Y_DEST[0],
        y_max=Y_DEST[1])
    info["destinatario"] = dest

    # ---- transportadora ----
    Y_TRANS = (1450, 1760)
    transp = {}
    transp["razao_social"] = pegar_valor(
        itens, ["razaosocial"], r"(.{3,60})", y_min=Y_TRANS[0],
        y_max=Y_TRANS[1], margem=60)
    transp["cnpj_cpf"] = pegar_valor(
        itens, ["cnpj"], r"(\d{2,3}\.\d{3}\.\d{3}/\d{3,6}[ -]\d{1,2})",
        y_min=Y_TRANS[0], y_max=Y_TRANS[1], margem=60)
    transp["placa"] = pegar_valor(
        itens, ["placadoveiculo", "placadovehiculo"],
        r"([A-Za-z]{2,4}[- ]?[0-9]{1,5}[- ]?[A-Za-z]{1,4}[- ]?[0-9]{0,4})",
        y_min=Y_TRANS[0], y_max=Y_TRANS[1], margem=60)
    info["transportadora"] = transp

    info["peso_bruto"] = pegar_valor(
        itens, ["pesobruto"], r"([0-9][0-9.,]{0,14})",
        y_min=1640, y_max=1760, margem=60, lado="ambos")
    info["peso_liquido"] = pegar_valor(
        itens, ["pesoliquido"], r"([0-9][0-9.,]{0,14})",
        y_min=1640, y_max=1760, margem=60, lado="ambos")

    # ---- produtos ----
    ini_y = fin_y = None
    # alguns DANFEs (ex.: calcario) rotulam a secao no plural: 'DADOS DOS
    # PRODUTOS/SERVICOS', 'DADOS DO PRODUTO/SERVICO' etc. - por isso varias
    # variantes (singular/plural) sao procuradas
    e1 = achar_rotulo(itens, [
        "dadosdoproduto", "dadosdoprodutos", "dadosdosprodutos",
        "dadosdoprodutoservicos", "dadosdoprodutosservicos",
        "dadosdosprodutoservicos", "dadosdosprodutosservicos",
        "dadosdoprodutoservicio", "dadosdosprodutoservicio",
        "datosdoproduto", "datosdoprodutos", "dadosdelproducto",
    ])
    e2 = achar_rotulo(itens, ["dadosadicionais", "datosadicionais"])
    if e1:
        ini_y = e1["y"] + 8  # excluir o titulo "DADOS DO PRODUTO" da tabela
    if e2:
        fin_y = e2["y"]
    if not e1:
        log.debug("campos: rotulo 'DADOS DO PRODUTO' nao encontrado")
    if not e2:
        log.debug("campos: rotulo 'DADOS ADICIONAIS' nao encontrado")
    produtos = []
    if ini_y and fin_y and ini_y < fin_y:
        for pag in paginas:
            produtos.extend(extrair_produtos(itens, ini_y, fin_y, pag))
        log.info("produtos: total combinado (paginas) = %d", len(produtos))
    else:
        log.warning("produtos: regiao de produtos invalida "
                    "(e1=%s e2=%s ini_y=%s fin_y=%s)",
                    bool(e1), bool(e2), ini_y, fin_y)
    info["produtos"] = produtos

    # ---- dados adicionais ----
    dados_extra = []
    if e2:
        for e in itens:
            if e["y"] > e2["y"] and e["x"] < e2["x"] + 1400:
                dados_extra.append(e["texto"])
        dados_extra.sort()
    info["dados_adicionais"] = " | ".join(dados_extra) if dados_extra else None

    # ---- texto OCR completo ----
    ent = sorted(itens, key=lambda e: (e["y"], e["x"]))
    info["texto_ocr"] = "\n".join(
        f"[p{e['pagina']} y={e['y']:.0f} x={e['x']:.0f}] {e['texto']}"
        for e in ent)

    return info
# ---------------------------------------------------------------------------
# Processamento principal
# ---------------------------------------------------------------------------

def salvar_json(datos, ruta):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ruta)


def carregar_json(ruta):
    if ruta.exists():
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"!! NAO foi possivel ler o JSON existente ({e}); sera criado do zero. ")
    return {}


# ---------------------------------------------------------------------------
# Completude da extracao (status "completa" / "parcial")
# ---------------------------------------------------------------------------

CAMPOS_NUCLEARES = [
    "numero_nota",
    "data_emissao",
    "valor_da_nota",
    "serie",
    "emitente.razao_social",
    "emitente.cnpj_cpf",
    "destinatario.nome",
    "destinatario.cnpj_cpf",
]


def _campo_aninhado(info, camino):
    """Le um campo anidado com notacion 'a.b.c'."""
    atual = info
    for parte in camino.split("."):
        if not isinstance(atual, dict):
            return None
        atual = atual.get(parte)
        if atual is None:
            return None
    return atual


def avaliar_completude(info, chave_valida):
    """Decide se a extracao foi completa ou parcial.

    Completa -> chave com DV valido E todos os campos nucleares presentes.
    Parcial  -> em qualquer outro caso (devolve tambem o motivo).
    """
    if not chave_valida:
        return "parcial", "chave de acesso com digito verificador invalido (DV)"
    faltantes = [c for c in CAMPOS_NUCLEARES
                 if _campo_aninhado(info, c) in (None, "")]
    if faltantes:
        return "parcial", "campos nucleares ausentes: " + ", ".join(faltantes)
    return "completa", None


def processar_pdf(caminho, raiz, engine, dpi):
    """Devolve (info_dict, chave, chave_valida, motivo_error)."""
    rel = str(caminho.relative_to(raiz))
    log.info("%s | inicio de extracao", rel)

    # 1) tentar texto embebido
    itens = None
    origem = "texto"
    try:
        itens = ler_paginas_texto(caminho)
    except Exception:
        pass

    # 2) se nao ha texto, OCR
    if not itens:
        origem = "ocr"
        try:
            itens = ler_paginas_ocr(caminho, engine, dpi)
        except Exception:
            itens = None

    if not itens:
        log.warning("%s | nao foi possivel extrair texto nem OCR do PDF", rel)
        return None, None, False, "nao foi possibil extrair texto nem OCR do PDF", []

    log.info("%s | origem=%s itens=%d", rel, origem, len(itens))
    chave, ok = extrair_chave_acesso(itens)
    if not chave:
        log.warning("%s | chave de acesso (44 digitos) NAO encontrada", rel)
        return None, None, False, "nao foi encontrada a chave de acesso (44 digitos)", []

    log.info("%s | chave=%s DV_valido=%s", rel, chave, ok)

    # Mesmo con DV invalido, os datos (parciales) sao extraidos em vez de
    # descartar o PDF: o campo status_extracao indica "completa" / "parcial"
    info = extrair_infos(itens, rel)
    info["origem_texto"] = origem
    # ---- normalizacion numerica BR ('.' milhar, ',' decimal) ----
    revision = normalizar_campos_numericos(info)
    status, motivo_parcial = avaliar_completude(info, ok)
    info["status_extracao"] = status
    if motivo_parcial:
        info["motivo_extracao_parcial"] = motivo_parcial
        log.warning("%s | extracao PARCIAL -> %s", rel, motivo_parcial)
    else:
        log.info("%s | extracao COMPLETA", rel)
    log.info("%s | produtos=%d status=%s", rel,
             len(info.get("produtos") or []), status)
    if not ok:
        return (info, chave, False,
                f"chave de acesso encontrada mas com digito verificador invalido: {chave}",
                revision)
    return info, chave, True, None, revision


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="Extrai chaves de acesso e informacao de notas fiscales (PDF -> JSON).")
    parser.add_argument("--raiz", default=None,
                        help="Pasta raiz a percorrer (por omissao: a do script).")
    parser.add_argument("--json", dest="json_path", default="notas_fiscales.json",
                        help="Arquivo JSON acumulador (por omissao: notas_fiscales.json).")
    parser.add_argument("--dpi", type=int, default=300,
                        help="Resolucion para OCR (por omissao: 300).")
    parser.add_argument("--so-novas", action="store_true",
                        help="NAO atualizar chaves ja existentes no JSON.")
    parser.add_argument("--sem-ocr", action="store_true",
                        help="NAO usar OCR (so PDFs com texto embebido).")
    parser.add_argument("--log", dest="log_path", default=None,
                        help="Arquivo de log de rastreio (por omissao: "
                             "rastreio_extracao.log na raiz).")
    args = parser.parse_args(argv)

    configurar_log(args.log_path)
    log.info("==== INICIO DA EXTRACAO PDF->JSON ====")

    raiz = Path(args.raiz).resolve() if args.raiz else Path(__file__).resolve().parent.parent
    json_path = Path(args.json_path)
    if not json_path.is_absolute():
        json_path = raiz / json_path

    pdfs = sorted(raiz.rglob("*.pdf"))
    print("=" * 78)
    print(" EXTRACCAO DE NOTAS FISCAIS (PDF -> JSON)")
    print(f" Raiz: {raiz}")
    print(f" JSON: {json_path}")
    print(f" PDFs encontrados: {len(pdfs)}")
    print("=" * 78)
    log.info("configuracao: raiz=%s json=%s pdfs=%d",
             raiz, json_path, len(pdfs))

    if not pdfs:
        print("NAO ha arquivos PDF na raiz nem subpastas.")
        return

    datos = carregar_json(json_path)
    engine = None
    if not args.sem_ocr:
        engine = obter_engine_ocr()

    ins = actual = sin_cambio = parc = 0
    fallos = []
    parciales = []
    revision_total = []

    for i, pdf in enumerate(pdfs, 1):
        print("-" * 78)
        print(f"[{i}/{len(pdfs)}] Processando: {pdf.relative_to(raiz)}")
        try:
            info, chave, ok, motivo, revision = processar_pdf(pdf, raiz, engine, args.dpi)
        except Exception as e:
            info = chave = ok = None
            motivo = f"erro inesperado: {e}"
            revision = []
            traceback.print_exc()
            log.error("%s | ERRO inesperado ao processar: %s", pdf.relative_to(raiz), e,
                      exc_info=True)

        if not chave:
            fallos.append((str(pdf.relative_to(raiz)), motivo))
            print(f"    >> NAO foi possivel processar -> {motivo}")
            log.error("%s | NAO processado -> %s", pdf.relative_to(raiz), motivo)
            continue

        revision_total.extend(revision)

        print(f"    >> Chave de acesso: {chave}")
        print(f"    >> Chave valida (DV): {'SI' if ok else 'NAO'}")
        status = info.get("status_extracao", "completa" if ok else "parcial")
        print(f"    >> Status extracao: {status.upper()}")
        if not ok:
            print("    >> AVISO: DV invalido; serao guardados dados PARCIAIS")

        ya_existia = chave in datos
        if ya_existia and args.so_novas:
            print("    >> A chave ja existe no JSON -> omitida (--so-novas)")
            sin_cambio += 1
            log.info("%s | chave ja existente -> omitida (--so-novas)", chave)
            continue

        info_ant = datos.get(chave)
        if info_ant == info:
            datos[chave] = info
            print("    >> OK: sem alteracao (informacao identica)")
            sin_cambio += 1
            log.info("%s | sem alteracao (informacao identica)", chave)
        elif ya_existia:
            datos[chave] = info
            print("    >> OK: chave JA EXISTENTE -> ATUALIZADA")
            actual += 1
            log.info("%s | chave atualizada", chave)
        else:
            datos[chave] = info
            print("    >> OK: chave NOVA -> INSERTADA")
            ins += 1
            log.info("%s | chave nova inserida", chave)

        if not ok:
            parc += 1
            parciales.append((str(pdf.relative_to(raiz)),
                              info.get("motivo_extracao_parcial") or motivo))

        salvar_json(datos, json_path)
        print(f"    >> JSON salvado em {json_path}")
        log.info("%s | JSON salvo em %s", pdf.relative_to(raiz), json_path)

    # ---- resumo final ----
    print()
    print("=" * 78)
    print(" RESUMO FINAL")
    print("=" * 78)
    print(f"  PDFs encontrados      : {len(pdfs)}")
    print(f"  Chaves INSERTADAS     : {ins}")
    print(f"  Chaves ACTUALIZADAS   : {actual}")
    print(f"  Sem alteracao         : {sin_cambio}")
    print(f"  Parciais (DV invalido): {parc}")
    print(f"  Falhas / omitidas     : {len(fallos)}")
    log.info("RESUMO: pdfs=%d inseridas=%d atualizadas=%d sem_alteracao=%d "
             "parciais=%d falhas=%d",
             len(pdfs), ins, actual, sin_cambio, parc, len(fallos))
    if parciales:
        print("-" * 78)
        print(" Detalhe de PDFs salvados PARCIALMENTE (status: parcial):")
        for archivo, motivo in parciales:
            print(f"  - {archivo}")
            print(f"      motivo: {motivo}")
    if fallos:
        print("-" * 78)
        print(" Detalhe de PDFs nao processados:")
        for archivo, motivo in fallos:
            print(f"  - {archivo}")
            print(f"      motivo: {motivo}")
    print("=" * 78)
    print(f"JSON final: {json_path}  ({len(datos)} chaves de acesso)")
    log.info("==== FIM DA EXTRACAO (JSON final: %s, %d chaves) ====",
             json_path, len(datos))

    # ---- reporte de normalizacion numerica ----
    if revision_total:
        reporte_path = raiz / "reporte_normalizacion.txt"
        n_anom = generar_reporte(revision_total, reporte_path)
        print("-" * 78)
        print(f" REPORTE DE NORMALIZACION NUMERICA: {reporte_path}")
        print(f"   Valores procesados : {len(revision_total)}")
        print(f"   Anomalias a revisar: {n_anom}")


if __name__ == "__main__":
    main()