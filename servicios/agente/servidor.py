#!/usr/bin/env python3
"""
API HTTP del agente para el dashboard de Node-RED (el mismo pipeline que chat.py).

    POST /chat   {"chat_id": "<8-64 de [A-Za-z0-9_-]>", "texto": "..."}
                 -> 200 {"respuesta": "...", "confirmar_s": 57 | null}
                    confirmar_s: segundos que quedan para responder SI/NO a una orden
                    pendiente (el dashboard muestra los botones mientras dure)
    GET  /salud  -> 200 {"ok": true}

Solo escucha en la red interna de Docker (el compose NO publica su puerto):
quien llega aqui es Node-RED, y a Node-RED se entra con contraseña.
Un hilo por peticion: una pregunta lenta al LLM no deja en cola un
"cierra todo" (las ordenes nunca pasan por el LLM).
"""
import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=os.environ.get("LOG_NIVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agente  # noqa: E402
import comandos  # noqa: E402

log = logging.getLogger("servidor")
PUERTO = int(os.environ.get("AGENTE_PUERTO", "8200"))
MAX_CUERPO = 4096
MAX_TEXTO = 500
_CHAT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class Manejador(BaseHTTPRequestHandler):
    server_version = "agente"

    def _responder(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path.rstrip("/") == "/salud":
            return self._responder(200, {"ok": True})
        self._responder(404, {"error": "ruta desconocida"})

    def do_POST(self):
        if self.path.rstrip("/") != "/chat":
            return self._responder(404, {"error": "ruta desconocida"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_CUERPO:
            return self._responder(413, {"error": f"cuerpo vacio o mayor de {MAX_CUERPO} bytes"})
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._responder(400, {"error": "el cuerpo no es JSON valido"})
        if not isinstance(d, dict):
            return self._responder(400, {"error": "se esperaba un objeto JSON"})
        chat_id, texto = d.get("chat_id"), d.get("texto")
        if not isinstance(chat_id, str) or not _CHAT_ID.match(chat_id):
            return self._responder(400, {"error": "chat_id invalido (8-64 caracteres A-Z, a-z, 0-9, _ o -)"})
        if not isinstance(texto, str) or not texto.strip():
            return self._responder(400, {"error": "texto vacio"})
        texto = texto.strip()
        if len(texto) > MAX_TEXTO:
            return self._responder(400, {"error": f"texto de mas de {MAX_TEXTO} caracteres"})
        try:
            # Prefijo "web:" para no compartir confirmaciones con la terminal.
            clave = "web:" + chat_id
            respuesta = agente.procesar(clave, texto)
        except Exception:                                      # noqa: BLE001
            log.exception("fallo procesando un mensaje del dashboard")
            return self._responder(500, {"error": "el agente fallo; revisa los logs del contenedor"})
        self._responder(200, {"respuesta": respuesta, "confirmar_s": comandos.pendiente_s(clave)})

    def log_message(self, fmt, *args):
        if "/salud" not in str(args[:1]):                      # el healthcheck llamaria cada 30 s
            log.info("%s %s", self.address_string(), fmt % args)


def main():
    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    servidor.daemon_threads = True
    log.info("API del agente en :%d (POST /chat, GET /salud)", PUERTO)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
