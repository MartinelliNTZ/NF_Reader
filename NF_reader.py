# -*- coding: utf-8 -*-
"""
Leitor de Notas Fiscais - arquivo principal/orquestrador.

Responsabilidade deste módulo:
    - iniciar a aplicação;
    - conectar a UI ao processamento;
    - validar pasta e configurações;
    - coordenar QThread;
    - chamar o pipeline existente em `core/`;
    - consolidar resultados;
    - entregar os dados prontos para a UI exibir.

A interface visual está em:
    core/ui_principal.py

Execução:
    python leitor_notas_fiscais.py
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication

# =============================================================================
# Configuração do projeto
# =============================================================================

DIRETORIO_BASE = Path(__file__).resolve().parent

if str(DIRETORIO_BASE) not in sys.path:
    sys.path.insert(0, str(DIRETORIO_BASE))


# =============================================================================
# Componentes do projeto
# =============================================================================

from core.ui_main import (
    JanelaPrincipal,
    aplicar_tema_global,
)

from core.extrair_notas import (
    carregar_json,
    generar_reporte as gerar_relatorio,
    ler_paginas_texto,
    obter_engine_ocr as obter_motor_ocr,
    processar_pdf,
    salvar_json,
)

from core.rastreio import (
    configurar_log,
    log as rastreio_log,
)

from core.generar_csv import (
    construir_filas as construir_linhas,
    guardar_csv as salvar_csv,
)


NOME_ARQUIVO_JSON = "notas_fiscais.json"
NOME_ARQUIVO_CSV = "notas_fiscais.csv"
NOME_ARQUIVO_RELATORIO = "relatorio_normalizacao.txt"


# =============================================================================
# Regras auxiliares de domínio
# =============================================================================

def valor_esta_vazio(valor) -> bool:
    """Retorna True quando um valor não contém informação útil."""
    if valor is None:
        return True

    if isinstance(valor, str):
        return valor.strip() == ""

    if isinstance(valor, (list, dict)):
        return len(valor) == 0

    return False


def mesclar_listas(
    nova_lista,
    lista_existente,
):
    """Mescla listas preservando valores existentes."""
    resultado = list(lista_existente)

    for indice, novo_valor in enumerate(nova_lista):
        if indice < len(resultado):
            valor_existente = resultado[indice]

            if isinstance(novo_valor, dict) and isinstance(valor_existente, dict):
                resultado[indice] = mesclar_dados(
                    novo_valor,
                    valor_existente,
                )

            elif valor_esta_vazio(valor_existente) and not valor_esta_vazio(novo_valor):
                resultado[indice] = novo_valor

        else:
            resultado.append(novo_valor)

    return resultado


def mesclar_dados(
    novos_dados,
    dados_existentes,
):
    """
    Mescla os novos dados sem sobrescrever valores já preenchidos.
    """
    if not isinstance(dados_existentes, dict):
        if valor_esta_vazio(dados_existentes):
            if isinstance(novos_dados, dict):
                return dict(novos_dados)

            return novos_dados

        return dados_existentes

    if not isinstance(novos_dados, dict):
        return dados_existentes

    resultado = dict(dados_existentes)

    for chave, novo_valor in novos_dados.items():
        if chave not in resultado:
            resultado[chave] = novo_valor
            continue

        valor_existente = resultado[chave]

        if isinstance(novo_valor, dict) and isinstance(valor_existente, dict):
            resultado[chave] = mesclar_dados(
                novo_valor,
                valor_existente,
            )

        elif isinstance(novo_valor, list) and isinstance(valor_existente, list):
            resultado[chave] = mesclar_listas(
                novo_valor,
                valor_existente,
            )

        elif valor_esta_vazio(valor_existente) and not valor_esta_vazio(novo_valor):
            resultado[chave] = novo_valor

    return resultado


# =============================================================================
# Utilitários do orquestrador
# =============================================================================

def formatar_tamanho_arquivo(
    caminho: Path,
) -> str:
    if not caminho.exists():
        return "—"

    tamanho = caminho.stat().st_size

    if tamanho < 1024:
        return f"{tamanho} B"

    if tamanho < 1024 * 1024:
        return f"{tamanho / 1024:.0f} kB"

    return f"{tamanho / (1024 * 1024):.1f} MB"


def abrir_caminho(
    caminho: Path,
) -> None:
    """Abre arquivo/pasta no aplicativo padrão do sistema operacional."""
    caminho = caminho.resolve()

    if not caminho.exists():
        raise FileNotFoundError(
            f"O caminho não existe: {caminho}"
        )

    if sys.platform.startswith("win"):
        os.startfile(str(caminho))

    elif sys.platform == "darwin":
        subprocess.Popen(
            ["open", str(caminho)]
        )

    else:
        subprocess.Popen(
            ["xdg-open", str(caminho)]
        )


# =============================================================================
# Processamento
# =============================================================================

CallbackProgresso = Optional[
    Callable[[int, str], None]
]
CallbackStatus = Optional[
    Callable[[str], None]
]
CallbackAviso = Optional[
    Callable[[str], None]
]


def processar_pasta(
    pasta_raiz,
    usar_ocr=True,
    incluir_texto_ocr_csv=False,
    dpi=300,
    apenas_adicionar=True,
    campos_csv=None,
    callback_progresso: CallbackProgresso = None,
    callback_status: CallbackStatus = None,
    callback_aviso: CallbackAviso = None,
) -> dict:
    """
    Executa o pipeline de processamento.

    Esta função não conhece a interface gráfica. Ela recebe callbacks genéricos
    que o worker converte em sinais Qt.
    """
    pasta_raiz = Path(
        pasta_raiz
    ).resolve()

    caminho_json = (
        pasta_raiz / NOME_ARQUIVO_JSON
    )
    caminho_csv = (
        pasta_raiz / NOME_ARQUIVO_CSV
    )
    caminho_relatorio = (
        pasta_raiz / NOME_ARQUIVO_RELATORIO
    )

    arquivos_pdf = sorted(
        pasta_raiz.rglob("*.pdf")
    )

    rastreio_log.info("==== PIPELINE NF_reader | pasta=%s", pasta_raiz)
    rastreio_log.info("pipeline | PDFs=%d OCR=%s incluir_ocr_csv=%s dpi=%d apenas_adicionar=%s",
                      len(arquivos_pdf), usar_ocr, incluir_texto_ocr_csv, dpi,
                      apenas_adicionar)
    rastreio_log.info("pipeline | filtro de campos (%s): %s",
                      "todos" if campos_csv is None else len(campos_csv),
                      "todos" if campos_csv is None else list(campos_csv))

    if not arquivos_pdf:
        rastreio_log.warning("pipeline | nenhum PDF encontrado em %s", pasta_raiz)
        return {
            "erro": (
                "Nenhum arquivo PDF foi encontrado "
                "na pasta informada."
            )
        }

    if callback_status:
        callback_status(
            "Carregando dados existentes..."
        )

    dados = carregar_json(
        caminho_json
    )

    if not isinstance(dados, dict):
        dados = {}

    quantidade_antes = len(dados)
    rastreio_log.info("pipeline | JSON existente: %s (chaves=%d)",
                      caminho_json, quantidade_antes)

    # -------------------------------------------------------------------------
    # OCR
    # -------------------------------------------------------------------------

    motor_ocr = None

    if usar_ocr:
        if callback_status:
            callback_status(
                "Inicializando o mecanismo de OCR (RapidOCR)..."
            )

        try:
            motor_ocr = obter_motor_ocr()
            rastreio_log.info("pipeline | motor OCR inicializado")

            if callback_status:
                callback_status(
                    "Mecanismo de OCR inicializado."
                )

        except Exception as erro:  # noqa: BLE001
            motor_ocr = None
            rastreio_log.error("pipeline | falha ao iniciar RapidOCR: %s", erro)

            mensagem = (
                f"Não foi possível iniciar o RapidOCR: {erro}. "
                "PDFs sem texto incorporado não poderão ser processados."
            )

            if callback_aviso:
                callback_aviso(mensagem)

    quantidade_total_pdfs = len(
        arquivos_pdf
    )

    horario_inicio = time.time()

    contadores = {
        "inseridas": 0,
        "atualizadas": 0,
        "parciais": 0,
        "falhas": 0,
    }

    detalhes_falhas = []
    registros_para_revisao = []

    # -------------------------------------------------------------------------
    # PDFs
    # -------------------------------------------------------------------------

    for indice, caminho_pdf in enumerate(
        arquivos_pdf,
        start=1,
    ):
        caminho_relativo = caminho_pdf.relative_to(
            pasta_raiz
        )

        percentual = int(
            ((indice - 1) / quantidade_total_pdfs) * 90
        )

        texto_progresso = (
            f"[{indice}/{quantidade_total_pdfs}] "
            f"Processando {caminho_pdf.name}"
        )

        rastreio_log.info("pipeline | [%d/%d] processando %s",
                          indice, quantidade_total_pdfs, caminho_relativo)

        if callback_progresso:
            callback_progresso(
                percentual,
                texto_progresso,
            )

        if callback_status:
            callback_status(
                texto_progresso
            )

        # PDF sem OCR disponível
        if motor_ocr is None:
            try:
                possui_texto = (
                    ler_paginas_texto(
                        caminho_pdf
                    )
                    is not None
                )

            except Exception:  # noqa: BLE001
                possui_texto = False

            if not possui_texto:
                contadores["falhas"] += 1
                rastreio_log.warning(
                    "pipeline | %s | PDF sem texto incorporado e sem OCR", 
                    caminho_relativo)
                detalhes_falhas.append(
                    {
                        "arquivo": str(
                            caminho_relativo
                        ),
                        "motivo": (
                            "PDF sem texto incorporado e "
                            "mecanismo de OCR indisponível."
                        ),
                    }
                )

                continue

        # Extração
        try:
            (
                informacoes_nota,
                chave_acesso,
                extracao_completa,
                motivo,
                revisao,
            ) = processar_pdf(
                caminho_pdf,
                pasta_raiz,
                motor_ocr,
                dpi,
            )

        except Exception as erro:  # noqa: BLE001
            informacoes_nota = None
            chave_acesso = None
            extracao_completa = None
            motivo = (
                f"Erro inesperado: {erro}"
            )
            revisao = []

            traceback.print_exc()
            rastreio_log.error(
                "pipeline | %s | erro inesperado: %s",
                caminho_relativo, erro,
                exc_info=True)

        if not chave_acesso:
            contadores["falhas"] += 1
            rastreio_log.error(
                "pipeline | %s | NAO processado -> %s",
                caminho_relativo, motivo)

            detalhes_falhas.append(
                {
                    "arquivo": str(
                        caminho_relativo
                    ),
                    "motivo": motivo,
                }
            )

            continue

        registros_para_revisao.extend(
            revisao
        )

        # Consolidação
        if chave_acesso in dados:
            if apenas_adicionar:
                dados[chave_acesso] = mesclar_dados(
                    informacoes_nota,
                    dados[chave_acesso],
                )
            else:
                dados[chave_acesso] = (
                    informacoes_nota
                )

            contadores["atualizadas"] += 1
            rastreio_log.info("pipeline | %s | chave %s ATUALIZADA",
                              caminho_relativo, chave_acesso)

        else:
            dados[chave_acesso] = (
                informacoes_nota
            )
            contadores["inseridas"] += 1
            rastreio_log.info("pipeline | %s | chave %s INSERTADA",
                              caminho_relativo, chave_acesso)

        if not extracao_completa:
            contadores["parciais"] += 1
            rastreio_log.warning("pipeline | %s | extracao PARCIAL (%s)",
                                 caminho_relativo,
                                 informacoes_nota.get("motivo_extracao_parcial")
                                 if informacoes_nota else motivo)

    # -------------------------------------------------------------------------
    # Saídas
    # -------------------------------------------------------------------------

    if callback_progresso:
        callback_progresso(
            92,
            "Salvando JSON...",
        )

    salvar_json(
        dados,
        caminho_json,
    )

    quantidade_depois = len(
        dados
    )

    if callback_progresso:
        callback_progresso(
            95,
            "Gerando CSV...",
        )

    cabecalhos, linhas = construir_linhas(
        dados,
        incluir_ocr=incluir_texto_ocr_csv,
        colunas=campos_csv,
    )

    salvar_csv(
        caminho_csv,
        cabecalhos,
        linhas,
    )

    quantidade_anomalias = None

    if registros_para_revisao:
        if callback_progresso:
            callback_progresso(
                98,
                "Gerando relatório de normalização...",
            )

        quantidade_anomalias = gerar_relatorio(
            registros_para_revisao,
            caminho_relatorio,
        )

    duracao_segundos = (
        time.time() - horario_inicio
    )

    rastreio_log.info(
        "pipeline | RESULTADO: pdfs=%d inseridas=%d atualizadas=%d "
        "parciais=%d falhas=%d antes=%d depois=%d duracao=%.1fs",
        quantidade_total_pdfs, contadores["inseridas"],
        contadores["atualizadas"], contadores["parciais"],
        contadores["falhas"], quantidade_antes,
        quantidade_depois, duracao_segundos)
    if detalhes_falhas:
        for d in detalhes_falhas:
            rastreio_log.error("pipeline | FALHA %s -> %s",
                               d.get("arquivo"), d.get("motivo"))
    rastreio_log.info("pipeline | JSON=%s CSV=%s", caminho_json, caminho_csv)

    if callback_progresso:
        callback_progresso(
            100,
            "Processamento concluído.",
        )

    if callback_status:
        callback_status(
            "Processamento concluído."
        )

    return {
        "pasta_raiz": str(pasta_raiz),
        "caminho_json": str(caminho_json),
        "caminho_csv": str(caminho_csv),
        "caminho_relatorio": str(
            caminho_relatorio
        ),
        "quantidade_pdfs": (
            quantidade_total_pdfs
        ),
        "inseridas": contadores["inseridas"],
        "atualizadas": (
            contadores["atualizadas"]
        ),
        "parciais": contadores["parciais"],
        "falhas": contadores["falhas"],
        "detalhes_falhas": detalhes_falhas,
        "quantidade_antes": (
            quantidade_antes
        ),
        "quantidade_depois": (
            quantidade_depois
        ),
        "linhas_csv": len(linhas),
        "colunas_csv": len(cabecalhos),
        "anomalias": quantidade_anomalias,
        "duracao_segundos": duracao_segundos,
    }


# =============================================================================
# Worker
# =============================================================================

class TrabalhadorProcessamento(QObject):
    """
    Adaptador entre o processamento e QThread.

    Sua função é executar `processar_pasta` fora da thread da interface.
    """

    progresso = Signal(int, str)
    status = Signal(str)
    aviso = Signal(str)
    concluido = Signal(dict)
    erro = Signal(str)

    def __init__(
        self,
        configuracao: dict,
    ):
        super().__init__()

        self.configuracao = dict(
            configuracao
        )

    @Slot()
    def executar(self) -> None:
        try:
            resultado = processar_pasta(
                pasta_raiz=(
                    self.configuracao["pasta"]
                ),
                usar_ocr=(
                    self.configuracao["usar_ocr"]
                ),
                incluir_texto_ocr_csv=(
                    self.configuracao[
                        "incluir_texto_ocr_csv"
                    ]
                ),
                dpi=(
                    self.configuracao["dpi"]
                ),
                apenas_adicionar=(
                    self.configuracao[
                        "apenas_adicionar"
                    ]
                ),
                campos_csv=(
                    self.configuracao.get(
                        "campos_csv"
                    )
                ),
                callback_progresso=(
                    self.progresso.emit
                ),
                callback_status=(
                    self.status.emit
                ),
                callback_aviso=(
                    self.aviso.emit
                ),
            )

            self.concluido.emit(
                resultado
            )

        except Exception:  # noqa: BLE001
            self.erro.emit(
                traceback.format_exc()
            )


# =============================================================================
# Orquestrador principal
# =============================================================================

class AplicacaoNotasFiscais(QObject):
    """
    Classe principal da aplicação.

    Responsabilidades:
        - criar/conectar a view;
        - validar entradas;
        - consultar estado da pasta;
        - iniciar e encerrar worker/thread;
        - receber resultados;
        - transformar arquivos em dados de apresentação;
        - mandar a view atualizar a tela.
    """

    def __init__(self):
        super().__init__()

        self.janela = JanelaPrincipal(
            DIRETORIO_BASE
        )

        self.thread_processamento: Optional[
            QThread
        ] = None

        self.trabalhador: Optional[
            TrabalhadorProcessamento
        ] = None

        self.ultimo_resultado: Optional[
            dict
        ] = None

        self._conectar_sinais()
        self.atualizar_estado_pasta()

    # =========================================================================
    # Inicialização
    # =========================================================================

    def _conectar_sinais(self) -> None:
        self.janela.processamento_solicitado.connect(
            self.iniciar_processamento
        )

        self.janela.pasta_alterada.connect(
            self.atualizar_estado_pasta
        )

        self.janela.abrir_pasta_solicitado.connect(
            self.abrir_pasta_atual
        )

        self.janela.abrir_json_solicitado.connect(
            self.abrir_json
        )

        self.janela.abrir_csv_solicitado.connect(
            self.abrir_csv
        )

    def mostrar(self) -> None:
        self.janela.show()

    # =========================================================================
    # Estado da pasta
    # =========================================================================

    def _obter_pasta(
        self,
        mostrar_erro=False,
    ) -> Optional[Path]:
        configuracao = (
            self.janela.obter_configuracao()
        )

        caminho = configuracao["pasta"]

        if not caminho:
            if mostrar_erro:
                self.janela.mostrar_aviso(
                    "Pasta não informada",
                    (
                        "Selecione uma pasta "
                        "contendo arquivos PDF."
                    ),
                )

            return None

        pasta = Path(caminho)

        if not pasta.is_dir():
            if mostrar_erro:
                self.janela.mostrar_aviso(
                    "Pasta inválida",
                    (
                        "A pasta informada não existe:\n"
                        f"{pasta}"
                    ),
                )

            return None

        return pasta

    @Slot()
    @Slot(str)
    def atualizar_estado_pasta(
        self,
        _caminho="",
    ) -> None:
        pasta = self._obter_pasta(
            mostrar_erro=False
        )

        if pasta is None:
            self.janela.definir_estado_pasta(
                "—",
                "—",
                "—",
                "—",
            )
            return

        arquivos_pdf = list(
            pasta.rglob("*.pdf")
        )

        caminho_json = (
            pasta / NOME_ARQUIVO_JSON
        )
        caminho_csv = (
            pasta / NOME_ARQUIVO_CSV
        )

        quantidade_chaves = 0

        if caminho_json.exists():
            try:
                dados = carregar_json(
                    caminho_json
                )

                if isinstance(dados, dict):
                    quantidade_chaves = len(
                        dados
                    )

            except Exception:  # noqa: BLE001
                quantidade_chaves = 0

        self.janela.definir_estado_pasta(
            quantidade_pdfs=len(arquivos_pdf),
            tamanho_json=formatar_tamanho_arquivo(
                caminho_json
            ),
            tamanho_csv=formatar_tamanho_arquivo(
                caminho_csv
            ),
            quantidade_chaves=quantidade_chaves,
        )

    # =========================================================================
    # Execução
    # =========================================================================

    @Slot()
    def iniciar_processamento(self) -> None:
        if self.thread_processamento is not None:
            self.janela.mostrar_informacao(
                "Processamento em andamento",
                (
                    "Já existe um processamento "
                    "em execução."
                ),
            )
            return

        configuracao = (
            self.janela.obter_configuracao()
        )

        pasta = self._obter_pasta(
            mostrar_erro=True
        )

        if pasta is None:
            return

        arquivos_pdf = list(
            pasta.rglob("*.pdf")
        )

        if not arquivos_pdf:
            self.janela.mostrar_aviso(
                "Nenhum PDF encontrado",
                (
                    "A pasta selecionada não "
                    "contém arquivos PDF."
                ),
            )
            return

        configuracao["pasta"] = str(
            pasta
        )

        if not configuracao.get("campos_csv"):
            self.janela.mostrar_aviso(
                "Nenhum campo selecionado",
                (
                    "Marque ao menos um campo no grid "
                    "\"Campos do CSV\" para gerar o CSV."
                ),
            )
            return

        self.ultimo_resultado = None

        self.janela.limpar_resultados()
        self.janela.definir_processando(True)
        self.janela.atualizar_progresso(
            0,
            "Preparando processamento...",
        )
        self.janela.atualizar_status(
            "Processando..."
        )

        self.thread_processamento = QThread()
        self.trabalhador = (
            TrabalhadorProcessamento(
                configuracao
            )
        )

        self.trabalhador.moveToThread(
            self.thread_processamento
        )

        self.thread_processamento.started.connect(
            self.trabalhador.executar
        )

        self.trabalhador.progresso.connect(
            self.janela.atualizar_progresso
        )
        self.trabalhador.status.connect(
            self.janela.atualizar_status
        )
        self.trabalhador.aviso.connect(
            self._mostrar_aviso_worker
        )
        self.trabalhador.concluido.connect(
            self._processamento_concluido
        )
        self.trabalhador.erro.connect(
            self._processamento_com_erro
        )

        self.trabalhador.concluido.connect(
            self.thread_processamento.quit
        )
        self.trabalhador.erro.connect(
            self.thread_processamento.quit
        )

        self.thread_processamento.finished.connect(
            self.trabalhador.deleteLater
        )
        self.thread_processamento.finished.connect(
            self.thread_processamento.deleteLater
        )
        self.thread_processamento.finished.connect(
            self._thread_finalizada
        )

        self.thread_processamento.start()

    @Slot(str)
    def _mostrar_aviso_worker(
        self,
        mensagem: str,
    ) -> None:
        self.janela.mostrar_aviso(
            "Aviso",
            mensagem,
        )

    @Slot(dict)
    def _processamento_concluido(
        self,
        resultado: dict,
    ) -> None:
        if resultado.get("erro"):
            self.janela.atualizar_progresso(
                0,
                resultado["erro"],
            )
            self.janela.mostrar_aviso(
                "Processamento não realizado",
                resultado["erro"],
            )
            return

        self.ultimo_resultado = resultado

        self.janela.atualizar_progresso(
            100,
            (
                "Processamento concluído em "
                f"{resultado['duracao_segundos']:.1f} s."
            ),
        )

        self.janela.atualizar_status(
            "Processamento concluído."
        )

        # A UI recebe dados já preparados.
        self._enviar_resultado_para_ui(
            resultado
        )

        self.atualizar_estado_pasta()

        self.janela.mostrar_informacao(
            "Concluído",
            (
                "Processamento concluído com sucesso.\n\n"
                f"PDFs encontrados: "
                f"{resultado['quantidade_pdfs']}\n"
                f"Novas chaves: "
                f"{resultado['inseridas']}\n"
                f"Já existentes: "
                f"{resultado['atualizadas']}\n"
                f"Extrações parciais: "
                f"{resultado['parciais']}\n"
                f"Falhas: "
                f"{resultado['falhas']}"
            ),
        )

    @Slot(str)
    def _processamento_com_erro(
        self,
        mensagem: str,
    ) -> None:
        self.janela.atualizar_progresso(
            0,
            (
                "O processamento foi "
                "interrompido por um erro."
            ),
        )

        self.janela.atualizar_status(
            "Erro durante o processamento."
        )

        self.janela.mostrar_erro(
            "Erro durante o processamento",
            mensagem,
        )

    @Slot()
    def _thread_finalizada(self) -> None:
        self.thread_processamento = None
        self.trabalhador = None

        self.janela.definir_processando(
            False
        )

    # =========================================================================
    # Preparação de dados para a UI
    # =========================================================================

    def _enviar_resultado_para_ui(
        self,
        resultado: dict,
    ) -> None:
        self.janela.exibir_metricas(
            pdfs=resultado["quantidade_pdfs"],
            novas=resultado["inseridas"],
            existentes=resultado["atualizadas"],
            parciais=resultado["parciais"],
            falhas=resultado["falhas"],
        )

        self.janela.exibir_resumo(
            self._montar_resumo(
                resultado
            )
        )

        texto_json = self._ler_json_formatado(
            Path(resultado["caminho_json"])
        )
        self.janela.exibir_json(
            texto_json
        )

        cabecalhos, linhas = (
            self._ler_csv_para_tabela(
                Path(resultado["caminho_csv"])
            )
        )

        self.janela.exibir_csv(
            cabecalhos,
            linhas,
        )

        self.janela.exibir_falhas(
            resultado.get(
                "detalhes_falhas",
                [],
            )
        )

        self.janela.definir_arquivos_disponiveis(
            json_disponivel=Path(
                resultado["caminho_json"]
            ).exists(),
            csv_disponivel=Path(
                resultado["caminho_csv"]
            ).exists(),
        )

    def _montar_resumo(
        self,
        resultado: dict,
    ) -> str:
        linhas = [
            "RESUMO DO PROCESSAMENTO",
            "",
            f"Pasta: {resultado['pasta_raiz']}",
            "",
            (
                "PDFs encontrados: "
                f"{resultado['quantidade_pdfs']}"
            ),
            (
                "PDFs com chave válida: "
                f"{resultado['quantidade_pdfs'] - resultado['falhas']}"
            ),
            (
                "Novas chaves: "
                f"{resultado['inseridas']}"
            ),
            (
                "Chaves já existentes / mescladas: "
                f"{resultado['atualizadas']}"
            ),
            (
                "Extrações parciais: "
                f"{resultado['parciais']}"
            ),
            (
                "Falhas: "
                f"{resultado['falhas']}"
            ),
            "",
            (
                "Chaves no JSON: "
                f"{resultado['quantidade_antes']} -> "
                f"{resultado['quantidade_depois']}"
            ),
            (
                "CSV: "
                f"{resultado['linhas_csv']} linhas x "
                f"{resultado['colunas_csv']} colunas"
            ),
            (
                "Duração: "
                f"{resultado['duracao_segundos']:.1f} s"
            ),
            "",
            "ARQUIVOS",
            (
                "JSON: "
                f"{resultado['caminho_json']}"
            ),
            (
                "CSV: "
                f"{resultado['caminho_csv']}"
            ),
        ]

        caminho_relatorio = Path(
            resultado["caminho_relatorio"]
        )

        if caminho_relatorio.exists():
            linhas.append(
                "Relatório: "
                f"{resultado['caminho_relatorio']}"
            )

        if resultado["anomalias"] is not None:
            linhas.extend(
                [
                    "",
                    "REVISÃO",
                    (
                        "Valores numéricos que precisam "
                        "de revisão manual: "
                        f"{resultado['anomalias']}"
                    ),
                ]
            )

        return "\n".join(linhas)

    def _ler_json_formatado(
        self,
        caminho: Path,
    ) -> str:
        if not caminho.exists():
            return (
                "Arquivo JSON não encontrado."
            )

        try:
            with caminho.open(
                "r",
                encoding="utf-8",
            ) as arquivo:
                dados = json.load(
                    arquivo
                )

            return json.dumps(
                dados,
                ensure_ascii=False,
                indent=2,
            )

        except Exception as erro:  # noqa: BLE001
            return (
                "Não foi possível carregar "
                f"o JSON:\n{erro}"
            )

    def _ler_csv_para_tabela(
        self,
        caminho: Path,
    ) -> tuple[list[str], list[list[str]]]:
        if not caminho.exists():
            return [], []

        try:
            with caminho.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as arquivo:
                leitor = csv.reader(
                    arquivo,
                    delimiter=",",
                )

                conteudo = list(
                    leitor
                )

        except Exception as erro:  # noqa: BLE001
            self.janela.mostrar_aviso(
                "Erro ao carregar CSV",
                str(erro),
            )
            return [], []

        if not conteudo:
            return [], []

        return (
            list(conteudo[0]),
            [
                list(linha)
                for linha in conteudo[1:]
            ],
        )

    # =========================================================================
    # Abertura de arquivos
    # =========================================================================

    @Slot()
    def abrir_pasta_atual(self) -> None:
        pasta = self._obter_pasta(
            mostrar_erro=True
        )

        if pasta is None:
            return

        try:
            abrir_caminho(
                pasta
            )

        except Exception as erro:  # noqa: BLE001
            self.janela.mostrar_erro(
                "Erro",
                (
                    "Não foi possível abrir "
                    f"a pasta:\n{erro}"
                ),
            )

    @Slot()
    def abrir_json(self) -> None:
        if not self.ultimo_resultado:
            return

        self._abrir_arquivo_resultado(
            "caminho_json",
            "JSON",
        )

    @Slot()
    def abrir_csv(self) -> None:
        if not self.ultimo_resultado:
            return

        self._abrir_arquivo_resultado(
            "caminho_csv",
            "CSV",
        )

    def _abrir_arquivo_resultado(
        self,
        chave: str,
        nome: str,
    ) -> None:
        caminho = Path(
            self.ultimo_resultado[chave]
        )

        try:
            abrir_caminho(
                caminho
            )

        except Exception as erro:  # noqa: BLE001
            self.janela.mostrar_erro(
                "Erro",
                (
                    f"Não foi possível abrir "
                    f"o {nome}:\n{erro}"
                ),
            )


# =============================================================================
# Entrada da aplicação
# =============================================================================

def main() -> None:
    # garante o arquivo de log de rastreio (rastreio_extracao.log na raiz)
    configurar_log()

    aplicativo = QApplication(
        sys.argv
    )

    aplicativo.setApplicationName(
        "Leitor de Notas Fiscais"
    )
    aplicativo.setOrganizationName(
        "AGROROBÓTICA"
    )

    aplicar_tema_global(
        aplicativo
    )

    orquestrador = (
        AplicacaoNotasFiscais()
    )
    orquestrador.mostrar()

    sys.exit(
        aplicativo.exec()
    )


if __name__ == "__main__":
    main()
