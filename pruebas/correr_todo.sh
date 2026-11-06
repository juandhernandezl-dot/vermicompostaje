#!/usr/bin/env bash
# Todas las pruebas que no necesitan hardware ni Docker. En la Pi:
#   sudo apt install -y g++ python3-serial python3-pymongo python3-httpx
#   pip install --break-system-packages mongomock     # solo para la prueba integrada
#   bash pruebas/correr_todo.sh
set -uo pipefail
cd "$(dirname "$0")/.."
fallos=0
correr() { echo; echo "=== $1"; shift; "$@" || fallos=$((fallos + 1)); }
correr "simulador v3"        bash -c "cd simulador && python3 vermicompost_mz.py --test"
correr "planta simulada"     bash -c "cd simulador && python3 planta_simulada.py --test"
correr "prueba integrada"    python3 pruebas/prueba_integrada.py
correr "servicio de voz"     python3 pruebas/prueba_voz.py
echo
[ "$fallos" -eq 0 ] && echo "TODO CORRECTO" || echo "FALLARON $fallos BLOQUE(S)"
exit "$fallos"
