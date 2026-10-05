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
import difflib
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
# variante tolerante: OCR troca a virgula decimal por ponto ("11.284.80")
REG_VALOR_TOL = r"\d{1,3}(?:[.,]\d{3})*[.,]\d{2}"


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
    "nomerazaosocial", "razaosocial", "social", "destinatarioremetente",
    "cnpjcpf", "cnpicpf", "cnpi", "cnpj", "cpfcnpj", "datadeemissao", "datadeentradasaida",
    "datadeentradasaid", "dataderecebimento", "endereco", "municipio",
    "bairro", "cep", "uf", "pais", "fonefax", "fone", "inscricaoestadual",
    "inscricao", "estadual", "hora",
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
    cands = []
    for e in itens:
        if not (y_min <= e["y"] <= y_max):
            continue
        if pagina is not None and e["pagina"] != pagina:
            continue
        cands.append(e)
    for e in cands:
        ct = compacta(e["texto"])
        for alvo in alvos:
            if alvo and alvo in ct:
                return e
    # Segunda passada: rotulos partidos em varios tokens da mesma linha
    # (ex.: "PESO" + "BRUTO", "VALOR" + "TOTAL" + "DA" + "NOTA").
    return achar_rotulo_multitoken(cands, alvos)


def _juntar_linhas(cands, dy_max=16, gap_max=180):
    """Agrupa tokens da mesma linha em ordem de x (para match de rotulos)."""
    linhas = []
    for e in sorted(cands, key=lambda t: (t["pagina"], t["y"], t["x"])):
        if linhas:
            ult = linhas[-1][-1]
            if (e["pagina"] == ult["pagina"]
                    and abs(e["y"] - ult["y"]) <= dy_max
                    and e["x"] - ult["x"] <= gap_max):
                linhas[-1].append(e)
                continue
        linhas.append([e])
    return linhas


def achar_rotulo_multitoken(cands, alvos):
    """Match de rotulo unindo tokens adjacentes da mesma linha.

    'cands' sao os tokens ja filtrados (janela/pagina) e 'alvos' os rotulos
    ja compactados. Devolve o primeiro token do trecho que casou (ou None).
    Complementa achar_rotulo, que so casa rotulos inteiros em um unico token.
    """
    for linha in _juntar_linhas(cands):
        # compactos por token com posicoes no concatenado
        partes = [compacta(e["texto"]) for e in linha]
        concat = "".join(partes)
        for alvo in alvos:
            if not alvo or alvo not in concat:
                continue
            # encontra a ocorrencia e mapeia de volta ao token de origem
            pos = concat.find(alvo)
            acc = 0
            for tok, part in zip(linha, partes):
                if acc <= pos < acc + len(part) or (pos < acc <= pos + len(alvo)):
                    return tok
                acc += len(part)
    # 3) fuzzy (typos do emissor / OCR, ex.: 'ADICIOINAIS'): janela deslizante
    # com no maximo 1 erro, ainda na mesma linha de tokens
    for linha in _juntar_linhas(cands):
        partes = [compacta(e["texto"]) for e in linha]
        concat = "".join(partes)
        for alvo in alvos:
            if len(alvo) < 6:
                continue
            n = len(alvo)
            for w in (n - 1, n, n + 1):
                if w < 6 or w > len(concat):
                    continue
                for i in range(len(concat) - w + 1):
                    if concat[i] != alvo[0]:
                        continue
                    if difflib.SequenceMatcher(
                            None, concat[i:i + w], alvo).ratio() < 0.9:
                        continue
                    acc = 0
                    for tok, part in zip(linha, partes):
                        if acc <= i < acc + len(part) or (
                                i < acc <= i + n):
                            return tok
                        acc += len(part)
    return None


def _token_valor(itens, rotulo, padrao, margem=45, abre_lado=False,
                 y_min=None, y_max=None, pagina=None, lado="direita"):
    """Procura o TOKEN do valor na mesma linha (ao lado) ou na linha de baixo."""
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
        if e is rotulo:
            # o proprio rotulo nunca e o valor dele (ex.: 'NOME/RAZÃO SOCIAL')
            continue
        ct = compacta(e["texto"])
        if any(ct.startswith(r) for r in ROTULOS_EXCLUIR):
            continue
        if re.search(padrao, e["texto"]):
            return e
    return None


def valor_ao_lado(itens, rotulo, padrao, margem=45, abre_lado=False,
                  y_min=None, y_max=None, pagina=None, lado="direita"):
    """Procura o valor na mesma linha (ao lado) ou na linha de abaixo."""
    e = _token_valor(itens, rotulo, padrao, margem, abre_lado=abre_lado,
                     y_min=y_min, y_max=y_max, pagina=pagina, lado=lado)
    if not e:
        return None
    m = re.search(padrao, e["texto"])
    if m.lastindex:
        return m.group(1).strip()
    return m.group(0).strip()


def _linha_de(e, itens, dy=8, gap_max=220):
    """Texto dos tokens da MESMA linha de 'e' no segmento continuo (por x).

    Corta quando o gap horizontal entre tokens excede 'gap_max' - assim
    colunas vizinhas (ex.: CPF apos o nome) nao se misturam.
    """
    msm = [t for t in itens
           if t["pagina"] == e["pagina"] and abs(t["y"] - e["y"]) <= dy]
    msm.sort(key=lambda t: t["x"])
    segmentos, atual = [], []
    for t in msm:
        if atual and t["x"] - atual[-1]["x"] > gap_max:
            segmentos.append(atual)
            atual = []
        atual.append(t)
    if atual:
        segmentos.append(atual)
    for seg in segmentos:
        if any(t is e for t in seg):
            return " ".join(t["texto"] for t in seg).strip()
    return e["texto"].strip()


def pegar_texto_linha(itens, rotulos, padrao, margem=45, y_min=0, y_max=1e18,
                      pagina=None, lado="direita", dy=8, gap_max=220):
    """Rotulo -> token-valor -> texto da linha inteira (rotulos em N tokens).

    Usado para campos compostos por varios tokens no PDF texto-embebido
    (nome do destinatario, endereco, razao social do emitente).
    """
    rot = achar_rotulo(itens, rotulos, y_min, y_max, pagina)
    if not rot:
        return None
    anc = _token_valor(itens, rot, padrao, margem, y_min=y_min, y_max=y_max,
                       pagina=pagina, lado=lado)
    if not anc:
        return None
    txt = _linha_de(anc, itens, dy=dy, gap_max=gap_max)
    m = re.search(padrao, txt)
    if m:
        return (m.group(1) if m.lastindex else m.group(0)).strip()
    return txt or None


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


# ---------------------------------------------------------------------------
# Classificacao de tokens de produto (independente de cabecalho) e reparo de
# chave - cobre varios layouts/emissores de DANFE e erros tipicos de OCR.
# ---------------------------------------------------------------------------

UNIDADES = {
    "TON", "TONS", "TN", "KG", "KGL", "KGS", "KGSC", "UN", "UND", "UNID",
    "UNI", "PC", "PCS", "L", "LT", "M", "M2", "M3", "SC", "CX", "GL", "BAG",
    "BIGBAG", "DZ", "FD", "BB", "DOSE", "HA", "ML", "T", "MIL", "G",
}

# unidade de VOLUME (litros): quando a nota nao traz "PESO LIQUIDO" - caso
# tipico dos combustiveis (gasolina, diesel, ARLA), que nao passam por
# balanca - o volume total em litros passa a ocupar a MESMA coluna
# "peso_liquido" (sem criar uma coluna nova de volume).
UNIDADES_VOLUME = {"L", "LT", "LTS", "LTR", "LITRO", "LITROS"}
# so estas variantes contam na varredura crua de tokens do OCR: exige-se uma
# unidade com 2+ letras para nao transformar um "L" solto de OCR em volume.
_ROTULOS_VOLUME = {"LT", "LTS", "LTR", "LITRO", "LITROS"}

RX_NCM = re.compile(r"^\d{8}$")
RX_CFOP = re.compile(r"^[1-7]\d{3}$")
RX_ORIGEM = re.compile(r"^\d{1,3}/\d{1,2}$")
RX_CST = re.compile(r"^\d{2,3}$")
RX_CODIGO = re.compile(r"^[A-Za-z]{1,4}\d{2,8}$")
RX_NUM_OCR = re.compile(r"^\d[\d.,]*$")

# mapa de confusoes tipicas de OCR dentro de codigos alfanumericos
_OCR_DIG = str.maketrans({"O": "0", "o": "0", "I": "1", "i": "1", "l": "1",
                          "S": "5", "s": "5", "B": "8", "Z": "2", "G": "6"})

# marcadores que encerram a descricao do produto (lixo de OCR/secoes)
_CORTES_DESCRICAO = (
    "obs", "origem", "icms", "inscricao", "calculodo", "informacoes",
    "reservado", "dadosadicionais", "valortotal", "base", "lote",
    "b.c", "bc.", "reg.min", "regmapa", "aplicacao", "viasolo",
)


def corrigir_codigo(texto):
    """Corrige confusoes de OCR em codigos ('PO15'/'POIS' -> 'P015')."""
    if not isinstance(texto, str):
        return texto
    t = texto.strip()
    if RX_CODIGO.match(t):
        return t
    if not re.fullmatch(r"[A-Za-z0-9]{2,12}", t):
        return t
    cand = t.translate(_OCR_DIG)
    if cand != t and RX_CODIGO.match(cand):
        return cand
    return t


def quebrar_token(texto):
    """Separa um token que o OCR juntou ('31042090|020|5102TN')."""
    t = str(texto).strip()
    if not t:
        return []
    blocos = t.split("|") if "|" in t else [t]
    partes = []
    for b in blocos:
        b = b.strip()
        if not b:
            continue
        m = re.fullmatch(r"(\d{4,8})([A-Za-z]{1,5})", b)
        if m:
            partes.extend([m.group(1), m.group(2)])
            continue
        m = re.fullmatch(r"([A-Za-z]{1,5})(\d{4,8})", b)
        if m:
            partes.extend([m.group(1), m.group(2)])
            continue
        # NCM + origem/CST + CFOP + unidade colados ("310420900205102TN")
        m = re.fullmatch(r"(\d{8})(\d{1,3})(\d{4})([A-Za-z]{1,5})", b)
        if m:
            partes.extend([m.group(1), m.group(2), m.group(3), m.group(4)])
            continue
        partes.append(b)
    return partes


def campo_por_padrao(token):
    """Deduz a que coluna pertence um token (sem usar o cabecalho)."""
    t = str(token).strip()
    up = t.upper()
    if RX_NCM.match(t):
        return "ncm"
    if RX_CFOP.match(t):
        return "cfop"
    if RX_ORIGEM.match(t):
        return "origem_cst"
    if up in UNIDADES:
        return "unidade"
    if RX_CODIGO.match(t):
        return "codigo"
    if RX_CST.match(t):
        return "origem_cst"
    return None


def limpar_descricao(texto):
    """Remove lixo de OCR/secoes que o OCR anexou a descricao do produto."""
    if not isinstance(texto, str):
        return texto
    t = re.sub(r"\s+", " ", texto).strip()
    baixo = t.lower()
    corte = len(t)
    for marca in _CORTES_DESCRICAO:
        i = baixo.find(marca)
        if 0 <= i < corte:
            corte = i
    t = t[:corte].strip(" .,;:-")
    return t or None


def realinhar_produto(prod):
    """Separa celulas que o OCR juntou e preenche campos que ficaram vazios.

    Casos observados: origem_cst='6/20 5102 TON' (CFOP+unidade colados),
    unidade='5102 TON', codigo='P015 DADOSADICIONAIS'.
    """
    if not isinstance(prod, dict):
        return prod
    sobra = []

    # 1) campos de texto: mantem o token que casa com o padrao do campo
    for campo, rx in (("codigo", RX_CODIGO), ("ncm", RX_NCM),
                      ("cfop", RX_CFOP), ("origem_cst", RX_ORIGEM)):
        bruto = prod.get(campo)
        if bruto in (None, ""):
            continue
        toks = []
        for parte in quebrar_token(bruto):
            toks.extend(parte.split())
        # token escolhido no texto original (antes de corrigir confusoes OCR)
        escolhido = next((t for t in toks if rx.match(t)), None)
        valor = escolhido
        if campo == "codigo":
            valor = corrigir_codigo(escolhido) if escolhido else None
            if not (valor and RX_CODIGO.match(valor)) and toks:
                cand = corrigir_codigo(toks[0])
                if RX_CODIGO.match(cand):
                    escolhido, valor = toks[0], cand
        if valor is not None and rx.match(valor):
            prod[campo] = valor
            resto = list(toks)
            if escolhido in resto:
                resto.remove(escolhido)
            sobra.extend(resto)
        else:
            prod[campo] = None
            sobra.extend(toks)

    # 2) unidade: aceita apenas unidades conhecidas
    bruto = prod.get("unidade")
    if bruto not in (None, ""):
        toks = []
        for parte in quebrar_token(bruto):
            toks.extend(parte.split())
        bom = next((t for t in toks if t.upper() in UNIDADES), None)
        prod["unidade"] = bom.upper() if bom else None
        if bom:
            toks.remove(bom)
        sobra.extend(toks)

    # 3) redistribui a sobra para os campos que ficaram vazios
    for t in sobra:
        campo = campo_por_padrao(t)
        if campo and prod.get(campo) in (None, ""):
            prod[campo] = corrigir_codigo(t) if campo == "codigo" else t

    # 4) quantidade + valor unitario que ficaram no MESMO campo
    for campo in ("quantidade", "valor_unitario"):
        bruto = prod.get(campo)
        if not isinstance(bruto, str):
            continue
        toks = [p for p in re.split(r"\s+", bruto.strip()) if p]
        if len(toks) < 2:
            continue
        nums = [normalizar_numero(t) for t in toks]
        nums = [n for n in nums if n is not None]
        if len(nums) >= 2:
            nums.sort()
            prod["quantidade"] = nums[0]
            prod["valor_unitario"] = nums[-1]

    prod["descricao"] = limpar_descricao(prod.get("descricao"))
    return prod


def _produto_suspeito(prod):
    """True quando o OCR juntou varias celulas num unico campo do produto.

    Sinal tipico de que a extracao guiada pelo cabecalho (ou o realinhamento)
    nao conseguiu separar colunas - nesse caso a extracao por conteudo e
    preferivel.
    """
    if not isinstance(prod, dict):
        return True
    for campo in ("codigo", "quantidade", "unidade", "valor_unitario",
                  "valor_total", "aliquota", "origem_cst", "cfop",
                  "bc_icms", "valor_icms", "valor_ipi", "desconto"):
        v = prod.get(campo)
        if isinstance(v, str) and " " in v.strip():
            return True
    cod = prod.get("codigo")
    if isinstance(cod, str) and len(cod) > 15:
        return True
    und = prod.get("unidade")
    if isinstance(und, str) and len(und) > 5:
        return True
    return False


def _produto_ruim(prod):
    """True quando o produto extraido pelo cabecalho deve ceder ao fallback.

    Alem das celulas coladas (_produto_suspeito), considera 'ruim' quando a
    quantidade ou o valor total nao podem sequer ser convertidos em numero.
    """
    if _produto_suspeito(prod):
        return True
    for campo in ("quantidade", "valor_total"):
        v = prod.get(campo)
        if isinstance(v, (int, float)):
            continue
        if not isinstance(v, str) or normalizar_numero(v) is None:
            return True
    return False


def _dv_chave(base43):
    soma = 0
    peso = 2
    for d in reversed(base43):
        soma += int(d) * peso
        peso = peso + 1 if peso < 9 else 2
    dv = 11 - (soma % 11)
    return "0" if dv >= 10 else str(dv)


def _cnpjs_do_texto(itens):
    achados = []
    for e in itens:
        for m in re.finditer(REG_CNPJ, e["texto"]):
            d = re.sub(r"\D", "", m.group(0))
            if len(d) == 14 and d not in achados:
                achados.append(d)
    return achados


def _reparar_ou_reconstruir(digitos, cnpjs):
    """Recupera uma chave valida a partir de um trecho com erro de OCR.

    1) procura uma janela de 44 digitos cujo DV passe apos corrigir 1 digito;
    2) se o trecho estiver deslocado (digito a mais/a menos), remonta a chave
       usando o CNPJ do emitente, a serie e o numero que ja aparecem nela.
    """
    if not digitos:
        return None
    for i in range(len(digitos) - 43):
        janela = digitos[i:i + 44]
        for j in range(44):
            for d in "0123456789":
                if d == janela[j]:
                    continue
                cand = janela[:j] + d + janela[j + 1:]
                if not validar_chave(cand):
                    continue
                if cnpjs and cand[6:20] not in cnpjs:
                    continue
                return cand
    for cnpj in cnpjs:
        ini = 0
        while True:
            i = digitos.find(cnpj, ini)
            if i < 0:
                break
            ini = i + 1
            if i < 6:
                continue
            resto = digitos[i + 14:]
            if len(resto) < 23 or resto[:2] != "55":
                continue
            base = digitos[i - 6:i] + cnpj + resto[:23]
            if len(base) == 43 and base.isdigit():
                return base + _dv_chave(base)
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

    cnpjs = _cnpjs_do_texto(itens)

    # 1) junto ao rotulo (mesma fila e filas adjacentes)
    for e in rotulos_achados:
        vizinhos = [t for t in itens
                    if abs(t["y"] - e["y"]) <= 50 and abs(t["x"] - e["x"]) <= 850]
        dig = "".join(re.findall(r"\d", "".join(t["texto"] for t in vizinhos)))
        janela = _janela_valida(dig)
        if janela:
            return janela, True
        # 1b) DV invalido por erro de OCR: corrige 1 digito ou remonta a chave
        # usando o CNPJ do emitente (a chave contem esse CNPJ nos digitos 7..20)
        reparada = _reparar_ou_reconstruir(dig, cnpjs)
        if reparada:
            log.info("chave: recuperada por reparo de DV/CNPJ -> %s", reparada)
            return reparada, True

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
        reparada = _reparar_ou_reconstruir(dig, cnpjs)
        if reparada:
            log.info("chave: recuperada (DV invalido) -> %s", reparada)
            return reparada, True
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
    def _sem_ac(txt):
        # remove acentos/maiusculas: 'CÓDIGO' -> 'CODIGO', 'DESCRIÇÃO' -> ...
        return normaliza(txt).upper()

    def _tem_codigo(txt):
        # variantes de OCR/emissor para 'CODIGO' / 'CODPRODUTO'
        for frag in ("CODIGO", "CODPRODUTO", "CODPROD", "CODIGOPRODUTO",
                     "COD PROD", "CDIGO", "COOIGO", "CODIGOPROD",
                     "CODIGO DO PRODUTO", "CODPRODUTO/SERVICO"):
            if frag in txt:
                return True
        return False

    cab = None
    cab_crit = None
    for l in linhas:
        txt = _sem_ac(" ".join(e["texto"] for e in l))
        if _tem_codigo(txt) and "CFOP" in txt and "VLR UNIT" in txt.replace("VLRUNIT", "VLR UNIT"):
            cab, cab_crit = l, "1. CODIGO+CFOP+VLR UNIT"
            break
    if cab is None:
        for l in linhas:
            txt = _sem_ac(" ".join(e["texto"] for e in l))
            if _tem_codigo(txt) and "CFOP" in txt:
                cab, cab_crit = l, "2. CODIGO+CFOP"
                break
    if cab is None:
        # alguns emissores nao imprimem a coluna CFOP no cabecalho; outros PDFs
        # tem o cabecalho espalhado por varias linhas pelo OCR. O que nunca
        # falta e CODIGO + DESCRICAO (ou so CODIGO) na tabela de produtos.
        for l in linhas:
            txt = _sem_ac(" ".join(e["texto"] for e in l))
            if _tem_codigo(txt) and ("DESCRICAO" in txt or "DESCRIPCION" in txt):
                cab, cab_crit = l, "3. CODIGO+DESCRICAO (sem CFOP no cab)"
                break
    if cab is None:
        for l in linhas:
            txt = _sem_ac(" ".join(e["texto"] for e in l))
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
        "quant": "quantidade",
        "vlr unit": "valor_unitario", "vlrunit": "valor_unitario",
        "vlrunt": "valor_unitario", "valorunitario": "valor_unitario",
        "vr": "valor_unitario", "unit": "valor_unitario",
        "desc": "desconto", "desu": "desconto",
        "vlr total": "valor_total", "vlrtotal": "valor_total",
        "valortotal": "valor_total", "valor": "valor_total",
        "bc icms": "bc_icms", "basecalc": "bc_icms",
        "vlr icms": "valor_icms", "vlricms": "valor_icms",
        "valoricms": "valor_icms", "gcicms": "valor_icms",
        "vlr ipi": "valor_ipi", "vlripi": "valor_ipi", "vlrtpi": "valor_ipi",
        "valoripi": "valor_ipi",
        "aliquot": "aliquota", "aliouota": "aliquota", "alicuota": "aliquota",
        # variantes OCR / layouts alternativos (calcario, KCL, etc.)
        "codproduto": "codigo", "codprod": "codigo",
        "codigoproduto": "codigo", "codigodoproduto": "codigo",
        "und": "unidade", "unds": "unidade",
        "qtd": "quantidade", "qde": "quantidade", "qt": "quantidade",
        "quantidade": "quantidade",
        "descricaodoproduto": "descricao", "descricaoproduto": "descricao",
        "descricaodosprodutos": "descricao",
        "ncm sh": "ncm", "ncmsh": "ncm",
        "orig cst": "origem_cst", "origem": "origem_cst",
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

    # ---- realinhamento de celulas que o OCR juntou ----
    produtos = [realinhar_produto(p) for p in produtos]

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


# rotulos de coluna (compactos) que nunca sao valores de produto
_ROTULOS_COLUNA = (
    "codigo", "codproduto", "codprod", "descricao", "produto", "ncm", "ncmsh",
    "origem", "cst", "cfop", "unidade", "unid", "und", "quantidade", "qtde",
    "qtd", "vlr", "valor", "unit", "total", "icms", "ipi", "aliquota",
    "desconto", "base", "vtrib", "vlrunt", "vlrtotal", "quant",
    # variantes com erros tipicos de OCR em cabecalhos de tabela
    "discrica", "descr", "servh", "servic", "odprod", "oprod", "dosprodutos",
    "doproduto",
)


def _decimais(token):
    """Numero de digitos apos o ultimo separador do token OCR."""
    return len(re.split(r"[.,]", token)[-1])


def _valores_num(token):
    """Interpretacoes plausiveis de um token numerico do OCR.

    '48.000'/'48,000' pode ser 48000 (milhar) ou 48.0 (3 decimais): devolve as
    duas para que a relacao valor_total = quantidade * valor_unitario escolha
    a correta. Tokens com mais de um separador tem interpretacao unica.
    """
    vals = []
    v = normalizar_numero(token)
    if v is not None:
        vals.append(v)
    m = re.fullmatch(r"(\d{1,3})[.,](\d{3})", token)
    if m:
        alt = float(f"{m.group(1)}.{m.group(2)}")
        if alt not in vals:
            vals.append(alt)
    # ultimo grupo com 1 digito: OCR dropou o ultimo decimal ("11.342.4")
    m2 = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})*)[.,](\d)", token)
    if m2:
        alt = normalizar_numero(f"{m2.group(1)}.{m2.group(2)}0")
        if alt is not None and alt not in vals:
            vals.append(alt)
    return vals


def _candidatos_numero(valor):
    """Leituras plausiveis de um valor (token OCR cru ou numero ja normalizado).

    '963,899' e ambiguo: pode ser 963,899 (3 decimais - tipico de quantidade em
    litros) ou 963899 (milhar). Devolve as duas para que o desempate seja feito
    pela relacao quantidade * valor_unitario = valor_total.
    """
    if valor is None or isinstance(valor, bool):
        return []
    if isinstance(valor, (int, float)):
        return [float(valor)]
    if not isinstance(valor, str) or not valor.strip():
        return []
    return [float(v) for v in _valores_num(valor.strip())]


def _litros_do_produto(prod):
    """Volume (litros) de UM produto medido em unidade de volume; None se nao.

    Para quantidade ambigua ('963,899') usa valor_unitario e valor_total para
    escolher a leitura cujo produto bate com o total; sem essa pista fica com a
    menor leitura (o volume de uma carga cabe em milhares, nao em milhoes).
    """
    if not isinstance(prod, dict):
        return None
    und = prod.get("unidade")
    if not isinstance(und, str) or und.strip().upper() not in UNIDADES_VOLUME:
        return None
    cands = [c for c in _candidatos_numero(prod.get("quantidade")) if c > 0]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    unit = _candidatos_numero(prod.get("valor_unitario"))
    total = _candidatos_numero(prod.get("valor_total"))
    if unit and total and unit[0] > 0:
        return min(cands, key=lambda q: abs(q * unit[0] - total[0]))
    return min(cands)


def _litros_por_produtos(produtos):
    """Soma o volume (litros) dos produtos medidos em unidade de volume."""
    total = 0.0
    achou = False
    for prod in produtos or []:
        q = _litros_do_produto(prod)
        if q is None:
            continue
        total += q
        achou = True
    return total if achou else None


def _litros_por_itens(itens):
    """Plano B: soma volumes em litros varrendo os tokens crus do OCR.

    Usado quando a tabela de produtos nao pode ser lida (ex.: cabecalho com
    "COD." que nao casa com o criterio do cabecalho). Para cada token que e
    EXATAMENTE uma unidade de volume ('LT', 'LITROS', ...) pega o numero
    imediatamente a direita na MESMA linha (coluna QUANTIDADE do DANFE) e exige
    casa decimal - assim nao confunde com codigos/CFOP/NCM vizinhos.
    """
    if not itens:
        return None
    total = 0.0
    achou = False
    for e in itens:
        if compacta(e["texto"]).upper() not in _ROTULOS_VOLUME:
            continue
        melhor = None
        for t in itens:
            if t["pagina"] != e["pagina"] or abs(t["y"] - e["y"]) > 14:
                continue
            dx = t["x"] - e["x"]
            if not (0 < dx <= 260):
                continue
            tok = t["texto"].strip()
            if not RX_NUM_OCR.match(tok) or ("." not in tok and "," not in tok):
                continue
            if melhor is None or dx < melhor[0]:
                melhor = (dx, tok)
        if melhor is None:
            continue
        cands = [c for c in _candidatos_numero(melhor[1]) if c > 0]
        if not cands:
            continue
        total += min(cands)
        achou = True
    return total if achou else None


def completar_volume_litros(info, itens):
    """Calcula o VOLUME total em litros e guarda em 'volume_litros'.

    Independe de 'peso_liquido': notas de combustivel (gasolina, diesel,
    ARLA) nao passam por balanca - o rotulo PESO LIQUIDO fica vazio (ou com
    lixo de OCR) - mas a QUANTIDADE em litros e confiavel. O resultado fica
    numa coluna propria ('volume_litros'), sem misturar litros com peso.
    """
    litros = _litros_por_produtos(info.get("produtos"))
    if litros is None:
        litros = _litros_por_itens(itens)
    info["volume_litros"] = round(litros, 4) if litros else None
    if litros:
        log.info("volume total em litros: %.4f", litros)


def _produto_de_pares(pares):
    """Monta um produto a partir de tokens (x, texto) sem usar cabecalho."""
    prod = {}
    numeros = []
    letras = []
    for x, txt in sorted(pares):
        for part in quebrar_token(txt):
            campo = campo_por_padrao(part)
            if campo and prod.get(campo) in (None, ""):
                prod[campo] = (corrigir_codigo(part)
                               if campo == "codigo" else part)
                continue
            if (RX_NUM_OCR.match(part) and "." not in part and "," not in part
                    and len(part) >= 6 and prod.get("codigo") in (None, "")):
                prod["codigo"] = part
                continue
            # aceita tambem tokens com ate 2 letras OCR coladas na cauda
            # ("11.342.4d", "47,2600s"), mas so se parecerem valor monetario
            mnum = re.fullmatch(r"(\d[\d.,]*)[A-Za-z]{0,2}", part)
            numerico = bool(mnum) and (
                "." in mnum.group(1) or "," in mnum.group(1)
                or len(mnum.group(1)) <= 6)
            vals = _valores_num(mnum.group(1)) if numerico else []
            if vals:
                numeros.append((x, part, vals))
            elif any(c.isalpha() for c in part):
                letras.append((x, part))
    if letras:
        prod["descricao"] = " ".join(t for _, t in sorted(letras))
    _atribuir_numeros(prod, numeros)
    return prod


def _atribuir_numeros(prod, numeros):
    """Atribui quantidade/valor unitario/total usando t ~= quantidade * unit.

    'numeros' e uma lista de (x, token, [valores_plausiveis]); a relacao
    valor_total = quantidade * valor_unitario desempata interpretacoes
    ambiguas do OCR (ex.: '48.000' -> 48.0 e nao 48000).
    """
    cand = []
    for idx, (_x, tok, vals) in enumerate(numeros):
        for v in vals:
            cand.append((idx, v, _decimais(tok)))
    n = len(cand)
    melhor = None
    for a in range(n):
        ia, q, da = cand[a]
        if q <= 0:
            continue
        for b in range(n):
            ib, u, db = cand[b]
            if ib == ia or u <= 0:
                continue
            alvo = q * u
            for c in range(n):
                ic, v, dc = cand[c]
                if ic in (ia, ib) or v <= 0:
                    continue
                if abs(v - alvo) <= max(0.02, alvo * 0.002):
                    score = da + db + dc
                    if melhor is None or score > melhor[0]:
                        melhor = (score, a, b, c)
    if melhor:
        _, a, b, c = melhor
        prod["quantidade"] = cand[a][1]
        prod["valor_unitario"] = cand[b][1]
        prod["valor_total"] = cand[c][1]
        usados = {cand[a][0], cand[b][0], cand[c][0]}
    else:
        usados = set()
        simples = sorted((num[2][0], i) for i, num in enumerate(numeros)
                         if num[2])
        if simples:
            q, i0 = simples[0]
            prod["quantidade"] = q
            usados.add(i0)
            if len(simples) >= 2:
                v, il = simples[-1]
                prod["valor_total"] = v
                usados.add(il)
            if len(simples) >= 3:
                u, im = simples[1]
                prod["valor_unitario"] = u
                usados.add(im)
    resto = [num[2][0] for i, num in enumerate(numeros)
             if i not in usados and num[2]]
    for campo, v in zip(("bc_icms", "valor_icms", "valor_ipi"), resto):
        if prod.get(campo) in (None, ""):
            prod[campo] = v
    return prod


def extrair_produtos_conteudo(itens, y_ini, y_fim, pagina):
    """Fallback: extrai produtos SEM depender do cabecalho da tabela.

    Usado quando o cabecalho da tabela fica ilegivel no OCR (ex.: DANFEs do
    emissor ADM/KCL). Classifica cada token por padrao e usa a relacao
    valor_total ~= quantidade * valor_unitario para desambiguar os numeros.
    """
    regiao = [e for e in itens
              if y_ini <= e["y"] <= y_fim and e["pagina"] == pagina]
    if not regiao:
        return []
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

    produtos = []
    for l in linhas:
        pares = []
        textos = []
        for e in l:
            ct = compacta(e["texto"])
            if not ct:
                continue
            if not any(c.isdigit() for c in ct) and \
                    any(k in ct for k in _ROTULOS_COLUNA):
                continue  # rotulo de coluna
            pares.append((e["x"], e["texto"]))
            textos.append(e["texto"])
        if not pares:
            continue
        n_nums = sum(1 for _, t in pares
                     if re.search(r"\d", t) and re.search(r"[.,]\d", t))
        tem_ncm = any(RX_NCM.match(p) for _, t in pares
                      for p in quebrar_token(t))
        if n_nums >= 2 or tem_ncm:
            produtos.append(_produto_de_pares(pares))
        elif produtos:
            extra = " ".join(t for t in textos if any(c.isalpha() for c in t))
            if extra:
                atual = produtos[-1]
                atual["descricao"] = ((atual.get("descricao") or "") + " "
                                      + extra).strip()
    for p in produtos:
        realinhar_produto(p)
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

    # A serie esta embutida na chave de acesso (posicoes 23..25). Quando a
    # chave passa no DV, essa e a fonte mais confiavel; caso contrario usa-se
    # o rotulo 'SERIE' do OCR (que sofre com confusoes 'No'/'CONSUL').
    serie = None
    if chave_valida and len(chave) == 44 and chave.isdigit():
        # confia na serie da chave so se o CNPJ embutido (digitos 7..20) for o
        # do emitente: chaves "validas por acidente" (OCR deslocado) dao serie
        # errada em 13016.pdf, 2199.pdf etc.
        if chave[6:20] in _cnpjs_do_texto(itens):
            serie = str(int(chave[22:25]))
    if not serie:
        serie = pegar_valor(itens, ["serie"], r"(\d{1,4})", y_max=900)
        if not serie:
            for e in itens:
                ct = compacta(e["texto"])
                if ct.startswith("serie"):
                    m = re.search(r"(\d{1,4})", e["texto"])
                    if m:
                        serie = m.group(1)
                        break
    if isinstance(serie, str):
        # valores so com letras ('No'/'CONSUL') nao sao serie: mantem digitos
        if serie.isdigit():
            serie = str(int(serie))
        else:
            m = re.search(r"(\d+)", serie)
            serie = str(int(m.group(1))) if m else None
    info["serie"] = serie

    info["data_emissao"] = pegar_valor(itens, ["datadeemissao", "datadeemission"],
                                       REG_DATA)
    info["data_entrada_saida"] = pegar_valor(
        itens, ["datadeentradasaid", "datadeentradasaida", "datadeentrada"],
        REG_DATA)
    info["hora_entrada_saida"] = pegar_valor(
        itens, ["horadeentrada/said", "horadeentrada"], r"\d{2}:\d{2}")
    info["valor_da_nota"] = pegar_valor(
        itens, ["valordanota", "valordalanota", "valortotaldanota"],
        REG_VALOR)
    if not info["valor_da_nota"]:
        # OCR trocou a virgula decimal por ponto ("11.284.80"): padrao
        # tolerante sobre os mesmos rotulos (sem 'produtos', senao o rotulo
        # 'VALOR TOTAL DOS PRODUTOS' roubaria o match do 'VALOR TOTAL DA NOTA')
        info["valor_da_nota"] = pegar_valor(
            itens, ["valordanota", "valordalanota", "valortotaldanota"],
            REG_VALOR_TOL)
    if not info["valor_da_nota"]:
        # ultimo recurso: total dos produtos
        info["valor_da_nota"] = pegar_valor(
            itens, ["valortotaldosprodutos"], REG_VALOR_TOL)
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
        # o emitente fica ABAIXO da canhoto (que tambem pode ter um 'LTDA'
        # de destinatario) e acima do bloco do destinatario (y>800): o
        # candidato de maior y no topo da pagina e o do emitente
        cand.sort(key=lambda e: e["y"], reverse=True)
        # une os tokens da mesma linha ('CALCARIO' 'OURO' 'BRANCO' 'LTDA')
        linha_txt = _linha_de(cand[0], itens)
        if not re.search(r"\d", linha_txt) and \
                re.search(r"S\.?\s*A\.?$|L\.?\s*T\.?\s*D\.?\s*A\b|S\.R\.L",
                          linha_txt, re.I):
            emitente["razao_social"] = linha_txt.strip(" .-–—,/")
        else:
            emitente["razao_social"] = cand[0]["texto"].strip(" .-–—,/")
    ie_em = pegar_valor(itens, ["inscricaoestadual"], r"(\d{6,12})",
                        y_min=0, y_max=900)
    emitente["inscricao_estadual"] = ie_em
    info["emitente"] = emitente
    # ---- destinatario ----
    # A altura do bloco do destinatario varia com o layout (MAP ~780-950,
    # ADM/KCL ~650-900, CALCARIO ~490-800). O cabecalho 'DESTINATARIO/
    # REMETENTE' existe em todos; ancorar a janela nele evita capturar o
    # bloco da transportadora (que fica logo abaixo) nas janelas fixas.
    # Depois tentam-se as janelas classicas (915..1230) e a ampliada
    # (780..1230, DANFEs de 2024) como plano B.
    Y_JANELAS = []
    rot_dest = achar_rotulo(itens, ["destinatarioremetente", "destinatario"])
    if rot_dest:
        Y_JANELAS.append((rot_dest["y"] - 5, rot_dest["y"] + 420))
    Y_JANELAS.extend(((915, 1230), (780, 1230)))
    dest = {}

    def _em_dest(chamar, *args, **kwargs):
        for y0, y1 in Y_JANELAS:
            v = chamar(*args, **kwargs, y_min=y0, y_max=y1)
            if v:
                return v
        return None

    dest["nome"] = _em_dest(
        pegar_texto_linha, itens, ["nomerazaosocial"], r"(.{2,60})",
        margem=60)
    if not dest["nome"]:
        dest["nome"] = _em_dest(
            pegar_valor, itens, ["nomerazaosocial"], r"(.{2,60})",
            margem=60)
    if not dest["nome"] and rot_dest:
        # OCR pode destruir o rotulo 'NOME/RAZAO SOCIAL' ('NOMERALAO SCCIAL');
        # o nome do destinatario e o primeiro texto da coluna da esquerda
        # logo abaixo do cabecalho do bloco.
        for e in sorted(itens, key=lambda t: (t["y"], t["x"])):
            if not (rot_dest["y"] + 5 < e["y"] <= rot_dest["y"] + 140):
                continue
            if e["x"] > 900:
                continue
            ct = compacta(e["texto"])
            if len(ct) < 3 or not any(c.isalpha() for c in ct):
                continue
            if any(ct.startswith(r) for r in ROTULOS_EXCLUIR):
                continue
            if any(k in ct for k in ("nome", "endereco", "municipio", "cep",
                                     "fone", "inscricao", "natureza", "cnp")):
                continue
            dest["nome"] = e["texto"].strip()
            break
    dest["cnpj_cpf"] = _em_dest(
        pegar_valor, itens,
        ["cnpjcpf", "cnpicpf", "cnpjicpf", "cnpi", "cpfcnpj"],
        REG_CNPJ + r"|" + REG_CPF,
        margem=60)
    if not dest["cnpj_cpf"]:
        # OCR pode destruir o rotulo 'CNPJ/CPF': usa o primeiro CNPJ/CPF da
        # janela do destinatario (o do emitente fica acima do cabecalho).
        y0, y1 = Y_JANELAS[0]
        for e in sorted(itens, key=lambda t: (t["y"], t["x"])):
            if not (y0 <= e["y"] <= y1):
                continue
            m = re.search(REG_CNPJ + r"|" + REG_CPF, e["texto"])
            if m:
                dest["cnpj_cpf"] = m.group(0)
                break
    if not dest["cnpj_cpf"]:
        m = re.search(REG_CNPJ + r"|" + REG_CPF, compacto_total)
        dest["cnpj_cpf"] = m.group(0) if m else None
    dest["endereco"] = _em_dest(
        pegar_texto_linha, itens, ["endereco"], r"(.{5,90})", margem=60)
    if not dest["endereco"]:
        m = re.search(r"endereco\s*([A-Z0-9.,À-ú\- ]{5,90})", compacto_total)
        dest["endereco"] = m.group(1) if m else None
    # Guardas anti-lixo OCR: endereco nunca e so digitos (CPF/data
    # capturados por engano quando o bloco destinatario vem falhado)
    # nem mera repeticao do nome do destinatario.
    if dest["endereco"]:
        _so_dig = "".join(c for c in dest["endereco"] if c.isdigit())
        _letras = "".join(c for c in dest["endereco"] if c.isalpha())
        if not _letras and len(_so_dig) >= 6:
            dest["endereco"] = None
        elif (dest.get("nome") and dest["endereco"]
                and compacta(dest["endereco"]) == compacta(dest["nome"])):
            dest["endereco"] = None
    dest["municipio"] = _em_dest(
        pegar_valor, itens, ["municipio"], r"([A-Za-zÀ-ú]{3,30})")
    dest["uf"] = _em_dest(
        pegar_valor, itens, ["uf"], r"([A-Za-zÀ-ú]{2,6})")
    if isinstance(dest["uf"], str) and len(dest["uf"]) >= 2:
        # "MTBrasil" ou "MTB" -> "MT"
        dest["uf"] = re.sub(r"^([A-Z]{2}).*", r"\1", dest["uf"])
    dest["cep"] = _em_dest(
        pegar_valor, itens, ["cep"], r"(\d{2,3}[\.\-]?\d{3}[\.\-]?\d{3})")
    if not dest["cep"]:
        m = re.search(r"cep[\.:]?\s*(\d{5,8})", compacto_total)
        dest["cep"] = m.group(1) if m else None
    dest["pais"] = _em_dest(
        pegar_valor, itens, ["pais"], r"([A-Za-zÀ-ú]{4,15})")
    if isinstance(dest["pais"], str):
        # "MTBrasil" -> "Brasil" (estado+pais colados)
        dest["pais"] = re.sub(r"^[A-Z]{2}(?=[A-ZÀ-Ú])", "", dest["pais"])
    dest["telefono"] = _em_dest(
        pegar_valor, itens, ["fone/fax", "fone"],
        r"([(]?\d{2,5}[)]?[\d\- ]{5,18})")
    dest["inscricao_estadual"] = _em_dest(
        pegar_valor, itens, ["inscricaoestadual"], r"(\d{6,12})")
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

    # janela ampla (labels a ~1780-1802 em alguns DANFEs); fallback sem
    # janela para layouts com a caixa de volumes em outra altura
    info["peso_bruto"] = pegar_valor(
        itens, ["pesobruto"], r"([0-9][0-9.,]{0,14})",
        y_min=1640, y_max=1900, margem=60, lado="ambos")
    info["peso_liquido"] = pegar_valor(
        itens, ["pesoliquido"], r"([0-9][0-9.,]{0,14})",
        y_min=1640, y_max=1900, margem=60, lado="ambos")
    if not info["peso_bruto"]:
        info["peso_bruto"] = pegar_valor(
            itens, ["pesobruto"], r"([0-9][0-9.,]{0,14})",
            margem=60, lado="ambos")
    if not info["peso_liquido"]:
        info["peso_liquido"] = pegar_valor(
            itens, ["pesoliquido"], r"([0-9][0-9.,]{0,14})",
            margem=60, lado="ambos")

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
    regiao_ok = bool(ini_y and fin_y and ini_y < fin_y)
    if regiao_ok:
        for pag in paginas:
            produtos.extend(extrair_produtos(itens, ini_y, fin_y, pag))
        log.info("produtos: total combinado (paginas) = %d", len(produtos))
    else:
        log.warning("produtos: regiao de produtos invalida "
                    "(e1=%s e2=%s ini_y=%s fin_y=%s)",
                    bool(e1), bool(e2), ini_y, fin_y)
    # Plano B: cabecalho da tabela ilegivel no OCR (ex.: DANFEs ADM/KCL) ou
    # celulas que continuaram coladas apos o realinhamento -> extrai por
    # conteudo, classificando cada token pelo padrao (nao usa o cabecalho).
    if not produtos or any(_produto_ruim(p) for p in produtos):
        lo = ini_y if regiao_ok else 0.0
        hi = fin_y if regiao_ok else 1e18
        alt = []
        for pag in paginas:
            alt.extend(extrair_produtos_conteudo(itens, lo, hi, pag))
        if alt and not any(_produto_suspeito(p) for p in alt):
            log.info("produtos: extracao por conteudo (fallback) = %d item(ns)",
                     len(alt))
            produtos = alt
    info["produtos"] = produtos

    # volume total em litros (combustiveis nao tem balanca; litros != peso)
    completar_volume_litros(info, itens)

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
    # Fallback: quando o campo 'valor da nota' sai ilegivel no OCR mas os
    # totais dos produtos foram lidos, usa a soma (notas com 1 item).
    if not info.get("valor_da_nota"):
        totais = [p.get("valor_total") for p in (info.get("produtos") or [])
                  if isinstance(p.get("valor_total"), (int, float))]
        if len(totais) == 1:
            info["valor_da_nota"] = totais[0]
            log.info("%s | valor_da_nota recuperado do total do produto", rel)
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