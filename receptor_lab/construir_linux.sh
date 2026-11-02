#!/usr/bin/env bash
# Construye el receptor como ejecutable de Linux (Ubuntu 22.04) y crea dos
# accesos en el menu de aplicaciones: "Receptor vermicompost" (huerto) y
# "Receptor vermicompost (simulacion)". Uso:  bash construir_linux.sh
set -euo pipefail
cd "$(dirname "$0")"
AQUI="$(pwd)"

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Falta el modulo venv: sudo apt install python3-venv"; exit 1
fi
# Entorno propio: el python3-paho-mqtt de Ubuntu 22.04 es la 1.6 y el receptor
# esta escrito para la 2.x (aunque tolera ambas). Asi no se toca el sistema.
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet "paho-mqtt>=2,<3" pyinstaller
.venv/bin/pyinstaller --noconfirm --onefile --name ReceptorVermicompost receptor_vermicompost.py

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
for modo in huerto sim; do
  if [ "$modo" = sim ]; then nombre="Receptor vermicompost (simulación)"; arg=" --sim"; else nombre="Receptor vermicompost"; arg=""; fi
  cat > "$APPS/receptor-vermicompost-$modo.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=$nombre
Comment=Lecturas en tiempo real y lotes de la cama de vermicompost (MQTT)
Exec="$AQUI/dist/ReceptorVermicompost"$arg
Icon=$AQUI/icono.ico
Terminal=false
Categories=Science;Utility;
DESK
done
echo
echo "Listo: $AQUI/dist/ReceptorVermicompost"
echo "Busca 'Receptor vermicompost' en el menu de aplicaciones, o ejecuta:"
echo "  ./dist/ReceptorVermicompost          (huerto)"
echo "  ./dist/ReceptorVermicompost --sim    (planta simulada de la Pi)"
