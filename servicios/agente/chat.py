#!/usr/bin/env python3
"""
Chat con el agente desde una terminal (sin Telegram).

    docker compose exec -it agente python chat.py        # dentro del stack
    python3 chat.py                                      # fuera, con CONTROL_URL, MONGO_URI y OLLAMA_BASE_URL

Una frase por linea; "salir" o Ctrl+D termina.
"""
import logging
import os
import sys

logging.basicConfig(level=os.environ.get("LOG_NIVEL", "WARNING"),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agente  # noqa: E402

CHAT_ID = "terminal"


def main():
    print("Asistente de la cama de vermicompost. Ejemplos: «¿cómo está la zona 1?», "
          "«abre la válvula de la zona 2 por 3 minutos», «pon la zona 3 en PI», «cierra todo».")
    while True:
        try:
            texto = input("tú> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if texto.lower() in {"salir", "q", "exit"}:
            break
        if texto:
            print("agente>", agente.procesar(CHAT_ID, texto), flush=True)


if __name__ == "__main__":
    main()
