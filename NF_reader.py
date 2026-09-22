# -*- coding: utf-8 -*-
"""
NF_reader.py - Utilidad unica para las Notas Fiscais (NF-e / DANFE).

Hace en un solo comando el flujo completo:
    1) EXTRAE el JSON acumulador desde los PDFs (core/extrair_notas.py).
       - Durante la extraccion se normalizan los numeros a formato BR
         ('.' milhar, ',' decimal): se guardan SOLO como numero.
    2) GENERA el CSV tabulado (core/generar_csv.py) con los numeros sin
       separador de milhar y decimal con coma (locale pt-BR).

Uso:
    python NF_reader.py [--raiz PASTA] [--json notas_fiscales.json]
                        [--csv notas_fiscales.csv] [--dpi 300]
                        [--so-novas] [--sem-ocr] [--incluir-ocr]
                        [--solo-json] [--solo-csv]

Ejemplos:
    python NF_reader.py                # extraer JSON + generar CSV (todo)
    python NF_reader.py --solo-csv     # solo regenerar el CSV desde el JSON
    python NF_reader.py --solo-json    # solo extraer/actualizar el JSON

Requisitos:  pip install pymupdf opencv-python rapidocr-onnxruntime
"""

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

RAIZ = Path(__file__).resolve().parent
CORE = RAIZ / "core"
for _p in (str(RAIZ), str(CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _motor_ocr_disponible():
    """Comprueba si el motor OCR se puede iniciar (sin ejecutar el OCR)."""
    try:
        from core import extrair_notas
        extrair_notas.obter_engine_ocr()
        return True
    except ImportError:
        return False
    except Exception:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extrae el JSON de los PDF y genera el CSV en un solo paso.")
    parser.add_argument("--raiz", default=None,
                        help="Pasta raiz con los PDF (por omision: esta carpeta).")
    parser.add_argument("--json", dest="json_path",
                        default=str(RAIZ / "notas_fiscales.json"),
                        help="Archivo JSON acumulador.")
    parser.add_argument("--csv", dest="csv_path",
                        default=str(RAIZ / "notas_fiscales.csv"),
                        help="Archivo CSV de salida.")
    parser.add_argument("--dpi", type=int, default=300,
                        help="Resolucion para OCR (por omision: 300).")
    parser.add_argument("--so-novas", action="store_true",
                        help="NAO atualizar chaves ja existentes no JSON.")
    parser.add_argument("--sem-ocr", action="store_true",
                        help="NAO usar OCR (so PDFs com texto embebido).")
    parser.add_argument("--incluir-ocr", action="store_true",
                        help="Agregar a columna 'texto_ocr' ao final do CSV.")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--solo-json", action="store_true",
                       help="So extraer/atualizar o JSON (sem CSV).")
    grupo.add_argument("--solo-csv", action="store_true",
                       help="So gerar o CSV desde o JSON existente.")
    args = parser.parse_args(argv)

    raiz = Path(args.raiz).resolve() if args.raiz else RAIZ
    json_path = Path(args.json_path)
    if not json_path.is_absolute():
        json_path = raiz / json_path
    csv_path = Path(args.csv_path)
    if not csv_path.is_absolute():
        csv_path = raiz / csv_path

    # ------------------------------------------------------------------
    # 1) EXTRACCION del JSON
    # ------------------------------------------------------------------
    if args.solo_csv:
        print("=" * 78)
        print(" MODO --solo-csv: se omite la extraccion del JSON")
        print("=" * 78)
    else:
        sem_ocr = args.sem_ocr
        if not sem_ocr and not _motor_ocr_disponible():
            print("  !! Motor OCR no disponible; se usara --sem-ocr "
                  "(los PDF escaneados no se procesaran).")
            sem_ocr = True

        from core import extrair_notas
        argv_extra = ["--raiz", str(raiz), "--json", str(json_path)]
        argv_extra += ["--dpi", str(args.dpi)]
        if args.so_novas:
            argv_extra.append("--so-novas")
        if sem_ocr:
            argv_extra.append("--sem-ocr")
        extrair_notas.main(argv_extra)

    # ------------------------------------------------------------------
    # 2) GENERACION del CSV
    # ------------------------------------------------------------------
    if args.solo_json:
        print("=" * 78)
        print(" MODO --solo-json: se omite la generacion del CSV")
        print("=" * 78)
    else:
        from core import generar_csv
        argv_csv = ["--json", str(json_path), "--csv", str(csv_path)]
        if args.incluir_ocr:
            argv_csv.append("--incluir-ocr")
        generar_csv.main(argv_csv)

    # ------------------------------------------------------------------
    print("=" * 78)
    print(" NF_READER FINALIZADO")
    print(f"  JSON: {json_path}")
    print(f"  CSV : {csv_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())