# -*- coding: utf-8 -*-
"""
Interface principal do Leitor de Notas Fiscais.

Responsabilidade deste módulo:
    - construir a interface PySide6;
    - receber interações do usuário;
    - emitir sinais para o orquestrador;
    - exibir estados, progresso e resultados;
    - aplicar o tema visual dark/dourado.

Este módulo NÃO executa:
    - OCR;
    - leitura/extração de notas;
    - regras de mesclagem;
    - geração de JSON/CSV;
    - processamento em segundo plano.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# =============================================================================
# Tema global
# =============================================================================

def aplicar_tema_global(aplicativo: QApplication) -> None:
    """
    Aplica Fusion + paleta escura antes da criação da janela.

    Isso impede que controles nativos do Windows mantenham fundo branco enquanto
    o restante da aplicação usa o tema escuro.
    """
    aplicativo.setStyle("Fusion")

    paleta = QPalette()

    fundo = QColor("#0D0F10")
    base = QColor("#151718")
    alternativo = QColor("#1B1D1E")
    botao = QColor("#202223")

    texto = QColor("#F2E7BE")
    texto_desabilitado = QColor("#746D58")
    dourado = QColor("#D4AF37")

    paleta.setColor(QPalette.ColorRole.Window, fundo)
    paleta.setColor(QPalette.ColorRole.WindowText, texto)
    paleta.setColor(QPalette.ColorRole.Base, base)
    paleta.setColor(QPalette.ColorRole.AlternateBase, alternativo)
    paleta.setColor(QPalette.ColorRole.ToolTipBase, botao)
    paleta.setColor(QPalette.ColorRole.ToolTipText, texto)
    paleta.setColor(QPalette.ColorRole.Text, texto)
    paleta.setColor(QPalette.ColorRole.Button, botao)
    paleta.setColor(QPalette.ColorRole.ButtonText, texto)
    paleta.setColor(QPalette.ColorRole.BrightText, QColor("#FFE58B"))
    paleta.setColor(QPalette.ColorRole.Link, dourado)
    paleta.setColor(QPalette.ColorRole.Highlight, dourado)
    paleta.setColor(QPalette.ColorRole.HighlightedText, QColor("#0B0C0D"))

    try:
        paleta.setColor(
            QPalette.ColorRole.PlaceholderText,
            QColor("#817A64"),
        )
    except AttributeError:
        pass

    for papel in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        paleta.setColor(
            QPalette.ColorGroup.Disabled,
            papel,
            texto_desabilitado,
        )

    aplicativo.setPalette(paleta)


# =============================================================================
# Cartão de métrica
# =============================================================================

class CartaoMetrica(QFrame):
    """Componente visual simples para exibir uma métrica."""

    def __init__(
        self,
        titulo: str,
        valor: str = "—",
        parent=None,
    ):
        super().__init__(parent)

        self.setObjectName("cartaoMetrica")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)

        self.rotulo_titulo = QLabel(titulo)
        self.rotulo_titulo.setObjectName("tituloMetrica")

        self.rotulo_valor = QLabel(str(valor))
        self.rotulo_valor.setObjectName("valorMetrica")

        layout.addWidget(self.rotulo_titulo)
        layout.addWidget(self.rotulo_valor)

    def definir_valor(self, valor) -> None:
        self.rotulo_valor.setText(str(valor))


# =============================================================================
# Janela principal
# =============================================================================

class JanelaPrincipal(QMainWindow):
    """
    View principal da aplicação.

    Toda ação que exige regra de negócio é comunicada ao orquestrador por sinais.
    """

    processamento_solicitado = Signal()
    pasta_alterada = Signal(str)

    abrir_pasta_solicitado = Signal()
    abrir_json_solicitado = Signal()
    abrir_csv_solicitado = Signal()

    def __init__(
        self,
        diretorio_inicial: str | Path,
        parent=None,
    ):
        super().__init__(parent)

        self._processando = False

        self.setWindowTitle(
            "Leitor de Notas Fiscais - PDF para JSON e CSV"
        )
        self.resize(1280, 820)
        self.setMinimumSize(980, 680)

        self._criar_interface(
            str(diretorio_inicial)
        )
        self._aplicar_estilo()

    # =========================================================================
    # Construção da interface
    # =========================================================================

    def _criar_interface(
        self,
        diretorio_inicial: str,
    ) -> None:
        widget_central = QWidget()
        self.setCentralWidget(widget_central)

        layout_principal = QVBoxLayout(widget_central)
        layout_principal.setContentsMargins(18, 16, 18, 16)
        layout_principal.setSpacing(12)

        # ---------------------------------------------------------------------
        # Cabeçalho
        # ---------------------------------------------------------------------

        titulo = QLabel("🧾 Leitor de Notas Fiscais")
        titulo.setObjectName("tituloPrincipal")

        subtitulo = QLabel("PDF de NF-e/DANFE → JSON + CSV")
        subtitulo.setObjectName("subtituloPrincipal")

        descricao = QLabel(
            "Selecione uma pasta com os PDFs. O programa também pesquisa "
            "subpastas e salva os arquivos consolidados na própria pasta."
        )
        descricao.setWordWrap(True)
        descricao.setObjectName("descricaoPrincipal")

        layout_principal.addWidget(titulo)
        layout_principal.addWidget(subtitulo)
        layout_principal.addWidget(descricao)

        # ---------------------------------------------------------------------
        # Pasta de trabalho
        # ---------------------------------------------------------------------

        grupo_pasta = QGroupBox("Pasta de trabalho")
        layout_pasta = QHBoxLayout(grupo_pasta)

        self.campo_pasta = QLineEdit(diretorio_inicial)
        self.campo_pasta.setPlaceholderText(
            "Selecione a pasta contendo os arquivos PDF..."
        )
        self.campo_pasta.editingFinished.connect(
            self._emitir_pasta_alterada
        )

        self.botao_selecionar_pasta = QPushButton(
            "Selecionar pasta"
        )
        self.botao_selecionar_pasta.clicked.connect(
            self._selecionar_pasta
        )

        self.botao_abrir_pasta = QPushButton("Abrir pasta")
        self.botao_abrir_pasta.clicked.connect(
            self.abrir_pasta_solicitado.emit
        )

        layout_pasta.addWidget(self.campo_pasta, 1)
        layout_pasta.addWidget(self.botao_selecionar_pasta)
        layout_pasta.addWidget(self.botao_abrir_pasta)

        layout_principal.addWidget(grupo_pasta)

        # ---------------------------------------------------------------------
        # Corpo principal
        # ---------------------------------------------------------------------

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ---------------------------------------------------------------------
        # Opções
        # ---------------------------------------------------------------------

        painel_opcoes = QFrame()
        painel_opcoes.setObjectName("painelOpcoes")
        painel_opcoes.setMinimumWidth(270)
        painel_opcoes.setMaximumWidth(360)

        layout_opcoes = QVBoxLayout(painel_opcoes)
        layout_opcoes.setContentsMargins(14, 14, 14, 14)
        layout_opcoes.setSpacing(12)

        titulo_opcoes = QLabel("⚙️ Opções de extração")
        titulo_opcoes.setObjectName("tituloSecao")
        layout_opcoes.addWidget(titulo_opcoes)

        self.checkbox_ocr = QCheckBox("Usar OCR (RapidOCR)")
        self.checkbox_ocr.setChecked(True)
        self.checkbox_ocr.setToolTip(
            "Necessário para PDFs digitalizados ou sem texto incorporado."
        )

        self.checkbox_apenas_adicionar = QCheckBox(
            "Adicionar apenas campos ausentes"
        )
        self.checkbox_apenas_adicionar.setChecked(True)
        self.checkbox_apenas_adicionar.setToolTip(
            "Preserva valores já existentes no JSON."
        )

        self.checkbox_texto_ocr_csv = QCheckBox(
            "Incluir 'texto_ocr' no CSV"
        )
        self.checkbox_texto_ocr_csv.setChecked(False)
        self.checkbox_texto_ocr_csv.setToolTip(
            "Inclui o texto completo extraído no CSV."
        )

        layout_opcoes.addWidget(self.checkbox_ocr)
        layout_opcoes.addWidget(self.checkbox_apenas_adicionar)
        layout_opcoes.addWidget(self.checkbox_texto_ocr_csv)

        layout_opcoes.addWidget(QLabel("DPI do OCR"))

        self.campo_dpi = QSpinBox()
        self.campo_dpi.setRange(150, 600)
        self.campo_dpi.setSingleStep(50)
        self.campo_dpi.setValue(300)

        layout_opcoes.addWidget(self.campo_dpi)
        layout_opcoes.addSpacing(8)

        # ---------------------------------------------------------------------
        # Estado da pasta
        # ---------------------------------------------------------------------

        titulo_estado = QLabel("Estado atual")
        titulo_estado.setObjectName("tituloSecao")
        layout_opcoes.addWidget(titulo_estado)

        self.rotulo_quantidade_pdfs = QLabel(
            "PDFs encontrados: —"
        )
        self.rotulo_json_existente = QLabel(
            "JSON existente: —"
        )
        self.rotulo_csv_existente = QLabel(
            "CSV existente: —"
        )
        self.rotulo_chaves_existentes = QLabel(
            "Chaves existentes: —"
        )

        layout_opcoes.addWidget(self.rotulo_quantidade_pdfs)
        layout_opcoes.addWidget(self.rotulo_json_existente)
        layout_opcoes.addWidget(self.rotulo_csv_existente)
        layout_opcoes.addWidget(self.rotulo_chaves_existentes)

        layout_opcoes.addStretch(1)

        self.botao_executar = QPushButton(
            "▶ Executar processamento"
        )
        self.botao_executar.setObjectName("botaoExecutar")
        self.botao_executar.clicked.connect(
            self.processamento_solicitado.emit
        )

        layout_opcoes.addWidget(self.botao_executar)

        splitter.addWidget(painel_opcoes)

        # ---------------------------------------------------------------------
        # Resultados
        # ---------------------------------------------------------------------

        painel_conteudo = QWidget()
        layout_conteudo = QVBoxLayout(painel_conteudo)
        layout_conteudo.setContentsMargins(10, 0, 0, 0)
        layout_conteudo.setSpacing(10)

        self.rotulo_status = QLabel("Pronto para processar.")
        self.rotulo_status.setObjectName("rotuloStatus")
        self.rotulo_status.setWordWrap(True)

        self.barra_progresso = QProgressBar()
        self.barra_progresso.setRange(0, 100)
        self.barra_progresso.setValue(0)
        self.barra_progresso.setFormat("%p%")

        layout_conteudo.addWidget(self.rotulo_status)
        layout_conteudo.addWidget(self.barra_progresso)

        # ---------------------------------------------------------------------
        # Métricas
        # ---------------------------------------------------------------------

        painel_metricas = QWidget()
        layout_metricas = QGridLayout(painel_metricas)
        layout_metricas.setContentsMargins(0, 0, 0, 0)
        layout_metricas.setHorizontalSpacing(8)
        layout_metricas.setVerticalSpacing(8)

        self.metrica_pdfs = CartaoMetrica("PDFs")
        self.metrica_novas = CartaoMetrica("Novas chaves")
        self.metrica_existentes = CartaoMetrica("Já existentes")
        self.metrica_parciais = CartaoMetrica("Parciais")
        self.metrica_falhas = CartaoMetrica("Falhas")

        layout_metricas.addWidget(self.metrica_pdfs, 0, 0)
        layout_metricas.addWidget(self.metrica_novas, 0, 1)
        layout_metricas.addWidget(self.metrica_existentes, 0, 2)
        layout_metricas.addWidget(self.metrica_parciais, 0, 3)
        layout_metricas.addWidget(self.metrica_falhas, 0, 4)

        layout_conteudo.addWidget(painel_metricas)

        # ---------------------------------------------------------------------
        # Abas
        # ---------------------------------------------------------------------

        self.abas = QTabWidget()

        # Resumo
        aba_resumo = QWidget()
        layout_resumo = QVBoxLayout(aba_resumo)

        self.texto_resumo = QPlainTextEdit()
        self.texto_resumo.setReadOnly(True)
        layout_resumo.addWidget(self.texto_resumo)

        self.abas.addTab(aba_resumo, "Resumo")

        # JSON
        aba_json = QWidget()
        layout_json = QVBoxLayout(aba_json)

        botoes_json = QHBoxLayout()

        self.botao_abrir_json = QPushButton("Abrir JSON")
        self.botao_abrir_json.setEnabled(False)
        self.botao_abrir_json.clicked.connect(
            self.abrir_json_solicitado.emit
        )

        botoes_json.addWidget(self.botao_abrir_json)
        botoes_json.addStretch(1)

        self.texto_json = QPlainTextEdit()
        self.texto_json.setReadOnly(True)
        self.texto_json.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.NoWrap
        )

        fonte_mono = QFont("Consolas")
        fonte_mono.setStyleHint(QFont.StyleHint.Monospace)
        self.texto_json.setFont(fonte_mono)

        layout_json.addLayout(botoes_json)
        layout_json.addWidget(self.texto_json)

        self.abas.addTab(aba_json, "JSON")

        # CSV
        aba_csv = QWidget()
        layout_csv = QVBoxLayout(aba_csv)

        botoes_csv = QHBoxLayout()

        self.botao_abrir_csv = QPushButton("Abrir CSV")
        self.botao_abrir_csv.setEnabled(False)
        self.botao_abrir_csv.clicked.connect(
            self.abrir_csv_solicitado.emit
        )

        botoes_csv.addWidget(self.botao_abrir_csv)
        botoes_csv.addStretch(1)

        self.tabela_csv = QTableWidget()
        self.tabela_csv.setAlternatingRowColors(True)
        self.tabela_csv.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.tabela_csv.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        layout_csv.addLayout(botoes_csv)
        layout_csv.addWidget(self.tabela_csv)

        self.abas.addTab(aba_csv, "CSV")

        # Falhas
        aba_falhas = QWidget()
        layout_falhas = QVBoxLayout(aba_falhas)

        self.tabela_falhas = QTableWidget()
        self.tabela_falhas.setColumnCount(2)
        self.tabela_falhas.setHorizontalHeaderLabels(
            ["Arquivo", "Motivo"]
        )
        self.tabela_falhas.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.tabela_falhas.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        self.tabela_falhas.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.tabela_falhas.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )

        layout_falhas.addWidget(self.tabela_falhas)
        self.abas.addTab(aba_falhas, "Falhas")

        layout_conteudo.addWidget(self.abas, 1)

        splitter.addWidget(painel_conteudo)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout_principal.addWidget(splitter, 1)

        barra_status = QStatusBar()
        self.setStatusBar(barra_status)
        barra_status.showMessage("Pronto.")

    # =========================================================================
    # Interação do usuário
    # =========================================================================

    @Slot()
    def _selecionar_pasta(self) -> None:
        pasta_atual = self.campo_pasta.text().strip()

        pasta = QFileDialog.getExistingDirectory(
            self,
            "Selecionar pasta com PDFs",
            pasta_atual,
        )

        if not pasta:
            return

        self.campo_pasta.setText(pasta)
        self.pasta_alterada.emit(pasta)

    @Slot()
    def _emitir_pasta_alterada(self) -> None:
        self.pasta_alterada.emit(
            self.campo_pasta.text().strip()
        )

    def obter_configuracao(self) -> dict:
        """Retorna somente os valores escolhidos pelo usuário."""
        return {
            "pasta": (
                self.campo_pasta.text()
                .strip()
                .strip('"')
                .strip("'")
            ),
            "usar_ocr": self.checkbox_ocr.isChecked(),
            "apenas_adicionar": (
                self.checkbox_apenas_adicionar.isChecked()
            ),
            "incluir_texto_ocr_csv": (
                self.checkbox_texto_ocr_csv.isChecked()
            ),
            "dpi": self.campo_dpi.value(),
        }

    # =========================================================================
    # Métodos usados pelo orquestrador para atualizar a view
    # =========================================================================

    def definir_estado_pasta(
        self,
        quantidade_pdfs: int | str,
        tamanho_json: str,
        tamanho_csv: str,
        quantidade_chaves: int | str,
    ) -> None:
        self.rotulo_quantidade_pdfs.setText(
            f"PDFs encontrados: {quantidade_pdfs}"
        )
        self.rotulo_json_existente.setText(
            f"JSON existente: {tamanho_json}"
        )
        self.rotulo_csv_existente.setText(
            f"CSV existente: {tamanho_csv}"
        )
        self.rotulo_chaves_existentes.setText(
            f"Chaves existentes: {quantidade_chaves}"
        )

    def definir_processando(
        self,
        processando: bool,
    ) -> None:
        self._processando = processando

        habilitado = not processando

        self.botao_executar.setEnabled(habilitado)
        self.botao_selecionar_pasta.setEnabled(habilitado)
        self.campo_pasta.setEnabled(habilitado)
        self.checkbox_ocr.setEnabled(habilitado)
        self.checkbox_apenas_adicionar.setEnabled(habilitado)
        self.checkbox_texto_ocr_csv.setEnabled(habilitado)
        self.campo_dpi.setEnabled(habilitado)

        if processando:
            self.botao_executar.setText(
                "Processando..."
            )
        else:
            self.botao_executar.setText(
                "▶ Executar processamento"
            )

    def atualizar_progresso(
        self,
        percentual: int,
        texto: str,
    ) -> None:
        self.barra_progresso.setValue(percentual)
        self.rotulo_status.setText(texto)

    def atualizar_status(
        self,
        texto: str,
    ) -> None:
        self.statusBar().showMessage(texto)

    def exibir_metricas(
        self,
        pdfs,
        novas,
        existentes,
        parciais,
        falhas,
    ) -> None:
        self.metrica_pdfs.definir_valor(pdfs)
        self.metrica_novas.definir_valor(novas)
        self.metrica_existentes.definir_valor(existentes)
        self.metrica_parciais.definir_valor(parciais)
        self.metrica_falhas.definir_valor(falhas)

    def exibir_resumo(
        self,
        texto: str,
    ) -> None:
        self.texto_resumo.setPlainText(texto)

    def exibir_json(
        self,
        texto: str,
    ) -> None:
        self.texto_json.setPlainText(texto)

    def exibir_csv(
        self,
        cabecalhos: Sequence[str],
        linhas: Sequence[Sequence[str]],
    ) -> None:
        self.tabela_csv.clear()

        self.tabela_csv.setColumnCount(
            len(cabecalhos)
        )
        self.tabela_csv.setHorizontalHeaderLabels(
            list(cabecalhos)
        )
        self.tabela_csv.setRowCount(
            len(linhas)
        )

        for indice_linha, linha in enumerate(linhas):
            for indice_coluna in range(len(cabecalhos)):
                valor = (
                    linha[indice_coluna]
                    if indice_coluna < len(linha)
                    else ""
                )

                self.tabela_csv.setItem(
                    indice_linha,
                    indice_coluna,
                    QTableWidgetItem(str(valor)),
                )

        if len(linhas) > 500:
            self.tabela_csv.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Interactive
            )
            self.tabela_csv.resizeColumnsToContents()
        else:
            self.tabela_csv.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents
            )

    def exibir_falhas(
        self,
        falhas: Sequence[dict],
    ) -> None:
        self.tabela_falhas.setRowCount(
            len(falhas)
        )

        for indice, falha in enumerate(falhas):
            self.tabela_falhas.setItem(
                indice,
                0,
                QTableWidgetItem(
                    str(falha.get("arquivo", ""))
                ),
            )
            self.tabela_falhas.setItem(
                indice,
                1,
                QTableWidgetItem(
                    str(falha.get("motivo", ""))
                ),
            )

    def definir_arquivos_disponiveis(
        self,
        json_disponivel: bool,
        csv_disponivel: bool,
    ) -> None:
        self.botao_abrir_json.setEnabled(
            json_disponivel
        )
        self.botao_abrir_csv.setEnabled(
            csv_disponivel
        )

    def limpar_resultados(self) -> None:
        self.barra_progresso.setValue(0)
        self.rotulo_status.setText(
            "Pronto para processar."
        )

        self.texto_resumo.clear()
        self.texto_json.clear()

        self.tabela_csv.clear()
        self.tabela_csv.setRowCount(0)
        self.tabela_csv.setColumnCount(0)

        self.tabela_falhas.setRowCount(0)

        self.metrica_pdfs.definir_valor("—")
        self.metrica_novas.definir_valor("—")
        self.metrica_existentes.definir_valor("—")
        self.metrica_parciais.definir_valor("—")
        self.metrica_falhas.definir_valor("—")

        self.definir_arquivos_disponiveis(
            False,
            False,
        )

    # =========================================================================
    # Diálogos
    # =========================================================================

    def mostrar_informacao(
        self,
        titulo: str,
        mensagem: str,
    ) -> None:
        QMessageBox.information(
            self,
            titulo,
            mensagem,
        )

    def mostrar_aviso(
        self,
        titulo: str,
        mensagem: str,
    ) -> None:
        QMessageBox.warning(
            self,
            titulo,
            mensagem,
        )

    def mostrar_erro(
        self,
        titulo: str,
        mensagem: str,
    ) -> None:
        QMessageBox.critical(
            self,
            titulo,
            mensagem,
        )

    # =========================================================================
    # Proteção ao fechar durante processamento
    # =========================================================================

    def closeEvent(
        self,
        evento,
    ) -> None:
        if self._processando:
            QMessageBox.warning(
                self,
                "Processamento em andamento",
                (
                    "Existe um processamento em andamento.\n\n"
                    "Aguarde a conclusão antes de fechar a aplicação."
                ),
            )
            evento.ignore()
            return

        evento.accept()

    # =========================================================================
    # Estilo
    # =========================================================================

    def _aplicar_estilo(self) -> None:
        aplicativo = QApplication.instance()

        if aplicativo is None:
            return

        aplicativo.setStyleSheet(
            """
            QMainWindow,
            QDialog,
            QWidget {
                background-color: #0D0F10;
                color: #F2E7BE;
                font-size: 13px;
            }

            QLabel#tituloPrincipal {
                background: transparent;
                color: #E0B83F;
                font-size: 27px;
                font-weight: 700;
            }

            QLabel#subtituloPrincipal {
                background: transparent;
                color: #F0D77A;
                font-size: 16px;
                font-weight: 600;
            }

            QLabel#descricaoPrincipal {
                background: transparent;
                color: #CFC39A;
            }

            QLabel#tituloSecao {
                background: transparent;
                color: #DDBA4C;
                font-size: 14px;
                font-weight: 700;
            }

            QLabel#rotuloStatus {
                background: transparent;
                color: #F3E7BB;
                font-weight: 600;
            }

            QGroupBox {
                background-color: #141617;
                color: #E7C85F;
                border: 1px solid #66531B;
                border-radius: 8px;
                margin-top: 11px;
                padding-top: 10px;
                font-weight: 600;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                color: #E0B83F;
                background-color: #141617;
            }

            QFrame#painelOpcoes {
                background-color: #141617;
                border: 1px solid #5F4D19;
                border-radius: 9px;
            }

            QFrame#cartaoMetrica {
                background-color: #17191A;
                border: 1px solid #705B1D;
                border-radius: 8px;
            }

            QLabel#tituloMetrica {
                background: transparent;
                color: #BDAA6D;
                font-size: 11px;
            }

            QLabel#valorMetrica {
                background: transparent;
                color: #F5D76E;
                font-size: 22px;
                font-weight: 700;
            }

            QLineEdit,
            QSpinBox,
            QPlainTextEdit,
            QTableWidget {
                background-color: #151718;
                color: #F4E8BF;
                border: 1px solid #66531B;
                border-radius: 5px;
                selection-background-color: #C49B2E;
                selection-color: #0B0C0D;
            }

            QLineEdit {
                min-height: 23px;
                padding: 6px 8px;
            }

            QLineEdit::placeholder {
                color: #817A64;
            }

            QSpinBox {
                min-height: 23px;
                padding: 5px 8px;
            }

            QLineEdit:focus,
            QSpinBox:focus,
            QPlainTextEdit:focus,
            QTableWidget:focus {
                border: 1px solid #E0B83F;
            }

            QPushButton {
                min-height: 24px;
                padding: 7px 13px;
                color: #F1DF9D;
                background-color: #202223;
                border: 1px solid #78621F;
                border-radius: 6px;
                font-weight: 500;
            }

            QPushButton:hover {
                color: #FFE89A;
                background-color: #302A17;
                border: 1px solid #D4AF37;
            }

            QPushButton:pressed {
                color: #0D0F10;
                background-color: #C9A437;
                border: 1px solid #F3D66F;
            }

            QPushButton:disabled {
                color: #6F6957;
                background-color: #181A1B;
                border: 1px solid #38331F;
            }

            QPushButton#botaoExecutar {
                min-height: 32px;
                color: #111213;
                background-color: #D4AF37;
                border: 1px solid #F1D66E;
                border-radius: 6px;
                font-weight: 700;
            }

            QPushButton#botaoExecutar:hover {
                background-color: #E6C14D;
                border: 1px solid #FFE58B;
            }

            QCheckBox {
                background: transparent;
                color: #E8D99E;
                spacing: 8px;
            }

            QCheckBox::indicator {
                width: 17px;
                height: 17px;
                background-color: #151718;
                border: 1px solid #76611F;
                border-radius: 4px;
            }

            QCheckBox::indicator:checked {
                background-color: #D4AF37;
                border: 1px solid #F1D66E;
            }

            QProgressBar {
                min-height: 20px;
                color: #F6EBC2;
                background-color: #151718;
                border: 1px solid #66531B;
                border-radius: 6px;
                text-align: center;
                font-weight: 600;
            }

            QProgressBar::chunk {
                background-color: #D4AF37;
                border-radius: 5px;
            }

            QTabWidget::pane {
                background-color: #121415;
                border: 1px solid #5D4A18;
                border-radius: 3px;
                top: -1px;
            }

            QTabBar::tab {
                min-width: 85px;
                padding: 9px 16px;
                color: #C9B879;
                background-color: #202223;
                border: 1px solid #4E4016;
                border-bottom: none;
                margin-right: 2px;
            }

            QTabBar::tab:selected {
                color: #FFE388;
                background-color: #2A2516;
                border: 1px solid #C9A232;
                border-bottom: none;
                font-weight: 700;
            }

            QTableWidget {
                background-color: #121415;
                alternate-background-color: #181A1B;
                color: #EDE2B9;
                gridline-color: #3F361B;
            }

            QTableWidget::item:selected {
                color: #0C0D0D;
                background-color: #D4AF37;
            }

            QHeaderView::section {
                color: #E8D17B;
                background-color: #242628;
                padding: 6px;
                border: 0;
                border-right: 1px solid #514318;
                border-bottom: 1px solid #514318;
                font-weight: 600;
            }

            QTableCornerButton::section {
                background-color: #242628;
                border: 1px solid #514318;
            }

            QScrollBar:vertical {
                width: 13px;
                background-color: #101213;
            }

            QScrollBar::handle:vertical {
                min-height: 30px;
                background-color: #65531F;
                border-radius: 6px;
            }

            QScrollBar:horizontal {
                height: 13px;
                background-color: #101213;
            }

            QScrollBar::handle:horizontal {
                min-width: 30px;
                background-color: #65531F;
                border-radius: 6px;
            }

            QScrollBar::add-line,
            QScrollBar::sub-line {
                width: 0;
                height: 0;
            }

            QStatusBar {
                color: #CFC08C;
                background-color: #111314;
                border-top: 1px solid #4B3E17;
            }

            QSplitter::handle {
                background-color: #5B4A18;
            }

            QToolTip {
                color: #F5E7B0;
                background-color: #242628;
                border: 1px solid #D4AF37;
                padding: 4px;
            }

            QMessageBox,
            QMessageBox QWidget {
                background-color: #141617;
                color: #F3E7BB;
            }

            QMessageBox QLabel {
                background: transparent;
                color: #F3E7BB;
            }
            """
        )
