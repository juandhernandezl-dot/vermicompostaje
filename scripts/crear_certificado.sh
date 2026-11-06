#!/usr/bin/env bash
# Crea el certificado HTTPS (autofirmado) para Node-RED. Hace falta HTTPS para que el
# navegador deje usar el micrófono del Asistente.
#
#   bash scripts/crear_certificado.sh                 # usa el nombre y la IP de esta Pi
#   bash scripts/crear_certificado.sh 192.168.1.30 huerto.local   # nombres/IP extra
#
# Deja cert.pem y key.pem en $DATOS_DIR/certs (fuera del repo: la llave no se sube a git).
# Luego:  docker compose up -d --force-recreate nodered
# La primera vez, cada celular/PC mostrará "la conexión no es privada": es normal con un
# certificado propio (Avanzado -> Continuar). Entrar por la IP o el nombre que pusiste aquí.
set -euo pipefail
cd "$(dirname "$0")/.."
DATOS_DIR="${DATOS_DIR:-$(grep -E '^DATOS_DIR=' .env 2>/dev/null | cut -d= -f2- || true)}"
DATOS_DIR="${DATOS_DIR:-../vermi-datos}"
DESTINO="$DATOS_DIR/certs"
mkdir -p "$DESTINO"
if [ ! -w "$DESTINO" ]; then
  echo "No puedo escribir en $DESTINO (Docker la creó como root al levantar el stack)."
  echo "Arréglalo con:  sudo chown -R \$USER '$DESTINO'   y vuelve a correr este script."
  exit 1
fi

SAN="DNS:localhost,IP:127.0.0.1,DNS:$(hostname),DNS:$(hostname).local"
for ip in $(hostname -I 2>/dev/null); do SAN="$SAN,IP:$ip"; done
for extra in "$@"; do
  if [[ "$extra" =~ ^[0-9.]+$ ]]; then SAN="$SAN,IP:$extra"; else SAN="$SAN,DNS:$extra"; fi
done

openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$DESTINO/key.pem" -out "$DESTINO/cert.pem" \
  -subj "/CN=$(hostname)" -addext "subjectAltName=$SAN" 2>/dev/null
chmod 600 "$DESTINO/key.pem"
echo "Certificado creado en $DESTINO (vale 825 días)."
echo "Nombres e IP que cubre: $SAN"
echo "Ahora:  docker compose up -d --force-recreate nodered   y entra por  https://<IP o nombre>:<puerto>/dashboard"
