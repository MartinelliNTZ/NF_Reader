# -*- coding: utf-8 -*-
"""
Leitor de Notas Fiscais - PDF -> JSON + CSV com Streamlit.

Uso:
    streamlit run leitor_notas_fiscais.py

O que este programa faz:
    1. Exibe um campo para informar o caminho de uma pasta e um botão "Executar".
    2. Ao executar, percorre todos os arquivos PDF da pasta, incluindo subpastas,
       e extrai os dados de cada nota fiscal:
       - chave de acesso;
       - emitente;
       - destinatário;
       - transportadora;
       - produtos;
       - valores;
       - demais informações disponíveis no pipeline existente em `core/`.
    3. Salva automaticamente, na própria pasta analisada:
       - notas_fiscais.json
       - notas_fiscais.csv
       - relatorio_normalizacao.txt
    4. Se o JSON ou CSV já existirem, os dados existentes são preservados por
       padrão. Apenas novas chaves e campos anteriormente vazios são preenchidos.

Requisitos:
    pip install streamlit pymupdf pandas rapidocr-onnxruntime opencv-python
"""

import json
import sys
import time
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st


# =============================================================================
# Configuração do projeto
# =============================================================================

DIRETORIO_BASE = Path(__file__).resolve().parent

if str(DIRETORIO_BASE) not in sys.path:
    sys.path.insert(0, str(DIRETORIO_BASE))


# =============================================================================
# Pipeline de extração existente
# =============================================================================
#
# Os nomes originais dos módulos externos são mantidos no import para garantir
# compatibilidade com o projeto atual. Quando necessário, são utilizados aliases
# em português dentro deste arquivo.
#

from core.extrair_notas import (
    carregar_json,
    generar_reporte as gerar_relatorio,
    ler_paginas_texto,
    obter_engine_ocr as obter_motor_ocr,
    processar_pdf,
    salvar_json,
)

from core.generar_csv import (
    construir_filas as construir_linhas,
    guardar_csv as salvar_csv,
)


NOME_ARQUIVO_JSON = "notas_fiscais.json"
NOME_ARQUIVO_CSV = "notas_fiscais.csv"
NOME_ARQUIVO_RELATORIO = "relatorio_normalizacao.txt"


# =============================================================================
# Utilitários de mesclagem
# =============================================================================

def valor_esta_vazio(valor):
    """Retorna True quando o valor não contém informação útil."""
    if valor is None:
        return True

    if isinstance(valor, str):
        return valor.strip() == ""

    if isinstance(valor, (list, dict)):
        return len(valor) == 0

    return False


def mesclar_listas(nova_lista, lista_existente):
    """
    Mescla duas listas elemento a elemento, preservando os dados existentes.

    Regras:
        - Elementos já preenchidos são mantidos.
        - Elementos vazios podem ser preenchidos com os novos valores.
        - Dicionários na mesma posição são mesclados recursivamente.
        - Novos elementos excedentes são adicionados ao final.
    """
    resultado = list(lista_existente)

    for indice, novo_valor in enumerate(nova_lista):
        if indice < len(resultado):
            valor_existente = resultado[indice]

            if isinstance(novo_valor, dict) and isinstance(valor_existente, dict):
                resultado[indice] = mesclar_dados(novo_valor, valor_existente)

            elif valor_esta_vazio(valor_existente) and not valor_esta_vazio(novo_valor):
                resultado[indice] = novo_valor

        else:
            resultado.append(novo_valor)

    return resultado


def mesclar_dados(novos_dados, dados_existentes):
    """
    Mescla os novos dados sobre os dados existentes sem sobrescrever informações
    já preenchidas.

    Regras:
        - Novas chaves são adicionadas.
        - Campos vazios nos dados existentes são preenchidos.
        - Campos já preenchidos são preservados.
        - Dicionários e listas são tratados recursivamente.
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
            resultado[chave] = mesclar_dados(novo_valor, valor_existente)

        elif isinstance(novo_valor, list) and isinstance(valor_existente, list):
            resultado[chave] = mesclar_listas(novo_valor, valor_existente)

        elif valor_esta_vazio(valor_existente) and not valor_esta_vazio(novo_valor):
            resultado[chave] = novo_valor

    return resultado


# =============================================================================
# Processamento da pasta
# =============================================================================

def processar_pasta(
    pasta_raiz,
    usar_ocr=True,
    incluir_texto_ocr_csv=False,
    dpi=300,
    apenas_adicionar=True,
):
    """
    Processa todos os PDFs encontrados em uma pasta e em suas subpastas.

    Ao final, gera ou atualiza:
        - JSON consolidado;
        - CSV consolidado;
        - relatório de normalização numérica.

    Retorna:
        dict: resumo completo do processamento.

    Em caso de erro de entrada, retorna:
        {"erro": "mensagem"}
    """
    pasta_raiz = Path(pasta_raiz).resolve()

    caminho_json = pasta_raiz / NOME_ARQUIVO_JSON
    caminho_csv = pasta_raiz / NOME_ARQUIVO_CSV
    caminho_relatorio = pasta_raiz / NOME_ARQUIVO_RELATORIO

    arquivos_pdf = sorted(pasta_raiz.rglob("*.pdf"))

    if not arquivos_pdf:
        return {
            "erro": "Nenhum arquivo PDF foi encontrado na pasta informada."
        }

    dados = carregar_json(caminho_json)

    if not isinstance(dados, dict):
        dados = {}

    quantidade_antes = len(dados)


    # -------------------------------------------------------------------------
    # Inicialização opcional do OCR
    # -------------------------------------------------------------------------

    motor_ocr = None

    if usar_ocr:
        with st.status(
            "Inicializando o mecanismo de OCR (RapidOCR)...",
            expanded=False,
        ) as status_ocr:
            try:
                motor_ocr = obter_motor_ocr()

                status_ocr.update(
                    label="Mecanismo de OCR inicializado.",
                    state="complete",
                )

            except Exception as erro:  # noqa: BLE001
                motor_ocr = None

                status_ocr.update(
                    label=f"Não foi possível iniciar o RapidOCR: {erro}",
                    state="error",
                )

                st.warning(
                    "Não foi possível iniciar o RapidOCR. "
                    "Arquivos PDF sem texto incorporado não poderão ser processados."
                )


    quantidade_total_pdfs = len(arquivos_pdf)

    barra_progresso = st.progress(
        0.0,
        text="Preparando processamento...",
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
    # Processamento individual dos PDFs
    # -------------------------------------------------------------------------

    for indice, caminho_pdf in enumerate(arquivos_pdf, start=1):
        caminho_relativo = caminho_pdf.relative_to(pasta_raiz)

        barra_progresso.progress(
            indice / quantidade_total_pdfs,
            text=f"[{indice}/{quantidade_total_pdfs}] {caminho_pdf.name}",
        )


        # Sem OCR, somente PDFs com texto incorporado podem ser processados.
        if motor_ocr is None:
            try:
                possui_texto = ler_paginas_texto(caminho_pdf) is not None

            except Exception:  # noqa: BLE001
                possui_texto = False

            if not possui_texto:
                contadores["falhas"] += 1

                detalhes_falhas.append(
                    {
                        "arquivo": str(caminho_relativo),
                        "motivo": (
                            "PDF sem texto incorporado e mecanismo de OCR "
                            "indisponível. Ative a opção "
                            "'Usar OCR (RapidOCR)'."
                        ),
                    }
                )

                continue


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
            motivo = f"Erro inesperado: {erro}"
            revisao = []

            traceback.print_exc()


        if not chave_acesso:
            contadores["falhas"] += 1

            detalhes_falhas.append(
                {
                    "arquivo": str(caminho_relativo),
                    "motivo": motivo,
                }
            )

            continue


        registros_para_revisao.extend(revisao)


        if chave_acesso in dados:
            if apenas_adicionar:
                dados[chave_acesso] = mesclar_dados(
                    informacoes_nota,
                    dados[chave_acesso],
                )
            else:
                dados[chave_acesso] = informacoes_nota

            contadores["atualizadas"] += 1

        else:
            dados[chave_acesso] = informacoes_nota
            contadores["inseridas"] += 1


        if not extracao_completa:
            contadores["parciais"] += 1


    # -------------------------------------------------------------------------
    # Salvamento do JSON
    # -------------------------------------------------------------------------

    barra_progresso.progress(
        1.0,
        text="Salvando JSON e CSV...",
    )

    salvar_json(
        dados,
        caminho_json,
    )

    quantidade_depois = len(dados)


    # -------------------------------------------------------------------------
    # Geração do CSV completo a partir do JSON consolidado
    # -------------------------------------------------------------------------

    cabecalhos, linhas = construir_linhas(
        dados,
        incluir_ocr=incluir_texto_ocr_csv,
    )

    salvar_csv(
        caminho_csv,
        cabecalhos,
        linhas,
    )


    # -------------------------------------------------------------------------
    # Relatório de revisão da normalização numérica
    # -------------------------------------------------------------------------

    quantidade_anomalias = None

    if registros_para_revisao:
        quantidade_anomalias = gerar_relatorio(
            registros_para_revisao,
            caminho_relatorio,
        )


    duracao_segundos = time.time() - horario_inicio


    return {
        "pasta_raiz": str(pasta_raiz),
        "caminho_json": str(caminho_json),
        "caminho_csv": str(caminho_csv),
        "caminho_relatorio": str(caminho_relatorio),
        "quantidade_pdfs": quantidade_total_pdfs,
        "inseridas": contadores["inseridas"],
        "atualizadas": contadores["atualizadas"],
        "parciais": contadores["parciais"],
        "falhas": contadores["falhas"],
        "detalhes_falhas": detalhes_falhas,
        "quantidade_antes": quantidade_antes,
        "quantidade_depois": quantidade_depois,
        "linhas_csv": len(linhas),
        "colunas_csv": len(cabecalhos),
        "anomalias": quantidade_anomalias,
        "duracao_segundos": duracao_segundos,
    }


# =============================================================================
# Interface Streamlit
# =============================================================================

def mostrar_estado_atual(pasta_raiz):
    """Mostra um resumo dos arquivos existentes antes do processamento."""
    pasta_raiz = Path(pasta_raiz)

    arquivos_pdf = list(pasta_raiz.rglob("*.pdf"))

    caminho_json = pasta_raiz / NOME_ARQUIVO_JSON
    caminho_csv = pasta_raiz / NOME_ARQUIVO_CSV

    coluna_pdf, coluna_json, coluna_csv = st.columns(3)

    coluna_pdf.metric(
        "📄 PDFs na pasta",
        len(arquivos_pdf),
    )

    coluna_json.metric(
        "JSON existente",
        (
            f"{caminho_json.stat().st_size / 1024:.0f} kB"
            if caminho_json.exists()
            else "—"
        ),
    )

    coluna_csv.metric(
        "CSV existente",
        (
            f"{caminho_csv.stat().st_size / 1024:.0f} kB"
            if caminho_csv.exists()
            else "—"
        ),
    )


    if caminho_json.exists():
        dados_existentes = carregar_json(caminho_json)

        quantidade_chaves = (
            len(dados_existentes)
            if isinstance(dados_existentes, dict)
            else 0
        )

        st.caption(
            f"O JSON já possui **{quantidade_chaves}** chaves de acesso. "
            "Ao executar no modo de preservação, somente campos ausentes "
            "serão adicionados."
        )


def mostrar_resultado(resultado):
    """Exibe na interface o resumo e os arquivos resultantes do processamento."""
    if resultado.get("erro"):
        st.warning(
            f"⚠️ {resultado['erro']}"
        )
        return


    st.success(
        f"✔ Processamento concluído em "
        f"{resultado['duracao_segundos']:.1f} s. "
        "Os arquivos JSON e CSV foram salvos na pasta informada."
    )


    st.write(
        "**Arquivos gerados ou atualizados:**"
    )

    st.write(
        f"- 📄 `{resultado['caminho_json']}`"
    )

    st.write(
        f"- 📊 `{resultado['caminho_csv']}`"
    )


    caminho_relatorio = Path(
        resultado["caminho_relatorio"]
    )

    if caminho_relatorio.exists():
        st.write(
            f"- 📝 `{resultado['caminho_relatorio']}`"
        )


    coluna_1, coluna_2, coluna_3, coluna_4, coluna_5 = st.columns(5)

    coluna_1.metric(
        "PDFs encontrados",
        resultado["quantidade_pdfs"],
    )

    coluna_2.metric(
        "Novas chaves",
        resultado["inseridas"],
    )

    coluna_3.metric(
        "Já existentes",
        resultado["atualizadas"],
    )

    coluna_4.metric(
        "Extrações parciais",
        resultado["parciais"],
    )

    coluna_5.metric(
        "Falhas",
        resultado["falhas"],
    )


    st.info(
        f"Chaves no JSON: "
        f"**{resultado['quantidade_antes']} → "
        f"{resultado['quantidade_depois']}** | "
        f"CSV: **{resultado['linhas_csv']} linhas × "
        f"{resultado['colunas_csv']} colunas**."
    )


    if resultado["anomalias"] is not None:
        st.warning(
            f"{resultado['anomalias']} valores numéricos precisam de "
            f"revisão manual. Consulte "
            f"`{Path(resultado['caminho_relatorio']).name}`."
        )


    aba_resumo, aba_json, aba_csv, aba_falhas = st.tabs(
        [
            "Resumo dos PDFs",
            "Visualizar JSON",
            "Visualizar CSV",
            "Falhas",
        ]
    )


    # -------------------------------------------------------------------------
    # Aba de resumo
    # -------------------------------------------------------------------------

    with aba_resumo:
        if resultado["quantidade_pdfs"]:
            tabela_resumo = pd.DataFrame(
                [
                    {
                        "PDFs encontrados": resultado["quantidade_pdfs"],
                        "Extraídos com chave válida": (
                            resultado["quantidade_pdfs"]
                            - resultado["falhas"]
                        ),
                        "Novas chaves": resultado["inseridas"],
                        "Já existentes / mescladas": resultado["atualizadas"],
                        "Extrações parciais": resultado["parciais"],
                        "Não processados": resultado["falhas"],
                    }
                ]
            )

            st.dataframe(
                tabela_resumo,
                width="stretch",
                hide_index=True,
            )


    # -------------------------------------------------------------------------
    # Aba JSON
    # -------------------------------------------------------------------------

    with aba_json:
        try:
            with open(
                resultado["caminho_json"],
                "r",
                encoding="utf-8",
            ) as arquivo_json:
                dados_json = json.load(
                    arquivo_json
                )

        except Exception as erro:  # noqa: BLE001
            st.error(
                f"Não foi possível ler o arquivo JSON: {erro}"
            )

        else:
            st.json(
                dados_json
            )


    # -------------------------------------------------------------------------
    # Aba CSV
    # -------------------------------------------------------------------------

    with aba_csv:
        try:
            tabela_csv = pd.read_csv(
                resultado["caminho_csv"],
                sep=";",
                encoding="utf-8-sig",
                dtype=str,
            )

        except Exception as erro:  # noqa: BLE001
            st.error(
                f"Não foi possível ler o arquivo CSV: {erro}"
            )

        else:
            st.dataframe(
                tabela_csv,
                width="stretch",
                hide_index=True,
            )


    # -------------------------------------------------------------------------
    # Aba de falhas
    # -------------------------------------------------------------------------

    with aba_falhas:
        if resultado["detalhes_falhas"]:
            st.dataframe(
                pd.DataFrame(
                    resultado["detalhes_falhas"]
                ),
                width="stretch",
                hide_index=True,
            )

        else:
            st.success(
                "Nenhuma falha: todos os PDFs foram processados."
            )


    # -------------------------------------------------------------------------
    # Downloads
    # -------------------------------------------------------------------------

    st.divider()

    coluna_download_json, coluna_download_csv = st.columns(2)


    with coluna_download_json:
        st.download_button(
            "⬇ Baixar JSON",
            data=Path(
                resultado["caminho_json"]
            ).read_bytes(),
            file_name=Path(
                resultado["caminho_json"]
            ).name,
            mime="application/json",
            key="download_json",
        )


    with coluna_download_csv:
        st.download_button(
            "⬇ Baixar CSV",
            data=Path(
                resultado["caminho_csv"]
            ).read_bytes(),
            file_name=Path(
                resultado["caminho_csv"]
            ).name,
            mime="text/csv",
            key="download_csv",
        )


# =============================================================================
# Aplicação principal
# =============================================================================

def main():
    """Inicializa e executa a interface Streamlit."""
    st.set_page_config(
        page_title="Leitor de Notas Fiscais - PDF para JSON e CSV",
        page_icon="🧾",
        layout="wide",
    )


    st.title(
        "🧾 Leitor de Notas Fiscais — PDF → JSON + CSV"
    )


    st.caption(
        "Lê uma pasta com arquivos PDF de NF-e/DANFE, extrai os dados e salva "
        f"`{NOME_ARQUIVO_JSON}`, `{NOME_ARQUIVO_CSV}` e "
        f"`{NOME_ARQUIVO_RELATORIO}` **na mesma pasta**. "
        "Se os arquivos já existirem, o modo padrão preserva os valores "
        "preenchidos e adiciona apenas as informações ausentes."
    )


    # -------------------------------------------------------------------------
    # Barra lateral
    # -------------------------------------------------------------------------

    with st.sidebar:
        st.subheader(
            "⚙️ Opções de extração"
        )


        usar_ocr = st.checkbox(
            "Usar OCR (RapidOCR)",
            value=True,
            help=(
                "Necessário para arquivos PDF digitalizados ou sem texto "
                "incorporado."
            ),
        )


        apenas_adicionar = st.checkbox(
            "Adicionar apenas campos ausentes",
            value=True,
            help=(
                "Quando ativado, valores já existentes não são sobrescritos. "
                "Quando desativado, registros com chaves já existentes são "
                "substituídos pela nova extração."
            ),
        )


        incluir_texto_ocr_csv = st.checkbox(
            "Incluir a coluna 'texto_ocr' no CSV",
            value=False,
            help=(
                "Adiciona ao final do CSV o texto completo extraído por OCR "
                "ou obtido diretamente do PDF."
            ),
        )


        dpi = st.number_input(
            "DPI do OCR",
            min_value=150,
            max_value=600,
            value=300,
            step=50,
            help=(
                "Resolução utilizada para rasterizar cada página antes da "
                "execução do OCR."
            ),
        )


    # -------------------------------------------------------------------------
    # Seleção da pasta
    # -------------------------------------------------------------------------

    caminho_informado = st.text_input(
        "📁 Caminho da pasta com os PDFs",
        value=str(DIRETORIO_BASE),
        help=(
            "As subpastas também serão pesquisadas."
        ),
    )


    executar = st.button(
        "▶ Executar",
        type="primary",
        width="stretch",
    )


    caminho_informado = (
        caminho_informado
        or ""
    ).strip().strip('"').strip("'")


    if not caminho_informado:
        st.info(
            "Informe o caminho de uma pasta contendo arquivos PDF de notas "
            "fiscais e clique em **Executar**."
        )
        return


    pasta_raiz = Path(
        caminho_informado
    )


    if not pasta_raiz.is_dir():
        st.error(
            f"❌ A pasta informada não existe: `{caminho_informado}`"
        )
        return


    # Se o caminho foi alterado, descarta o resultado anterior armazenado
    # na sessão do Streamlit.
    if st.session_state.get(
        "pasta_anterior"
    ) != str(pasta_raiz):
        st.session_state.pop(
            "resultado_processamento",
            None,
        )


    st.session_state[
        "pasta_anterior"
    ] = str(pasta_raiz)


    mostrar_estado_atual(
        pasta_raiz
    )


    # -------------------------------------------------------------------------
    # Execução
    # -------------------------------------------------------------------------

    if executar:
        with st.spinner(
            "Processando a pasta..."
        ):
            st.session_state[
                "resultado_processamento"
            ] = processar_pasta(
                pasta_raiz,
                usar_ocr=usar_ocr,
                incluir_texto_ocr_csv=incluir_texto_ocr_csv,
                dpi=int(dpi),
                apenas_adicionar=apenas_adicionar,
            )


    # -------------------------------------------------------------------------
    # Resultado persistido durante a sessão
    # -------------------------------------------------------------------------

    if st.session_state.get(
        "resultado_processamento"
    ):
        mostrar_resultado(
            st.session_state[
                "resultado_processamento"
            ]
        )


if __name__ == "__main__":
    main()
