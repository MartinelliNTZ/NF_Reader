# -*- coding: utf-8 -*-
"""
Aplica la normalizacion numerica BR ('.' milhar, ',' decimal) al JSON
acumulador ya existente, SIN volver a ejecutar el OCR de los PDFs.

Uso:
    python migrar_numeros.py [--json notas_fiscales.json]
                             [--reporte reporte_normalizacion.txt]

- Convierte los campos numericos a numeros (int/float), sin separador
  de milhar.
- Recupera quantidade que quedo en 'unidade'/'valor_unitario' y el
  precio unitario que quedo en 'desconto' (errores de alineacion del OCR).
- Escribe el archivo de reporte con los cambios y las anomalias.
- Es idempotente: si un campo ya es numero se mantiene.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from .normalizar_numeros import (generar_reporte, normalizar_campos_numericos)
except ImportError:  # ejecucion directa: python core/migrar_numeros.py
    from normalizar_numeros import (generar_reporte, normalizar_campos_numericos)


def carregar_json(ruta):
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_json(datos, ruta):
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    tmp.replace(ruta)


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Normaliza los numeros del JSON existente (sin re-OCR).")
    parser.add_argument("--json", dest="json_path", default="notas_fiscales.json")
    parser.add_argument("--reporte", dest="reporte_path",
                        default="reporte_normalizacion.txt")
    args = parser.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    json_path = Path(args.json_path)
    if not json_path.is_absolute():
        json_path = base / json_path
    reporte_path = Path(args.reporte_path)
    if not reporte_path.is_absolute():
        reporte_path = base / reporte_path

    print("=" * 78)
    print(" MIGRACION: NORMALIZACION NUMERICA (JSON -> numeros)")
    print(f" JSON    : {json_path}")
    print(f" Reporte: {reporte_path}")
    print("=" * 78)

    if not json_path.exists():
        print(f"!! NAO existe el JSON: {json_path}")
        return 1

    datos = carregar_json(json_path)
    registros = []
    for chave, nota in datos.items():
        if not isinstance(nota, dict):
            continue
        regs = normalizar_campos_numericos(nota, chave=chave)
        registros.extend(regs)

    salvar_json(datos, json_path)
    n_anom = generar_reporte(registros, reporte_path)

    print("-" * 78)
    print(f" Notas procesadas     : {len(datos)}")
    print(f" Valores procesados   : {len(registros)}")
    print(f" Anomalias a revisar  : {n_anom}")
    print(f" JSON actualizado     : {json_path}")
    print(f" Reporte generado     : {reporte_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())