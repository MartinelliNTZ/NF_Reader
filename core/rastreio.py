# -*- coding: utf-8 -*-
"""Log de rastreio do pipeline de leitura de notas fiscais.

Gera um arquivo de log (rastreio_extracao.log, na raiz do projeto) com o
historico detalhado da extracao de cada PDF:

    - deteccao da tabela de produtos (rotulos encontrados e criterio usado)
    - colunas mapeadas e produtos extraidos (codigo/descricao/valores)
    - avisos: campos ausentes, DV invalido, itens sem descricao etc.
    - erros e excecoes com traceback

Uso no codigo:
    from .rastreio import log        # dentro do pacote 'core'
    from rastreio import log         # execucao direta: python core/xxx.py

    log.info("mensagem %s", valor)
    log.warning("atenção...")
    log.error("erro...", exc_info=True)
"""
from __future__ import annotations

import logging
from pathlib import Path

_NOME_PADRAO = "rastreio_extracao.log"


def _caminho_padrao():
    """Raiz do projeto = dois niveis acima de core/rastreio.py."""
    base = Path(__file__).resolve().parent.parent
    return base / _NOME_PADRAO


def configurar_log(caminho=None, nivel=logging.DEBUG, modo="a"):
    """Configura o logger global 'nf_reader' (idempotente).

    Se um handler ja existia para o logger, ele e substituido para nao
    duplicar mensagens (ex.: novo --log fornecido no CLI).
    """
    logger = logging.getLogger("nf_reader")
    logger.propagate = False          # nao borbulhar para o root
    logger.setLevel(nivel)

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    destino = Path(caminho) if caminho else _caminho_padrao()
    destino = destino.resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(destino, mode=modo, encoding="utf-8")
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    fh.setLevel(nivel)
    logger.addHandler(fh)
    return logger


# Logger unico usado por todo o pipeline. A configuracao default (arquivo
# rastreio_extracao.log na raiz) e feita ja na importacao, para que qualquer
# modulo que o consuma (extrair_notas.py, NF_reader.py, ui_main.py) escreva
# no mesmo arquivo sem precisar de setup manual.
log = configurar_log()