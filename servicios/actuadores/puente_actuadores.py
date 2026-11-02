#!/usr/bin/env python3
"""
Puente de actuadores - nodo central RPi5 (contenedor Docker)
Proyecto Minciencias 113982 - UAO

Es el UNICO programa que abre el puerto USB del ESP32 de las electrovalvulas.
Los controladores (RL, DMC, PI), Node-RED o el agente conversacional le piden
las acciones por HTTP dentro de la red de Docker; el puente las traduce al
protocolo de texto del firmware (actuador_valvulas.ino).

Responsabilidades:
  1. Mantener viva la comunicacion con el ESP32: PING cada PERIODO_PING_S.
     Si el puente se cae, el watchdog del ESP32 cierra todas las valvulas.
  2. Registrar CADA riego en MongoDB (coleccion "riegos"): valvula, inicio,
     duracion pedida y aplicada, quien lo ordeno, cuando y por que se cerro.
  3. Ofrecer una API HTTP sencilla:
       GET  /estado                       estado de la conexion y de cada valvula
       POST /valvulas/<v>/abrir           {"segundos": 30, "origen": "rl"}
       POST /valvulas/<v>/cerrar          {"origen": "manual"}
       POST /valvulas/cerrar_todo         {"origen": "manual"}
       POST /niveles                      {"niveles": [0, 1, 0.5], "ts": 60, "origen": "rl"}
     /niveles sirve para controladores que deciden un nivel por zona cada Ts
     (0 = cerrada, 1 = abierta todo el periodo, 0.5 = medio periodo): cada nivel
     se convierte en un pulso de nivel*ts segundos.

Al conectar (o reconectar) con el ESP32 se envia CERRAR_TODO: el sistema arranca
siempre desde un estado conocido.
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial
from pymongo import DESCENDING, MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# CONFIGURACION (variables de entorno)
# ---------------------------------------------------------------------------
PUERTO = os.getenv("PUERTO_ESP32", "/dev/esp32")
BAUDIOS = int(os.getenv("BAUDIOS_ESP32", "115200"))
NUM_VALVULAS = int(os.getenv("NUM_VALVULAS", "3"))
NOMBRES = [n.strip() for n in os.getenv(
    "NOMBRES_VALVULAS", "cama1-zona1,cama1-zona2,cama1-zona3").split(",")]
PERIODO_PING_S = float(os.getenv("PERIODO_PING_S", "10"))
ESPERA_RESPUESTA_S = float(os.getenv("ESPERA_RESPUESTA_S", "3"))
SEGUNDOS_MAX_PEDIDO = int(os.getenv("SEGUNDOS_MAX_PEDIDO", "3600"))   # el ESP32 recorta a su maximo
PUERTO_HTTP = int(os.getenv("PUERTO_HTTP", "8000"))
SITIO = os.getenv("SITIO", "huertolab")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017")
MONGO_DB = os.getenv("MONGO_DB", "vermicompost")
MONGO_COLECCION = os.getenv("MONGO_COLECCION_RIEGOS", "riegos")


def log(texto):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {texto}", flush=True)


def ahora_utc():
    return datetime.now(timezone.utc)


def nombre(v):
    return NOMBRES[v - 1] if v - 1 < len(NOMBRES) and NOMBRES[v - 1] else f"valvula{v}"


# ---------------------------------------------------------------------------
# REGISTRO DE RIEGOS EN MONGODB
# ---------------------------------------------------------------------------
class Registro:
    """Un documento por pulso. Si MongoDB falla, el riego sigue funcionando y el
    problema queda en el log (nunca se bloquea una orden por la base de datos)."""

    def __init__(self, uri=MONGO_URI, cliente=None):
        cli = cliente or MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True)
        self.col = cli[MONGO_DB][MONGO_COLECCION]
        try:
            self.col.create_index([("valvula", 1), ("estado", 1)])
            self.col.create_index([("inicio", DESCENDING)])
        except PyMongoError as e:
            log(f"[mongo] aun no disponible ({e}); se reintentara en cada riego")

    def _seguro(self, accion, *args, **kw):
        try:
            return accion(*args, **kw)
        except PyMongoError as e:
            log(f"[mongo] no se pudo registrar: {e}")
            return None

    def abierto(self, v, pedida, aplicada, recortado, origen):
        doc = {"sitio": SITIO, "valvula": v, "nombre": nombre(v), "inicio": ahora_utc(),
               "fecha": datetime.now().strftime("%Y-%m-%d"),
               "duracion_pedida_s": pedida, "duracion_aplicada_s": aplicada,
               "recortado": recortado, "origen": origen, "estado": "abierta"}
        self._seguro(self.col.insert_one, doc)

    def cerrado(self, v, motivo, segundos_reales=None, origen_cierre=None):
        """Cierra el ultimo riego abierto de la valvula v."""
        doc = self._seguro(self.col.find_one, {"valvula": v, "estado": "abierta", "sitio": SITIO},
                           sort=[("inicio", DESCENDING)])
        if not doc:
            return
        fin = ahora_utc()
        if segundos_reales is None:
            segundos_reales = round((fin - doc["inicio"]).total_seconds())
        cambios = {"estado": "cerrada", "fin": fin, "duracion_real_s": segundos_reales,
                   "motivo_cierre": motivo}
        if origen_cierre:
            cambios["origen_cierre"] = origen_cierre
        self._seguro(self.col.update_one, {"_id": doc["_id"]}, {"$set": cambios})

    def cerrar_pendientes(self, motivo):
        """Riegos que quedaron 'abiertos' en la base pero ya no lo estan (reinicio)."""
        for v in range(1, NUM_VALVULAS + 1):
            while True:
                antes = self._seguro(self.col.count_documents,
                                     {"valvula": v, "estado": "abierta", "sitio": SITIO})
                if not antes:
                    break
                self.cerrado(v, motivo)


# ---------------------------------------------------------------------------
# ENLACE CON EL ESP32
# ---------------------------------------------------------------------------
class EnlaceESP32:
    """Abre el puerto, envia ordenes con identificador y empareja las respuestas."""

    def __init__(self, registro, puerto=PUERTO, baudios=BAUDIOS):
        self.registro = registro
        self.puerto, self.baudios = puerto, baudios
        self.ser = None
        self.lock_tx = threading.Lock()
        self.lock_estado = threading.Lock()
        self.pendientes = {}                     # id -> Queue con la respuesta
        self.siguiente_id = 1
        self.conectado = False
        self.ultimo_contacto = None
        self.config_esp32 = ""
        self.abierta = [False] * (NUM_VALVULAS + 1)
        self.origen_cierre = {}                  # v -> origen de un CERRAR en curso
        self._parar = threading.Event()

    # ---- ciclo de vida ----
    def iniciar(self):
        threading.Thread(target=self._mantener, name="enlace", daemon=True).start()
        threading.Thread(target=self._latido, name="latido", daemon=True).start()

    def _abrir_puerto(self):
        # serial_for_url acepta tanto un dispositivo (/dev/esp32) como una URL
        # socket://host:puerto: asi el MISMO puente habla con la planta
        # simulada (docker-compose.sim.yml) sin cambiar nada mas.
        s = serial.serial_for_url(self.puerto, do_not_open=True)
        s.baudrate, s.timeout = self.baudios, 0.2
        # DTR/RTS en reposo: con el circuito de auto-reset del DevKit, abrir el
        # puerto con estas lineas activas reiniciaria el ESP32.
        s.dtr = False
        s.rts = False
        s.open()
        s.reset_input_buffer()
        return s

    def _mantener(self):
        while not self._parar.is_set():
            try:
                self.ser = self._abrir_puerto()
                log(f"[esp32] puerto {self.puerto} abierto a {self.baudios} baudios")
                threading.Thread(target=self._sincronizar, daemon=True).start()
                self._leer()                       # bloquea hasta que se pierda el puerto
            except (serial.SerialException, OSError) as e:
                if self.conectado or self.ser is None:
                    log(f"[esp32] sin puerto ({e}); reintentando cada 3 s")
            self._marcar_desconectado()
            time.sleep(3)

    def _sincronizar(self):
        """Primer contacto: confirma que responde, lee su configuracion y cierra todo."""
        time.sleep(0.5)
        try:
            self.config_esp32 = self.enviar("CONFIG")
            self.enviar("CERRAR_TODO", origen="puente-arranque")
            self.registro.cerrar_pendientes("REINICIO_PUENTE")
            log(f"[esp32] sincronizado: {self.config_esp32}")
        except Exception as e:
            log(f"[esp32] no respondio al sincronizar: {e}")

    def _marcar_desconectado(self):
        if self.conectado:
            log("[esp32] conexion perdida")
        self.conectado = False
        with self.lock_estado:
            for q in self.pendientes.values():
                q.put(None)
            self.pendientes.clear()
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _latido(self):
        while not self._parar.is_set():
            time.sleep(PERIODO_PING_S)
            if self.ser is not None:
                try:
                    self.enviar("PING")
                except Exception as e:
                    log(f"[esp32] PING sin respuesta: {e}")

    # ---- recepcion ----
    def _leer(self):
        buf = b""
        while not self._parar.is_set():
            datos = self.ser.read(256)          # lanza SerialException si se desconecta
            if not datos:
                continue
            buf += datos
            while b"\n" in buf:
                linea, buf = buf.split(b"\n", 1)
                texto = linea.decode(errors="replace").strip()
                if texto:
                    self._procesar_linea(texto)

    def _procesar_linea(self, texto):
        self.ultimo_contacto = time.time()
        if not self.conectado:
            self.conectado = True
            log("[esp32] comunicacion establecida")
        m = re.match(r"^#(\d+)\s+(.*)$", texto)
        if m:
            with self.lock_estado:
                q = self.pendientes.pop(int(m.group(1)), None)
            if q:
                q.put(m.group(2))
            return
        if texto.startswith("EVT CERRADA"):
            partes = texto.split()
            try:
                v, motivo, seg = int(partes[2]), partes[3], int(partes[4])
            except (IndexError, ValueError):
                log(f"[esp32] evento ilegible: {texto}")
                return
            self.abierta[v] = False
            origen = self.origen_cierre.pop(v, None) if motivo == "ORDEN" else None
            self.registro.cerrado(v, motivo, seg, origen)
            log(f"[riego] {nombre(v)} cerrada ({motivo}, {seg} s)")
        elif texto == "EVT INICIO":
            log("[esp32] el ESP32 se reinicio: todas las valvulas cerradas")
            self.abierta = [False] * (NUM_VALVULAS + 1)
            self.registro.cerrar_pendientes("REINICIO_ESP32")
        elif texto == "EVT WATCHDOG":
            log("[esp32] AVISO: el watchdog del ESP32 cerro las valvulas (falta de comunicacion)")
        else:
            log(f"[esp32] {texto}")

    # ---- envio ----
    def enviar(self, orden, origen=None):
        """Envia una orden y espera su respuesta. Devuelve el texto de la respuesta."""
        if self.ser is None:
            raise ConnectionError("el ESP32 no esta conectado")
        with self.lock_estado:
            ident = self.siguiente_id
            self.siguiente_id = self.siguiente_id % 9999 + 1
            q = queue.Queue()
            self.pendientes[ident] = q
        with self.lock_tx:
            self.ser.write(f"#{ident} {orden}\n".encode())
            self.ser.flush()
        try:
            resp = q.get(timeout=ESPERA_RESPUESTA_S)
        except queue.Empty:
            with self.lock_estado:
                self.pendientes.pop(ident, None)
            raise TimeoutError(f"el ESP32 no respondio a '{orden}'")
        if resp is None:
            raise ConnectionError("se perdio la conexion con el ESP32")
        if resp.startswith("ERR"):
            raise ValueError(resp)
        return resp

    # ---- acciones ----
    def abrir(self, v, segundos, origen):
        resp = self.enviar(f"ABRIR {v} {int(segundos)}")
        if self.abierta[v]:
            # El firmware reinicia el pulso sin avisar del cierre del anterior.
            self.registro.cerrado(v, "REEMPLAZADO")
        partes = resp.split()                  # OK ABRIR v aplicado [RECORTADO]
        aplicado = int(partes[3])
        recortado = len(partes) > 4 and partes[4] == "RECORTADO"
        self.abierta[v] = True
        self.registro.abierto(v, int(segundos), aplicado, recortado, origen)
        log(f"[riego] {nombre(v)} abierta {aplicado} s (origen {origen}"
            f"{', RECORTADO' if recortado else ''})")
        return {"valvula": v, "nombre": nombre(v), "segundos_aplicados": aplicado,
                "recortado": recortado}

    def cerrar(self, v, origen):
        if self.abierta[v]:
            self.origen_cierre[v] = origen
        self.enviar(f"CERRAR {v}")
        return {"valvula": v, "nombre": nombre(v), "cerrada": True}

    def cerrar_todo(self, origen):
        for v in range(1, NUM_VALVULAS + 1):
            if self.abierta[v]:
                self.origen_cierre[v] = origen
        self.enviar("CERRAR_TODO")
        return {"cerradas": True}

    def estado(self):
        info = {"conectado": self.conectado and self.ser is not None,
                "ultimo_contacto_s": (round(time.time() - self.ultimo_contacto, 1)
                                      if self.ultimo_contacto else None),
                "config_esp32": self.config_esp32, "valvulas": []}
        restantes = [0] * NUM_VALVULAS
        if info["conectado"]:
            try:
                partes = self.enviar("ESTADO").split()[1:]
                restantes = [int(x) for x in partes][:NUM_VALVULAS]
            except Exception as e:
                info["error"] = str(e)
        for v in range(1, NUM_VALVULAS + 1):
            r = restantes[v - 1] if v - 1 < len(restantes) else 0
            info["valvulas"].append({"valvula": v, "nombre": nombre(v), "abierta": r > 0,
                                     "segundos_restantes": r})
        return info


# ---------------------------------------------------------------------------
# API HTTP
# ---------------------------------------------------------------------------
ENLACE = None


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cuerpo(self):
        largo = int(self.headers.get("Content-Length", 0) or 0)
        if not largo:
            return {}
        try:
            return json.loads(self.rfile.read(largo))
        except ValueError:
            raise ValueError("el cuerpo no es JSON valido")

    def do_GET(self):
        if self.path.rstrip("/") == "/estado":
            self._responder(ENLACE.estado())
        else:
            self._responder({"error": "ruta desconocida"}, 404)

    def do_POST(self):
        try:
            datos = self._cuerpo()
            origen = str(datos.get("origen", "api"))[:40]
            ruta = self.path.rstrip("/")
            m = re.match(r"^/valvulas/(\d+)/(abrir|cerrar)$", ruta)
            if m:
                v = int(m.group(1))
                if not 1 <= v <= NUM_VALVULAS:
                    return self._responder({"error": f"valvula fuera de 1..{NUM_VALVULAS}"}, 400)
                if m.group(2) == "abrir":
                    seg = datos.get("segundos")
                    if not isinstance(seg, (int, float)) or not 1 <= seg <= SEGUNDOS_MAX_PEDIDO:
                        return self._responder(
                            {"error": f"'segundos' debe estar entre 1 y {SEGUNDOS_MAX_PEDIDO}"}, 400)
                    return self._responder(ENLACE.abrir(v, seg, origen))
                return self._responder(ENLACE.cerrar(v, origen))
            if ruta == "/valvulas/cerrar_todo":
                return self._responder(ENLACE.cerrar_todo(origen))
            if ruta == "/niveles":
                niveles, ts = datos.get("niveles"), datos.get("ts", 60)
                if (not isinstance(niveles, list) or not niveles or len(niveles) > NUM_VALVULAS
                        or not isinstance(ts, (int, float)) or not 1 <= ts <= SEGUNDOS_MAX_PEDIDO):
                    return self._responder({"error": "se espera {'niveles': [...], 'ts': segundos}"}, 400)
                resultado = []
                for i, nivel in enumerate(niveles):
                    v = i + 1
                    nivel = max(0.0, min(1.0, float(nivel)))
                    seg = round(nivel * ts)
                    if seg >= 1:
                        resultado.append(ENLACE.abrir(v, seg, origen))
                    elif ENLACE.abierta[v]:
                        resultado.append(ENLACE.cerrar(v, origen))
                return self._responder({"aplicado": resultado})
            return self._responder({"error": "ruta desconocida"}, 404)
        except ValueError as e:
            self._responder({"error": str(e)}, 400)
        except (TimeoutError, ConnectionError) as e:
            self._responder({"error": str(e)}, 503)


def main():
    global ENLACE
    log(f"Puente de actuadores: {NUM_VALVULAS} valvulas en {PUERTO}, API en el puerto {PUERTO_HTTP}")
    ENLACE = EnlaceESP32(Registro())
    ENLACE.iniciar()
    ThreadingHTTPServer(("0.0.0.0", PUERTO_HTTP), Manejador).serve_forever()


if __name__ == "__main__":
    main()
