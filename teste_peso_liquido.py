# -*- coding: utf-8 -*-
"""
Testes do preenchimento de 'peso_liquido' a partir do volume em LITROS.

Contexto: notas de combustivel (gasolina, diesel, ARLA) nao passam por
balanca, entao os rotulos PESO BRUTO / PESO LIQUIDO ficam vazios na fonte.
Nesses casos o volume total em litros passa a ocupar a MESMA coluna
'peso_liquido' (sem criar coluna nova de volume).

Execucao:
    python teste_peso_liquido.py
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.extrair_notas import (  # noqa: E402
    REG_PESO,
    ROTULOS_PESO_BRUTO,
    ROTULOS_PESO_LIQUIDO,
    _litros_por_itens,
    _litros_por_produtos,
    completar_volume_litros,
    extrair_infos,
    pegar_valor_peso,
)

FALHAS = []


def checar(nome, obtido, esperado, tol=1e-4):
    if isinstance(esperado, (int, float)) and isinstance(obtido, (int, float)):
        ok = abs(obtido - esperado) <= tol
    else:
        ok = obtido == esperado
    print(f"   {'OK ' if ok else 'XX '} {nome}: {obtido!r}"
          f"{'' if ok else f' (esperado {esperado!r})'}")
    if not ok:
        FALHAS.append(nome)


def test_litros_por_produtos():
    print(" TESTE _litros_por_produtos")
    # exemplo real: gasolina + 2x diesel  ->  251,6991 + 963,899 + 1.733,124
    produtos = [
        {"unidade": "LT", "quantidade": "251,6991",
         "valor_unitario": "5,35", "valor_total": "1.346,59"},
        {"unidade": "LT", "quantidade": "963,899",
         "valor_unitario": "5,15", "valor_total": "4.964,08"},
        {"unidade": "LT", "quantidade": "1.733,124",
         "valor_unitario": "6,29", "valor_total": "10.901,35"},
    ]
    checar("3 itens LT (ambiguidade '963,899' resolvida pelo total)",
           _litros_por_produtos(produtos), 251.6991 + 963.899 + 1733.124)

    checar("ambiguo '963,899' sem total -> leitura decimal",
           _litros_por_produtos([{"unidade": "LT", "quantidade": "963,899"}]),
           963.899)

    checar("produto em TON nao entra na soma",
           _litros_por_produtos([
               {"unidade": "LT", "quantidade": "1.000,00",
                "valor_unitario": "5,00", "valor_total": "5.000,00"},
               {"unidade": "TON", "quantidade": "37,0000"},
           ]), 1000.0)

    checar("sem produtos de volume -> None",
           _litros_por_produtos([{"unidade": "TON", "quantidade": "37,0"}]),
           None)
    checar("lista vazia -> None", _litros_por_produtos([]), None)


def test_litros_por_itens():
    print(" TESTE _litros_por_itens (plano B, tokens crus)")
    itens = [
        {"texto": "LT", "x": 720, "y": 1560, "pagina": 1},
        {"texto": "251,6991", "x": 800, "y": 1560, "pagina": 1},
        {"texto": "5,35", "x": 1000, "y": 1560, "pagina": 1},
        {"texto": "LT", "x": 720, "y": 1600, "pagina": 1},
        {"texto": "1.733,124", "x": 800, "y": 1600, "pagina": 1},
        {"texto": "6,29", "x": 1000, "y": 1600, "pagina": 1},
    ]
    checar("2 linhas LT -> soma das quantidades",
           _litros_por_itens(itens), 251.6991 + 1733.124)

    # 'L' solto (1 letra) NAO dispara a varredura crua (evita falso positivo)
    checar("token 'L' isolado nao conta",
           _litros_por_itens([{"texto": "L", "x": 720, "y": 1560,
                               "pagina": 1},
                              {"texto": "999,99", "x": 760, "y": 1560,
                               "pagina": 1}]), None)

    # numero sem casa decimal (codigo/CFOP) nao conta
    checar("numero inteiro a direita nao conta",
           _litros_por_itens([{"texto": "LT", "x": 720, "y": 1560,
                               "pagina": 1},
                              {"texto": "5929", "x": 760, "y": 1560,
                               "pagina": 1}]), None)


def test_completar_volume_litros():
    print(" TESTE completar_volume_litros")
    # peso vazio nao importa: o volume e calculado a partir do produto
    info = {"arquivo": "x.pdf", "peso_bruto": None, "peso_liquido": None,
            "produtos": [{"unidade": "LT", "quantidade": "251,6991",
                          "valor_unitario": "5,35",
                          "valor_total": "1.346,59"}]}
    completar_volume_litros(info, [])
    checar("volume_litros calculado", info["volume_litros"], 251.6991)
    checar("peso_liquido permanece vazio", info["peso_liquido"], None)

    # mesmo com peso_liquido presente, o volume continua sendo calculado
    info2 = {"arquivo": "y.pdf", "peso_bruto": "0",
             "peso_liquido": "37.000,000",
             "produtos": [{"unidade": "LT", "quantidade": "999,0"}]}
    completar_volume_litros(info2, [])
    checar("peso_liquido do documento e preservado",
           info2["peso_liquido"], "37.000,000")
    checar("volume_litros calculado mesmo com peso presente",
           info2["volume_litros"], 999.0)

    # lixo de OCR no peso nao bloqueia o volume (caso real dos combustiveis)
    info_garb = {"arquivo": "g.pdf", "peso_liquido": "103052747",
                 "produtos": [{"unidade": "LT", "quantidade": "251,6991",
                               "valor_unitario": "5,35",
                               "valor_total": "1.346,59"}]}
    completar_volume_litros(info_garb, [])
    checar("volume calculado mesmo com peso com lixo",
           info_garb["volume_litros"], 251.6991)

    # sem produtos de volume -> volume_litros None
    info3 = {"arquivo": "z.pdf", "peso_liquido": None,
             "produtos": [{"unidade": "TON", "quantidade": "37,0"}]}
    completar_volume_litros(info3, [])
    checar("sem litros -> volume_litros None", info3["volume_litros"], None)


def _itens_nota_combustivel():
    """DANFE minimo de combustivel (texto embutido simulado) com 1 item LT."""
    return [
        {"texto": "DADOS DO PRODUTO/SERVIÇO", "x": 90, "y": 1500, "pagina": 1},
        # cabecalho da tabela
        {"texto": "CODIGO", "x": 40, "y": 1540, "pagina": 1},
        {"texto": "DESCRICAO", "x": 120, "y": 1540, "pagina": 1},
        {"texto": "NCM", "x": 560, "y": 1540, "pagina": 1},
        {"texto": "CFOP", "x": 640, "y": 1540, "pagina": 1},
        {"texto": "UNID", "x": 720, "y": 1540, "pagina": 1},
        {"texto": "QTDE", "x": 800, "y": 1540, "pagina": 1},
        {"texto": "VLRUNIT", "x": 1000, "y": 1540, "pagina": 1},
        {"texto": "VLRTOTAL", "x": 1200, "y": 1540, "pagina": 1},
        # linha de dados
        {"texto": "1", "x": 40, "y": 1560, "pagina": 1},
        {"texto": "GASOLINA", "x": 120, "y": 1560, "pagina": 1},
        {"texto": "COMUM", "x": 240, "y": 1560, "pagina": 1},
        {"texto": "27101259", "x": 560, "y": 1560, "pagina": 1},
        {"texto": "5929", "x": 660, "y": 1560, "pagina": 1},
        {"texto": "LT", "x": 720, "y": 1560, "pagina": 1},
        {"texto": "251,6991", "x": 800, "y": 1560, "pagina": 1},
        {"texto": "5,35", "x": 1000, "y": 1560, "pagina": 1},
        {"texto": "1.346,59", "x": 1200, "y": 1560, "pagina": 1},
        {"texto": "DADOS ADICIONAIS", "x": 90, "y": 1700, "pagina": 1},
    ]


def test_integracao_extrair_infos():
    print(" TESTE integracao extrair_infos (volume_litros)")
    info = extrair_infos(_itens_nota_combustivel(), "combustivel.pdf")
    prods = info.get("produtos") or []
    und = prods[0].get("unidade") if prods else None
    checar("produto extraido com unidade LT", und, "LT")
    checar("volume_litros = volume (litros)", info.get("volume_litros"), 251.6991)
    checar("peso_liquido permanece vazio", info.get("peso_liquido"), None)


def test_pegar_valor_peso_coluna_abaixo():
    """Caso real 8649.pdf: o peso esta na fila de BAIXO, na mesma coluna do
    rotulo - a inscricao estadual da transportadora ('254783430'), que fica
    35px ACIMA e 63px a direita do rotulo, nao pode ser lida como peso."""
    print(" TESTE pegar_valor_peso (coluna de baixo - caso 8649.pdf)")
    itens = [
        {"texto": "NUMERACAO", "x": 1542, "y": 1374, "pagina": 1},
        {"texto": "254783430", "x": 2321, "y": 1360, "pagina": 1},
        {"texto": "PESOBRUTO", "x": 1992, "y": 1386, "pagina": 1},
        {"texto": "PESOLIOUIDO", "x": 2258, "y": 1395, "pagina": 1},
        {"texto": "0", "x": 1875, "y": 1424, "pagina": 1},
        {"texto": "37.000,000", "x": 2314, "y": 1436, "pagina": 1},
    ]
    checar("peso_bruto = '0' (nao '254783430')",
           pegar_valor_peso(itens, ["pesobruto"], REG_PESO, margem=60,
                            y_min=1200, y_max=1640), "0")
    checar("peso_liquido = '37.000,000' (nao '254783430')",
           pegar_valor_peso(itens, ["pesoliquido", "pesoliq"], REG_PESO,
                            margem=60, y_min=1200, y_max=1640), "37.000,000")

    # variante de OCR do rotulo (Q lido como O) na fila classica (~1660)
    itens_ocr = [
        {"texto": "PESO LIOUIDO", "x": 2271, "y": 1328, "pagina": 1},
        {"texto": "133601609", "x": 2329, "y": 1289, "pagina": 1},
        {"texto": "48.000,000", "x": 2323, "y": 1365, "pagina": 1},
    ]
    checar("rotulo 'PESO LIOUIDO' -> valor da coluna de baixo",
           pegar_valor_peso(itens_ocr, ["pesoliquido", "pesoliq"], REG_PESO,
                            margem=60, y_min=1200, y_max=1640), "48.000,000")

    # abreviatura 'PESO LIQ.' (alvo curto): casada pelo rotulo compactado
    itens_abrev = [
        {"texto": "PESO LIQ.", "x": 1992, "y": 1386, "pagina": 1},
        {"texto": "36.000,00", "x": 2050, "y": 1430, "pagina": 1},
    ]
    checar("abreviatura 'PESO LIQ.' reconhecida",
           pegar_valor_peso(itens_abrev, ["pesoliquido", "pesoliq"], REG_PESO),
           "36.000,00")

    # layout em que o valor fica AO LADO do rotulo (mesma fila): sem numero
    # na coluna de baixo, o criterio generico continua valendo
    itens_lado = [
        {"texto": "PESO LIQUIDO", "x": 1992, "y": 1386, "pagina": 1},
        {"texto": "37.000,000", "x": 2150, "y": 1382, "pagina": 1},
    ]
    checar("valor ao lado do rotulo (mesma fila)",
           pegar_valor_peso(itens_lado, ["pesoliquido", "pesoliq"], REG_PESO),
           "37.000,000")

    # sem rotulo -> None
    checar("sem rotulo -> None",
           pegar_valor_peso([{"texto": "0", "x": 1875, "y": 1424,
                              "pagina": 1}], ["pesoliquido"], REG_PESO), None)


def test_rotulos_peso_ocr():
    """Toda nomenclatura de PESO LIQUIDO que o OCR costuma produzir deve ser
    reconhecida e o valor da coluna de baixo lido (nao a inscricao estadual
    da transportadora, que fica logo acima do rotulo)."""
    print(" TESTE nomenclaturas de PESO LIQUIDO (erros tipicos de OCR)")
    variantes = [
        "PESO LIQUIDO", "PESO LÍQUIDO", "PESO LIQUID0", "PES0 LIQUIDO",
        "PES0 LIQUID0", "PESO LIOUIDO", "PESO LIQUlDO", "PÊSO LÍQUIDO",
        "PESO LIQ.", "PESO LIQ", "PESO LIQDO", "PESO LIQUD0",
        "PESO L1QUIDO", "PESO L1QUID0", "PESO LIOU1DO", "PESO LIQUIDO KG",
        "PESO LÍQUIDO (KG)", "PESO LÍQUIDO KGS", "PESO LIQUIDOKG",
        "PESO LlQUIDO",
    ]
    for rotulo in variantes:
        itens = [
            {"texto": rotulo, "x": 2258, "y": 1395, "pagina": 1},
            {"texto": "254783430", "x": 2321, "y": 1360, "pagina": 1},
            {"texto": "37.000,000", "x": 2314, "y": 1436, "pagina": 1},
        ]
        checar(f"peso_liquido com rotulo {rotulo!r}",
               pegar_valor_peso(itens, ROTULOS_PESO_LIQUIDO, REG_PESO,
                                margem=60, y_min=1200, y_max=1640),
               "37.000,000")

    # 'PESO BRUTO' nao pode casar com a lista do liquido (e vice-versa)
    itens_bruto = [
        {"texto": "PESO BRUTO", "x": 1992, "y": 1386, "pagina": 1},
        {"texto": "0", "x": 1875, "y": 1424, "pagina": 1},
    ]
    checar("'PESO BRUTO' nao casa com a lista do liquido",
           pegar_valor_peso(itens_bruto, ROTULOS_PESO_LIQUIDO, REG_PESO),
           None)
    checar("peso_bruto com rotulo 'PES0 BRUT0'",
           pegar_valor_peso([{"texto": "PES0 BRUT0", "x": 1992, "y": 1386,
                              "pagina": 1},
                             {"texto": "0", "x": 1875, "y": 1424,
                              "pagina": 1}],
                            ROTULOS_PESO_BRUTO, REG_PESO), "0")

    # rotulo partido em varios tokens pela OCR ("PESO" + "LIQUID0")
    checar("rotulo partido em 2 tokens ('PESO' + 'LIQUID0')",
           pegar_valor_peso([{"texto": "PESO", "x": 2250, "y": 1395,
                              "pagina": 1},
                             {"texto": "LIQUID0", "x": 2330, "y": 1396,
                              "pagina": 1},
                             {"texto": "48.000,000", "x": 2323, "y": 1436,
                              "pagina": 1}],
                            ROTULOS_PESO_LIQUIDO, REG_PESO,
                            margem=60, y_min=1200, y_max=1640), "48.000,000")


def main():
    print("-" * 60)
    test_litros_por_produtos()
    test_litros_por_itens()
    test_completar_volume_litros()
    test_integracao_extrair_infos()
    test_pegar_valor_peso_coluna_abaixo()
    test_rotulos_peso_ocr()
    print("-" * 60)
    if FALHAS:
        print(f" FALHAS: {len(FALHAS)} -> {FALHAS}")
        return 1
    print(" TODOS OS TESTES PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
