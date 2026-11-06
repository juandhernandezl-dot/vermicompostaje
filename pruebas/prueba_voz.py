#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prueba_voz.py - API HTTP del servicio de voz, con motor de mentira (sin modelos)
================================================================================
Verifica el contrato y los limites de servicios/voz/servidor.py. NO prueba la
calidad de Whisper ni de Piper: eso solo se puede comprobar con los modelos
reales en la Pi (docs/GUIA_PI5.md, "Voz").

    python3 pruebas/prueba_voz.py
"""
import json
import os
import subprocess
import sys
import time
import wave
import io
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUERTO = 17300                       # distinto del 8300 del stack
URL = f"http://127.0.0.1:{PUERTO}"
fallos = []


def ok(cond, msg):
    print(("  ok   " if cond else "  FALLO ") + msg)
    if not cond:
        fallos.append(msg)


def llamar(ruta, cuerpo=None, tipo="application/octet-stream"):
    req = urlreq.Request(URL + ruta, data=cuerpo, method="POST" if cuerpo is not None else "GET",
                         headers={"Content-Type": tipo})
    try:
        with urlreq.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def main():
    env = dict(os.environ, VOZ_MOTOR="falso", VOZ_PUERTO=str(PUERTO), PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, os.path.join(RAIZ, "servicios", "voz", "servidor.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                llamar("/salud"); break
            except (URLError, ConnectionError):
                time.sleep(0.1)
        print("\n[voz] API HTTP")
        s, _, b = llamar("/salud")
        ok(s == 200 and json.loads(b) == {"ok": True, "stt": True, "tts": True}, "GET /salud responde y dice si los modelos estan listos")
        s, _, b = llamar("/transcribir", "TEXTO:cierra todo".encode())
        ok(s == 200 and json.loads(b)["texto"] == "cierra todo", "transcribir devuelve el texto")
        s, _, b = llamar("/transcribir", "TEXTO:" .encode() + ("a" * 900).encode())
        ok(s == 200 and len(json.loads(b)["texto"]) == 500, "el texto transcrito nunca pasa de 500 caracteres (limite del agente)")
        s, _, b = llamar("/transcribir", b"")
        ok(s == 400, "audio vacio -> 400")
        s, _, b = llamar("/transcribir", b"x" * (3 * 1024 * 1024 + 1))
        ok(s == 413, "audio de mas de 3 MB -> 413")
        s, _, b = llamar("/transcribir", b"LARGO")
        ok(s == 413, "audio de mas de 30 s -> 413")
        s, _, b = llamar("/transcribir", b"ILEGIBLE")
        ok(s == 415, "audio que no se puede decodificar -> 415")
        s, tipo, b = llamar("/hablar", json.dumps({"texto": "Zona 1: humedad 26.0 % y 21.6 °C."}).encode(), "application/json")
        ok(s == 200 and tipo == "audio/wav", "hablar devuelve audio/wav")
        w = wave.open(io.BytesIO(b))
        ok(w.getnframes() > 0, "el WAV tiene muestras y se puede abrir")
        for malo in (b"", b"{}", b'{"texto": ""}', b'{"texto": 5}', b"[1]", b"no es json", b'{"texto": "   "}'):
            s, _, _ = llamar("/hablar", malo, "application/json")
            if s != 400:
                ok(False, f"/hablar con {malo!r} debia ser 400 y fue {s}")
                break
        else:
            ok(True, "/hablar rechaza vacio, no-JSON, tipos raros y texto en blanco (400)")
        s, _, _ = llamar("/hablar", b"x" * 9000, "application/json")
        ok(s == 413, "/hablar con cuerpo de mas de 8 KB -> 413")
        s, _, _ = llamar("/otra")
        ok(s == 404, "ruta desconocida -> 404")
        # limpieza de simbolos para voz y recorte (la funcion que usa el servidor real)
        sys.path.insert(0, os.path.join(RAIZ, "servicios", "voz"))
        os.environ["VOZ_MOTOR"] = "falso"
        import servidor
        t = servidor.limpiar_para_voz("Humedad 26.0 % a 21.6 °C. «Responda SÍ o NO».")
        ok("%" not in t and "°" not in t and "«" not in t and "por ciento" in t and "grados" in t,
           "los simbolos se convierten en palabras antes de hablar")
        largo = ("Esta es una frase corta. " * 60)
        t = servidor.limpiar_para_voz(largo)
        ok(len(t) <= 600 and t.endswith("."), "un texto largo se recorta al final de una frase")
    finally:
        p.terminate()
        p.wait(timeout=5)
    print()
    print("PRUEBA DE VOZ: todo correcto." if not fallos else f"PRUEBA DE VOZ: FALLARON {len(fallos)}.")
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
