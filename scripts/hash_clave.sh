#!/usr/bin/env bash
# Genera el hash bcrypt de una clave para NODERED_ADMIN_HASH o NODERED_DASH_HASH.
# Uso (en la carpeta del repo, en la Pi):   bash scripts/hash_clave.sh
# La clave no queda en el historial de la terminal ni en ningun archivo.
set -euo pipefail
read -rsp "Clave (minimo 8 caracteres): " c; echo
read -rsp "Repitela: " c2; echo
[ "$c" = "$c2" ] || { echo "Las claves no coinciden." >&2; exit 1; }
[ "${#c}" -ge 8 ] || { echo "Minimo 8 caracteres." >&2; exit 1; }
hash=$(printf '%s' "$c" | docker compose run --rm --no-deps -T --entrypoint node nodered \
  -e "process.stdout.write(require('bcryptjs').hashSync(require('fs').readFileSync(0,'utf8'),10))")
unset c c2
echo "Pega en .env, con comillas simples:"
echo "  ...._HASH='$hash'"
