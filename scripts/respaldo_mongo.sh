#!/usr/bin/env bash
# Respaldo de la base del vermicompost. Los datos de la tesis viven en una
# microSD: copiarlos fuera es la unica proteccion real contra su desgaste.
#   bash scripts/respaldo_mongo.sh                 # deja el .gz en ~/respaldos_vermi
# Programado cada noche (crontab -e):
#   15 2 * * * bash /home/gitcode1/vermicompostaje/scripts/respaldo_mongo.sh
# Restaurar:
#   docker exec -i mongodb mongorestore --archive --gzip --drop < archivo.gz
set -euo pipefail
DESTINO="${1:-$HOME/respaldos_vermi}"
mkdir -p "$DESTINO"
ARCHIVO="$DESTINO/vermicompost_$(date +%Y-%m-%d_%H%M).gz"
docker exec mongodb mongodump --db vermicompost --archive --gzip > "$ARCHIVO"
echo "Respaldo: $ARCHIVO ($(du -h "$ARCHIVO" | cut -f1))"
# Conserva los 14 mas recientes.
ls -1t "$DESTINO"/vermicompost_*.gz | tail -n +15 | xargs -r rm --
