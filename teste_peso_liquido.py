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
    _litros_por_itens,
    _litros_por_produtos,
    completar_volume_litros,
    extrair_infos,
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


def main():
    print("-" * 60)
    test_litros_por_produtos()
    test_litros_por_itens()
    test_completar_volume_litros()
    test_integracao_extrair_infos()
    print("-" * 60)
    if FALHAS:
        print(f" FALHAS: {len(FALHAS)} -> {FALHAS}")
        return 1
    print(" TODOS OS TESTES PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
