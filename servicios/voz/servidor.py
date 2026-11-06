#!/usr/bin/env python3
"""
API interna de voz para el dashboard (la llama Node-RED; el compose NO publica el puerto).

    POST /transcribir   cuerpo = audio crudo (webm/ogg/mp4/wav...), hasta 3 MB y 30 s
                        -> 200 {"texto": "..."}      ("" si no se oyo habla)
                           400 vacio | 413 muy grande o muy largo | 415 audio ilegible
                           503 {"error": "cargando"}  modelos aun descargandose
    POST /hablar        {"texto": "..."}  (se recorta a 600 caracteres)
                        -> 200 audio/wav  | 400 texto vacio | 503 cargando
    GET  /salud         -> 200 {"ok": true, "stt": bool, "tts": bool}
                           (200 aunque los modelos sigan bajando: el servidor esta vivo)

Los modelos se cargan en un hilo aparte: la primera vez se bajan (~0.5 GB) y,
si no hay internet, se reintenta cada minuto sin tumbar el servicio.
Un solo trabajo pesado a la vez (candado): la Pi no tiene CPU para dos.
VOZ_MOTOR=falso (solo pruebas) sustituye los modelos por unos de mentira.
"""
import io
import json
import logging
import math
import os
import re
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=os.environ.get("LOG_NIVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("voz")

PUERTO = int(os.environ.get("VOZ_PUERTO", "8300"))
MOTOR = os.environ.get("VOZ_MOTOR", "real")
MODELOS = Path(os.environ.get("VOZ_MODELOS", "/modelos"))
WHISPER = os.environ.get("VOZ_WHISPER", "small")
PIPER_VOZ = os.environ.get("VOZ_PIPER", "es_MX-claude-high")
MAX_AUDIO_BYTES = 3 * 1024 * 1024
MAX_AUDIO_S = 30
MAX_TEXTO = 600
# Sesga a Whisper hacia el vocabulario del huerto sin contener ninguna orden
# completa: si oyera silencio y "alucinara" la pista, no saldria una orden.
PISTA = "Conversación con el asistente del huerto: válvulas, zonas 1, 2 y 3, riego, humedad."


class AudioIlegible(Exception):
    pass


class AudioLargo(Exception):
    pass


class NoListo(Exception):
    pass


def limpiar_para_voz(texto: str) -> str:
    """Lo que se lee mal en voz alta (simbolos) pasa a palabras; recorta a MAX_TEXTO."""
    t = texto.replace("°C", " grados").replace("%", " por ciento")
    t = t.replace("«", "").replace("»", "").replace("*", "").replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_TEXTO:
        corte = t[:MAX_TEXTO]
        punto = max(corte.rfind(". "), corte.rfind("? "), corte.rfind("! "))
        t = corte[:punto + 1] if punto > 200 else corte
    return t


class MotorReal:
    def __init__(self):
        self.stt = None
        self.tts = None
        self._candado = threading.Lock()
        threading.Thread(target=self._cargar, daemon=True).start()

    def _cargar(self):
        while not (self.stt and self.tts):
            try:
                if not self.stt:
                    from faster_whisper import WhisperModel
                    log.info("Cargando Whisper '%s' (la primera vez se descarga)...", WHISPER)
                    self.stt = WhisperModel(WHISPER, device="cpu", compute_type="int8",
                                            download_root=str(MODELOS / "whisper"))
                    log.info("Whisper listo")
                if not self.tts:
                    from piper import PiperVoice
                    from piper.download_voices import download_voice
                    carpeta = MODELOS / "piper"
                    carpeta.mkdir(parents=True, exist_ok=True)
                    modelo = carpeta / (PIPER_VOZ + ".onnx")
                    if not modelo.exists():
                        log.info("Descargando la voz de Piper '%s'...", PIPER_VOZ)
                        download_voice(PIPER_VOZ, carpeta)
                    self.tts = PiperVoice.load(modelo)
                    log.info("Voz de Piper lista")
            except Exception:                                  # noqa: BLE001
                log.exception("No se pudieron cargar los modelos de voz; reintento en 60 s")
                time.sleep(60)

    @property
    def listo(self):
        return {"stt": self.stt is not None, "tts": self.tts is not None}

    def transcribir(self, datos: bytes) -> str:
        if not self.stt:
            raise NoListo()
        with self._candado:
            try:
                segmentos, info = self.stt.transcribe(
                    io.BytesIO(datos), language="es", beam_size=1, vad_filter=True,
                    condition_on_previous_text=False, temperature=0.0, initial_prompt=PISTA)
            except Exception as e:                             # noqa: BLE001
                raise AudioIlegible(str(e)) from e
            if info.duration > MAX_AUDIO_S:
                raise AudioLargo()
            return " ".join(s.text.strip() for s in segmentos).strip()

    def hablar(self, texto: str) -> bytes:
        if not self.tts:
            raise NoListo()
        with self._candado:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                self.tts.synthesize_wav(texto, w)
            return buf.getvalue()


class MotorFalso:
    """Solo para pruebas: 'transcribe' el texto que viaja tras 'TEXTO:' y 'habla' un pitido."""
    listo = {"stt": True, "tts": True}

    def transcribir(self, datos: bytes) -> str:
        if datos.startswith(b"TEXTO:"):
            return datos[6:].decode("utf-8", "replace").strip()
        if datos.startswith(b"ILEGIBLE"):
            raise AudioIlegible("no es audio")
        if datos.startswith(b"LARGO"):
            raise AudioLargo()
        return os.environ.get("VOZ_FALSO_TEXTO", "abre la válvula de la zona 2 por 1 minuto")

    def hablar(self, texto: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                                   for i in range(8000)))
        return buf.getvalue()


motor = MotorFalso() if MOTOR == "falso" else MotorReal()


class Manejador(BaseHTTPRequestHandler):
    server_version = "voz"

    def _json(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer(self, maximo):
        """Cuerpo crudo; None si ya respondio con error."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0:
            self._json(400, {"error": "cuerpo vacio"})
        elif n > maximo:
            # Se lee (y descarta) lo que viene para que el cliente reciba el 413 y no
            # un "conexion cortada"; mas de 16 MB simplemente se corta.
            restante = n if n <= 16 * 1024 * 1024 else 0
            while restante > 0:
                restante -= len(self.rfile.read(min(65536, restante)) or b"\0" * restante)
            self.close_connection = True
            self._json(413, {"error": f"cuerpo mayor de {maximo} bytes"})
        else:
            return self.rfile.read(n)
        return None

    def do_GET(self):
        if self.path.rstrip("/") == "/salud":
            return self._json(200, dict(ok=True, **motor.listo))
        self._json(404, {"error": "ruta desconocida"})

    def do_POST(self):
        ruta = self.path.rstrip("/")
        if ruta == "/transcribir":
            datos = self._leer(MAX_AUDIO_BYTES)
            if datos is None:
                return
            try:
                texto = motor.transcribir(datos)
            except NoListo:
                return self._json(503, {"error": "cargando"})
            except AudioLargo:
                return self._json(413, {"error": f"audio de mas de {MAX_AUDIO_S} s"})
            except AudioIlegible:
                return self._json(415, {"error": "audio ilegible"})
            except Exception:                                  # noqa: BLE001
                log.exception("fallo transcribiendo")
                return self._json(500, {"error": "fallo interno"})
            texto = re.sub(r"[\x00-\x1f\x7f]+", " ", texto).strip()    # sin caracteres de control
            log.info("transcrito (%d bytes): %r", len(datos), texto[:80])
            return self._json(200, {"texto": texto[:500]})
        if ruta == "/hablar":
            crudo = self._leer(8192)
            if crudo is None:
                return
            try:
                d = json.loads(crudo)
                texto = limpiar_para_voz(d["texto"]) if isinstance(d, dict) and isinstance(d.get("texto"), str) else ""
            except (ValueError, UnicodeDecodeError):
                texto = ""
            if not texto:
                return self._json(400, {"error": "texto vacio"})
            try:
                wav = motor.hablar(texto)
            except NoListo:
                return self._json(503, {"error": "cargando"})
            except Exception:                                  # noqa: BLE001
                log.exception("fallo sintetizando")
                return self._json(500, {"error": "fallo interno"})
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
            return
        self._json(404, {"error": "ruta desconocida"})

    def log_message(self, fmt, *args):
        if "/salud" not in str(args[:1]):
            log.info("%s %s", self.address_string(), fmt % args)


def main():
    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    servidor.daemon_threads = True
    log.info("API de voz en :%d (motor %s)", PUERTO, MOTOR)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
