# -*- coding: utf-8 -*-
"""Validacion: los campos numericos del JSON quedaron como numeros reales.

Uso:
    python core/validar_numeros.py [--json notas_fiscales.json]
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser(description="Valida los tipos numericos del JSON.")
parser.add_argument("--json", dest="json_path", default=str(BASE / "notas_fiscales.json"))
args = parser.parse_args()

json_path = Path(args.json_path)
if not json_path.is_absolute():
    json_path = BASE / json_path

sys.stdout.reconfigure(encoding="utf-8")

CAMPOS_NOTA = ["valor_da_nota", "peso_bruto", "peso_liquido"]
CAMPOS_PROD = ["quantidade", "valor_unitario", "desconto", "valor_total",
               "bc_icms", "valor_icms", "valor_ipi", "aliquota"]

with open(json_path, encoding="utf-8") as f:
    datos = json.load(f)

num_notas = 0
num_prods = 0
errores = []
for chave, nota in datos.items():
    num_notas += 1
    arch = nota.get("arquivo", "?")
    for c in CAMPOS_NOTA:
        v = nota.get(c)
        if v not in (None, "") and not isinstance(v, (int, float)):
            errores.append(f"{arch} nota.{c} = {v!r} ({type(v).__name__})")
    for p in nota.get("produtos") or []:
        num_prods += 1
        for c in CAMPOS_PROD:
            v = p.get(c)
            if v not in (None, "") and not isinstance(v, (int, float)):
                errores.append(f"{arch} prod.{c} = {v!r} ({type(v).__name__})")

print(f"Notas: {num_notas} | Productos: {num_prods}")
if errores:
    print("ERRORES DE TIPO:")
    for e in errores:
        print("  -", e)
    sys.exit(1)
print("VALIDACION OK: todos los campos numericos son int/float; "
      "no quedan separadores de milhar en el JSON.")