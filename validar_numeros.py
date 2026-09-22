# -*- coding: utf-8 -*-
"""Validacion: los campos numericos del JSON quedaron como numeros reales."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

CAMPOS_NOTA = ["valor_da_nota", "peso_bruto", "peso_liquido"]
CAMPOS_PROD = ["quantidade", "valor_unitario", "desconto", "valor_total",
               "bc_icms", "valor_icms", "valor_ipi", "aliquota"]

with open("notas_fiscales.json", encoding="utf-8") as f:
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