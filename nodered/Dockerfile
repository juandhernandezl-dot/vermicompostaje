# Node-RED con los nodos del dashboard y de MongoDB ya instalados en la imagen.
# Asi la carpeta nodered/ del repo (montada en /data) solo guarda lo que es
# codigo -flows.json, settings.js, package.json- y no 230 MB de node_modules.
FROM nodered/node-red:latest
RUN npm install --no-audit --no-fund --omit=dev \
      @flowfuse/node-red-dashboard@~1.33.0 \
      node-red-contrib-mongodb4@~3.4.0
