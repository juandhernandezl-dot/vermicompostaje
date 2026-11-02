#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
planta_simulada.py - LA CAMA DE VERMICOMPOST EN SOFTWARE, CON EL MISMO CONTRATO
==============================================================================

Sustituye al hardware del huerto sin que el resto del sistema se entere:

    puente_actuadores  --socket://planta:7001-->  firmware REAL del ESP32
                                                  (actuador_valvulas.ino
                                                  compilado para el PC)
    emisor_sensores    --socket://planta:7002-->  3 sensores SN-3002 Modbus RTU
                                                  (direcciones 3, 4 y 6)
                                  ^                       |
                                  |   modelo multizona    |
                                  +---- vermicompost_mz <-+  (abre/cierra
                                        (v3, sin cambios)     aspersores)

Los contenedores de sensores, actuadores, control, agente y Node-RED corren
EXACTAMENTE el mismo codigo que en el huerto; solo cambia la URL del puerto
serie (docker-compose.sim.yml). Por eso "probar con el simulador" valida el
camino real, incluido el firmware: el .ino que se flashea es el que se prueba.

TIEMPO
------
Todo el software (pulsos de riego, latido del puente, muestreo, Ts del control)
corre en tiempo REAL. Solo la fisica de la cama se acelera ESCALA veces: con
ESCALA=10, un minuto real equivale a 10 minutos de cama (una valvula abierta
30 s riega lo que regaria en 5 min). Asi una prueba de horas dura minutos sin
descuadrar ningun temporizador del sistema.

UNIDADES
--------
El modelo trabaja en humedad relativa a capacidad de campo (W/W_fc). El sensor
real da % volumetrico (VWC). Aqui: VWC = W/W_fc * VWC_CC, con VWC_CC = 35 %
(el mismo valor por defecto que planta.json). Es la conversion que habra que
calibrar en campo; el simulador la hace explicita en vez de esconderla.

API DE PRUEBAS (HTTP, puerto 7003)
---------------------------------
    GET  /estado                       humedad, temperatura y valvulas por zona
    POST /falla   {"sensor": 6, "activa": true}   ese sensor deja de responder
    POST /reiniciar_esp32                          reinicia el firmware (EVT INICIO)

USO
---
    python3 planta_simulada.py                      # puertos 7001/7002/7003 (--puerto-esp/--puerto-modbus/--puerto-api)
    python3 planta_simulada.py --escala 30 --ruido 0.3
    python3 planta_simulada.py --test               # autopruebas
"""

import argparse
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from vermicompost_mz import BedParams, MultiZoneBed  # noqa: E402

FIRMWARE_DIR = os.path.join(AQUI, "..", "firmware")
FIRMWARE_FUENTE = os.path.join(FIRMWARE_DIR, "actuador_valvulas", "actuador_valvulas.ino")
FIRMWARE_HOST = os.path.join(FIRMWARE_DIR, "pruebas", "host_main.cpp")
FIRMWARE_BIN = os.environ.get("FIRMWARE_BIN", os.path.join(AQUI, ".build", "firmware_host"))

PIN_A_ZONA = {25: 0, 26: 1, 27: 2}          # mismo cableado que el .ino
DIRECCIONES = [3, 4, 6]                      # sensor Modbus de cada zona


def log(t):
    print(f"{time.strftime('%H:%M:%S')} [planta] {t}", flush=True)


# =============================================================================
# CRC MODBUS
# =============================================================================
def crc16(datos):
    crc = 0xFFFF
    for b in datos:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def con_crc(cuerpo):
    c = crc16(cuerpo)
    return cuerpo + bytes((c & 0xFF, c >> 8))


# =============================================================================
# FIRMWARE REAL COMPILADO PARA EL PC
# =============================================================================
def asegurar_firmware():
    """Compila el firmware si falta o si el .ino es mas nuevo que el binario."""
    fuentes = [FIRMWARE_FUENTE, FIRMWARE_HOST,
               os.path.join(FIRMWARE_DIR, "pruebas", "Arduino.h")]
    if os.path.exists(FIRMWARE_BIN) and all(
            os.path.getmtime(FIRMWARE_BIN) >= os.path.getmtime(f) for f in fuentes if os.path.exists(f)):
        return FIRMWARE_BIN
    compilador = shutil.which("g++")
    if not compilador:
        raise RuntimeError("Falta g++ para compilar el firmware (sudo apt install g++), "
                           "o define FIRMWARE_BIN con un binario ya compilado.")
    os.makedirs(os.path.dirname(FIRMWARE_BIN), exist_ok=True)
    subprocess.run([compilador, "-std=c++17", "-O1", "-I", os.path.join(FIRMWARE_DIR, "pruebas"),
                    "-o", FIRMWARE_BIN, FIRMWARE_HOST], check=True)
    log(f"firmware compilado: {FIRMWARE_BIN}")
    return FIRMWARE_BIN


class ESP32Simulado:
    """El firmware corre como proceso; su 'puerto serie' es stdin/stdout y los
    reles se leen de stderr ('PIN 25=1'). Un cliente TCP a la vez, como el
    USB real (solo el puente lo abre)."""

    def __init__(self, cama, lock_cama):
        self.cama, self.lock_cama = cama, lock_cama
        self.proc = None
        self.cliente = None
        self.lock_cliente = threading.Lock()
        self.reles = {}
        self.arrancar()

    def arrancar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = subprocess.Popen([asegurar_firmware()], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0)
        threading.Thread(target=self._salida, daemon=True).start()
        threading.Thread(target=self._pines, daemon=True).start()

    def _salida(self):
        proc = self.proc
        for linea in iter(proc.stdout.readline, b""):
            with self.lock_cliente:
                if self.cliente:
                    try:
                        self.cliente.sendall(linea)
                    except OSError:
                        self.cliente = None

    def _pines(self):
        proc = self.proc
        for linea in iter(proc.stderr.readline, b""):
            m = re.match(rb"PIN (\d+)=(\d)", linea)
            if m and int(m.group(1)) in PIN_A_ZONA:
                z = PIN_A_ZONA[int(m.group(1))]
                abierto = m.group(2) == b"1"
                self.reles[z] = abierto
                with self.lock_cama:
                    self.cama.set_level(z, 1.0 if abierto else 0.0)

    def atender(self, conn):
        with self.lock_cliente:
            if self.cliente:
                try:
                    self.cliente.close()
                except OSError:
                    pass
            self.cliente = conn
        try:
            while True:
                datos = conn.recv(256)
                if not datos:
                    break
                self.proc.stdin.write(datos)
        except OSError:
            pass
        finally:
            with self.lock_cliente:
                if self.cliente is conn:
                    self.cliente = None


# =============================================================================
# SENSORES SN-3002 (esclavos Modbus RTU)
# =============================================================================
class SensoresModbus:
    def __init__(self, cama, lock_cama, vwc_cc=35.0, ruido=0.2):
        self.cama, self.lock_cama = cama, lock_cama
        self.vwc_cc, self.ruido = vwc_cc, ruido
        self.fallas = set()

    def registros(self, zona):
        with self.lock_cama:
            z = self.cama.zone_state(zona, ligero=True)
        vwc = max(0.0, min(100.0, z["moisture"] * self.vwc_cc + random.gauss(0, self.ruido)))
        temp = z["temperature"] + random.gauss(0, 0.1)
        ec = 0.0 if vwc < 1.0 else 300.0 + 25.0 * vwc + random.gauss(0, 5)
        ph = 7.0 + random.gauss(0, 0.05)
        n, p, k = ec * 0.06, ec * 0.025, ec * 0.08      # el sensor estima NPK desde la EC
        crudos = [round(vwc * 10), round(temp * 10), round(ec), round(ph * 10),
                  round(n), round(p), round(k), 0, 0]
        return [v & 0xFFFF for v in crudos]

    def responder(self, trama):
        """Trama de peticion completa -> bytes de respuesta (o b'' si nadie contesta)."""
        unidad, funcion = trama[0], trama[1]
        if unidad not in DIRECCIONES or unidad in self.fallas:
            return b""
        if funcion != 0x03:
            return con_crc(bytes([unidad, funcion | 0x80, 1]))
        inicio, cantidad = struct.unpack(">HH", trama[2:6])
        regs = self.registros(DIRECCIONES.index(unidad))
        if cantidad < 1 or inicio + cantidad > len(regs):
            return con_crc(bytes([unidad, 0x83, 2]))
        datos = struct.pack(f">{cantidad}H", *regs[inicio:inicio + cantidad])
        return con_crc(bytes([unidad, 0x03, 2 * cantidad]) + datos)

    def atender(self, conn):
        buf = b""
        try:
            while True:
                datos = conn.recv(256)
                if not datos:
                    break
                buf += datos
                while len(buf) >= 8:                  # peticiones 0x03/0x06: 8 bytes
                    trama = buf[:8]
                    if crc16(trama[:6]) != (trama[7] << 8 | trama[6]):
                        buf = buf[1:]                  # resincroniza byte a byte
                        continue
                    buf = buf[8:]
                    time.sleep(0.02)                   # tiempo de respuesta del sensor
                    resp = self.responder(trama)
                    if resp:
                        conn.sendall(resp)
        except OSError:
            pass


# =============================================================================
# FISICA EN TIEMPO REAL (ACELERADA)
# =============================================================================
class Reloj(threading.Thread):
    def __init__(self, cama, lock_cama, escala):
        super().__init__(daemon=True, name="fisica")
        self.cama, self.lock_cama, self.escala = cama, lock_cama, escala

    def run(self):
        t_real0 = time.monotonic()
        while True:
            time.sleep(0.2)
            objetivo = (time.monotonic() - t_real0) * self.escala
            with self.lock_cama:
                while self.cama.t + self.cama.params.dt <= objetivo:
                    self.cama.step()


def servir_tcp(puerto, atender, nombre):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", puerto))
    srv.listen(4)
    log(f"{nombre} escuchando en :{puerto}")

    def bucle():
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log(f"{nombre}: conectado {addr[0]}")
            threading.Thread(target=atender, args=(conn,), daemon=True).start()
    threading.Thread(target=bucle, daemon=True, name=nombre).start()
    return srv


def api_pruebas(puerto, cama, lock_cama, esp, sensores):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, d, c=200):
            b = json.dumps(d).encode()
            self.send_response(c)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.rstrip("/") != "/estado":
                return self._json({"error": "ruta desconocida"}, 404)
            with lock_cama:
                st = cama.get_state(ligero=True)
            self._json({"t_sim_h": round(st["time_hours"], 3), "escala": REL.escala,
                        "zonas": [{"zona": i + 1, "sensor": DIRECCIONES[i],
                                   "humedad_rel": round(z["moisture"], 4),
                                   "vwc_pct": round(z["moisture"] * sensores.vwc_cc, 2),
                                   "temperatura_C": round(z["temperature"], 2),
                                   "valvula_abierta": z["valve_level"] > 0.5}
                                  for i, z in enumerate(st["zones"])],
                        "sensores_en_falla": sorted(sensores.fallas)})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                d = {}
            if self.path == "/falla":
                s = int(d.get("sensor", 0))
                (sensores.fallas.add if d.get("activa", True) else sensores.fallas.discard)(s)
                return self._json({"sensores_en_falla": sorted(sensores.fallas)})
            if self.path == "/reiniciar_esp32":
                esp.arrancar()
                return self._json({"ok": True})
            self._json({"error": "ruta desconocida"}, 404)

    srv = ThreadingHTTPServer(("0.0.0.0", puerto), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"API de pruebas en :{puerto}")


REL = None


def construir_cama(humedad_inicial):
    p = BedParams(init_moisture=humedad_inicial)      # 6 m, 3 zonas de 2 m, 2 % de pendiente
    cama = MultiZoneBed(p)
    if cama.n_zones != 3:
        raise SystemExit(f"la cama simulada debe tener 3 zonas (tiene {cama.n_zones})")
    return cama


def main(argv=None):
    global REL
    ap = argparse.ArgumentParser(description="Planta simulada (ESP32 + sensores Modbus por TCP).")
    ap.add_argument("--escala", type=float, default=float(os.environ.get("ESCALA", "10")))
    ap.add_argument("--vwc-cc", type=float, default=float(os.environ.get("VWC_CC", "35")))
    ap.add_argument("--ruido", type=float, default=float(os.environ.get("RUIDO_VWC", "0.2")))
    ap.add_argument("--humedad-inicial", type=float,
                    default=float(os.environ.get("HUMEDAD_INICIAL", "0.62")),
                    help="W/W_fc al arrancar (0.62 * 35 = 21.7 %% VWC)")
    ap.add_argument("--puerto-esp", type=int, default=int(os.environ.get("PLANTA_PUERTO_ESP", "7001")))
    ap.add_argument("--puerto-modbus", type=int, default=int(os.environ.get("PLANTA_PUERTO_MODBUS", "7002")))
    ap.add_argument("--puerto-api", type=int, default=int(os.environ.get("PLANTA_PUERTO_API", "7003")))
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args(argv)
    if a.test:
        return selftest()

    cama = construir_cama(a.humedad_inicial)
    lock = threading.Lock()
    esp = ESP32Simulado(cama, lock)
    sensores = SensoresModbus(cama, lock, a.vwc_cc, a.ruido)
    REL = Reloj(cama, lock, a.escala)
    REL.start()
    servir_tcp(a.puerto_esp, esp.atender, "ESP32 (firmware real)")
    servir_tcp(a.puerto_modbus, sensores.atender, "sensores Modbus 3/4/6")
    api_pruebas(a.puerto_api, cama, lock, esp, sensores)
    log(f"fisica x{a.escala:g}, VWC = W/W_fc * {a.vwc_cc:g}")
    try:
        while True:
            time.sleep(300)
            with lock:
                st = cama.get_state(ligero=True)
            log("t_sim=%.1f h | " % st["time_hours"] + " | ".join(
                f"Z{i + 1} {z['moisture'] * a.vwc_cc:5.1f}%{' RIEGO' if z['valve_level'] > 0.5 else ''}"
                for i, z in enumerate(st["zones"])))
    except KeyboardInterrupt:
        esp.proc.kill()
    return 0


# =============================================================================
# AUTOPRUEBAS
# =============================================================================
def selftest():
    import serial
    fallos = []

    def check(c, m):
        if not c:
            fallos.append(m)

    global REL
    cama = construir_cama(0.62)
    lock = threading.Lock()
    esp = ESP32Simulado(cama, lock)
    sens = SensoresModbus(cama, lock, 35.0, 0.0)
    REL = Reloj(cama, lock, 60.0)
    REL.start()
    p1, p2 = 17901, 17902
    servir_tcp(p1, esp.atender, "esp")
    servir_tcp(p2, sens.atender, "modbus")
    time.sleep(0.5)

    # 1) Firmware real por socket://, con la misma API que usa el puente.
    s = serial.serial_for_url(f"socket://127.0.0.1:{p1}", timeout=1)
    s.write(b"#1 CONFIG\n#2 ABRIR 2 3\n")
    vistas = s.read(400).decode()
    check("#1 OK CONFIG N=3" in vistas, f"CONFIG no respondio: {vistas!r}")
    check("#2 OK ABRIR 2 3" in vistas, f"ABRIR no respondio: {vistas!r}")
    time.sleep(0.4)
    check(esp.reles.get(1) is True, "el rele de la zona 2 debe estar activo")
    with lock:
        check(cama.levels[1] == 1.0, "el aspersor 2 del modelo debe estar abierto")
    time.sleep(3.0)
    resto = s.read(200).decode()
    check("EVT CERRADA 2 TIEMPO 3" in resto, f"el pulso debe cerrarse solo: {resto!r}")
    with lock:
        check(cama.levels[1] == 0.0, "al cerrar el firmware, el modelo debe cerrar el aspersor")
    s.close()

    # 2) Sensores: trama del datasheet adaptada a la direccion 3.
    m = serial.serial_for_url(f"socket://127.0.0.1:{p2}", timeout=1)
    pet = con_crc(bytes([3, 3, 0, 0, 0, 7]))
    m.write(pet)
    r = m.read(3 + 14 + 2)
    check(len(r) == 19 and crc16(r[:-2]) == (r[-1] << 8 | r[-2]), f"respuesta Modbus invalida: {r.hex()}")
    if len(r) == 19:
        vwc = struct.unpack(">H", r[3:5])[0] / 10
        with lock:
            esperado = cama.zone_state(0)["moisture"] * 35
        check(abs(vwc - esperado) < 0.2, f"humedad {vwc} != {esperado:.1f}")
    sens.fallas.add(4)
    m.write(con_crc(bytes([4, 3, 0, 0, 0, 7])))
    check(m.read(19) == b"", "un sensor en falla no debe responder")
    m.write(con_crc(bytes([9, 3, 0, 0, 0, 7])))
    check(m.read(19) == b"", "una direccion inexistente no debe responder")
    m.close()

    # 3) Regar sube la humedad del modelo (fisica acelerada x60).
    with lock:
        h0 = cama.zone_state(2)["moisture"]
    s = serial.serial_for_url(f"socket://127.0.0.1:{p1}", timeout=1)
    s.write(b"#3 ABRIR 3 20\n")
    time.sleep(6)
    with lock:
        h1 = cama.zone_state(2)["moisture"]
    check(h1 > h0 + 0.01, f"regar 6 s (x60) debe subir la humedad: {h0:.3f} -> {h1:.3f}")
    s.close()
    esp.proc.kill()

    if fallos:
        print("PLANTA SIMULADA - FALLOS:")
        for f in fallos:
            print("  -", f)
        return 1
    print("PLANTA SIMULADA - autopruebas correctas (firmware real + Modbus + fisica).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
