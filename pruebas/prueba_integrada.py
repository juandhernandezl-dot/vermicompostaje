#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prueba_integrada.py - EL STACK COMPLETO CONTRA LA PLANTA SIMULADA, SIN DOCKER
============================================================================

Corre el codigo REAL de los servicios (sensores, actuadores, control) en un
solo proceso, contra la planta simulada (firmware real del ESP32 + sensores
Modbus + fisica del v3), con un MongoDB en memoria (mongomock). Sirve para
verificar el contrato entre piezas en cualquier PC, antes de tocar la Pi:

    pip install pymongo mongomock pyserial httpx
    python3 pruebas/prueba_integrada.py

En la Pi, la prueba equivalente con contenedores reales es:
    docker compose -f docker-compose.yml -f docker-compose.sim.yml up -d
(ver docs/GUIA_PI5.md).
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Puertos PROPIOS de la prueba, distintos de los del stack de Docker (7001-7003, 8000, 8100,
# 11434). Si la prueba compartiera puertos con un stack encendido, sus servicios no podrian
# abrirlos y las ordenes de la prueba le llegarian al stack real (en el huerto: riegos reales).
P_ESP, P_MODBUS, P_API = 17201, 17202, 17203
P_PUENTE, P_CONTROL, P_LLM = 17280, 17281, 17299
API = f"http://127.0.0.1:{P_API}"


def puertos_libres():
    ocupados = []
    for p in (P_ESP, P_MODBUS, P_API, P_PUENTE, P_CONTROL, P_LLM):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Igual que los servidores reales: un puerto en TIME_WAIT de una corrida anterior
        # no cuenta como ocupado; solo cuenta otro programa ESCUCHANDO en el.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", p))
        except OSError:
            ocupados.append(p)
        finally:
            s.close()
    return ocupados

# ---- configuracion de la prueba (antes de importar los servicios) ----------
os.environ.update({
    "PUERTO_ESP32": f"socket://127.0.0.1:{P_ESP}", "PUERTO_HTTP": str(P_PUENTE), "PERIODO_PING_S": "2",
    "PUERTO_SERIE": f"socket://127.0.0.1:{P_MODBUS}", "DIRECCIONES_SENSORES": "3,4,6",
    "INTERVALO_MUESTREO_S": "2", "ENVIAR_TIEMPO_REAL": "0", "LOTE_DESDE_SCRIPT": "0",
    "CARPETA_RESPALDO": "/tmp/prueba_integrada_respaldo", "TZ": "America/Bogota",
    "PUENTE_URL": f"http://127.0.0.1:{P_PUENTE}", "TS_S": "3", "LECTURA_MAX_EDAD_S": "8",
    "SIMULADOR_DIR": os.path.join(RAIZ, "simulador"),
    "CONTROL_CONFIG": os.path.join(RAIZ, "servicios", "control", "control.json"),
})
import mongomock  # noqa: E402

CLIENTE = mongomock.MongoClient(tz_aware=True)


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MongoClient = lambda *a, **k: CLIENTE         # todos comparten la base en memoria
    return mod


fallos = []


def check(c, m):
    print(("  ok   " if c else "  FALLO ") + m, flush=True)
    if not c:
        fallos.append(m)


def http(metodo, url, cuerpo=None):
    req = urlreq.Request(url, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                         method=metodo, headers={"Content-Type": "application/json"})
    try:
        with urlreq.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def esperar(cond, seg, paso=0.5):
    fin = time.time() + seg
    while time.time() < fin:
        if cond():
            return True
        time.sleep(paso)
    return False


def main():
    ocupados = puertos_libres()
    if ocupados:
        print(f"ABORTADA: puertos ocupados {ocupados}. Otra prueba sigue corriendo o algo los usa.\n"
              "La prueba no arranca a medias para no mandarle ordenes a otro sistema.")
        return 2
    db = CLIENTE["vermicompost"]
    # 1) planta simulada (fisica x60, sin ruido para que la prueba sea estable)
    sys.argv = ["planta_simulada.py"]
    planta = cargar("planta_simulada", os.path.join(RAIZ, "simulador", "planta_simulada.py"))
    threading.Thread(target=planta.main, args=(["--escala", "60", "--ruido", "0", "--puerto-esp", str(P_ESP),
                                                 "--puerto-modbus", str(P_MODBUS), "--puerto-api", str(P_API)],), daemon=True).start()
    time.sleep(1.5)

    # 2) puente de actuadores (codigo real)
    puente = cargar("puente", os.path.join(RAIZ, "servicios", "actuadores", "puente_actuadores.py"))
    threading.Thread(target=puente.main, daemon=True).start()

    # 3) emisor de sensores (codigo real)
    emisor = cargar("emisor", os.path.join(RAIZ, "servicios", "sensores", "emisor_sensores.py"))
    emisor.signal.signal = lambda *a: None             # signal() solo vale en el hilo principal
    sys.argv = ["emisor_sensores.py", "--modo", "prueba"]
    threading.Thread(target=emisor.main, daemon=True).start()

    print("\n[1] sensores -> MongoDB")
    check(esperar(lambda: len({d["sensor_id"] for d in db.lecturas.find()}) == 3, 20),
          "llegan lecturas de los sensores 3, 4 y 6")
    d = db.lecturas.find_one({"sensor_id": 3})
    check(d is not None and 20 < d["humedad_pct"] < 23, f"humedad simulada ~21.7 % VWC ({d and d['humedad_pct']})")

    print("\n[2] puente <-> firmware real")
    check(esperar(lambda: http("GET", f"http://127.0.0.1:{P_PUENTE}/estado")[1].get("conectado"), 15),
          "el puente se sincroniza con el ESP32 (CONFIG + CERRAR_TODO)")
    est = http("GET", f"http://127.0.0.1:{P_PUENTE}/estado")[1]
    check("N=3" in est.get("config_esp32", ""), f"el firmware declara 3 valvulas ({est.get('config_esp32')})")
    check(len(est["valvulas"]) == 3 and not any(v["abierta"] for v in est["valvulas"]),
          "3 valvulas, todas cerradas al arrancar")

    # 4) control (codigo real). Usa la misma variable PUERTO_HTTP que el puente
    # (en Docker son contenedores distintos); aqui se separan a mano.
    os.environ["PUERTO_HTTP"] = str(P_CONTROL)
    control = cargar("control", os.path.join(RAIZ, "servicios", "control", "control.py"))
    threading.Thread(target=control.main, daemon=True).start()
    time.sleep(1.5)
    C = f"http://127.0.0.1:{P_CONTROL}"

    print("\n[3] orden manual a traves del arbitro")
    c, r = http("POST", C + "/manual/abrir", {"zona": 1, "segundos": 40, "origen": "prueba"})
    check(c == 200 and r.get("segundos_aplicados") == 40, f"abrir zona 1 en manual -> {c} {r}")
    check(esperar(lambda: http("GET", API + "/estado")[1]["zonas"][0]["valvula_abierta"], 5),
          "el aspersor 1 del modelo se abre (rele del firmware)")
    rg = db.riegos.find_one({"valvula": 1})
    check(rg is not None and rg["origen"] == "prueba" and rg["estado"] == "abierta",
          "el puente registra el riego en 'riegos'")
    c, r = http("POST", C + "/manual/abrir", {"zona": 1, "segundos": 5000, "origen": "prueba"})
    check(c == 400, "una apertura manual mayor que el tope se rechaza")

    print("\n[4] PI en la zona 2 (21.7 % < banda 25-32 -> debe regar)")
    c, r = http("POST", C + "/modo", {"zona": 2, "modo": "pi", "origen": "prueba"})
    check(c == 200 and r["modo"] == "pi", f"zona 2 -> pi ({c})")
    c, r = http("POST", C + "/manual/abrir", {"zona": 2, "segundos": 30, "origen": "agente"})
    check(c == 409 and r.get("error") == "requiere_manual", "una orden manual en zona PI se rechaza (arbitraje)")
    check(esperar(lambda: db.decisiones.find_one({"zona": 2, "modo": "pi", "segundos": {"$gt": 0}}), 12),
          "el PI decide regar y lo registra en 'decisiones'")
    check(esperar(lambda: http("GET", API + "/estado")[1]["zonas"][1]["valvula_abierta"], 6),
          "la valvula 2 se abre por orden del PI")
    h0 = http("GET", API + "/estado")[1]["zonas"][1]["vwc_pct"]

    print("\n[5] RL en la zona 3")
    c, r = http("POST", C + "/modo", {"zona": 3, "modo": "rl", "origen": "prueba"})
    check(c == 200, f"zona 3 -> rl con la tabla entrenada ({c} {r})")
    check(esperar(lambda: db.decisiones.find_one({"zona": 3, "modo": "rl", "motivo": {"$regex": "^RL"}}), 12),
          "la politica RL toma decisiones")

    print("\n[6] sin lectura fiable no se riega")
    http("POST", API + "/falla", {"sensor": 6, "activa": True})
    check(esperar(lambda: db.decisiones.find_one({"zona": 3, "motivo": {"$regex": "no se riega: lectura"}}), 25),
          "con el sensor 6 caido, la zona 3 no riega")
    http("POST", API + "/falla", {"sensor": 6, "activa": False})

    print("\n[7] la fisica responde al control")
    check(esperar(lambda: http("GET", API + "/estado")[1]["zonas"][1]["vwc_pct"] > h0 + 0.5, 30),
          "la humedad de la zona 2 sube mientras el PI riega")

    print("\n[8] cerrar todo")
    c, r = http("POST", C + "/cerrar_todo", {"origen": "prueba"})
    check(c == 200, "cerrar_todo responde")
    check(esperar(lambda: not any(z["valvula_abierta"] for z in
                                  http("GET", API + "/estado")[1]["zonas"]), 6),
          "todas las valvulas del modelo cerradas")
    est = http("GET", C + "/estado")[1]
    check(all(z["modo"] == "manual" for z in est["zonas"]), "todas las zonas quedan en manual")
    check(esperar(lambda: db.riegos.count_documents({"estado": "abierta"}) == 0, 6),
          "ningun riego queda 'abierto' en la base")
    cerr = db.riegos.find_one({"valvula": 2, "estado": "cerrada"}, sort=[("inicio", -1)])
    check(cerr is not None and cerr.get("motivo_cierre") in ("ORDEN", "TIEMPO", "REEMPLAZADO"),
          f"el riego del PI se cierra con motivo ({cerr and cerr.get('motivo_cierre')})")
    check(db.control_modos.count_documents({"modo": "manual"}) == 3, "los modos quedan guardados en Mongo")

    print("\n[9] reinicio del ESP32")
    http("POST", C + "/modo", {"zona": 1, "modo": "manual", "origen": "prueba"})
    http("POST", C + "/manual/abrir", {"zona": 1, "segundos": 60, "origen": "prueba"})
    time.sleep(1)
    http("POST", API + "/reiniciar_esp32", {})
    check(esperar(lambda: db.riegos.find_one({"valvula": 1, "motivo_cierre": "REINICIO_ESP32"}) is not None, 10),
          "un reinicio del ESP32 cierra en la base el riego que estaba abierto")
    check(not http("GET", API + "/estado")[1]["zonas"][0]["valvula_abierta"],
          "tras el reinicio la valvula queda cerrada (arranque seguro)")

    print("\n[10] agente en terminal (pipeline completo, LLM de prueba)")
    llm_recibido = []

    class LLMFalso(BaseHTTPRequestHandler):                 # imita POST /api/chat de Ollama
        def log_message(self, *a):
            pass

        def do_POST(self):
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            llm_recibido.append(d)
            b = json.dumps({"message": {"content": "Las lombrices comen restos de cocina."},
                            "done_reason": "stop"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = ThreadingHTTPServer(("127.0.0.1", P_LLM), LLMFalso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update({"CONTROL_URL": C, "OLLAMA_BASE_URL": f"http://127.0.0.1:{P_LLM}"})
    sys.path.insert(0, os.path.join(RAIZ, "servicios", "agente"))
    import estado as est_agente                              # noqa: E402
    est_agente.MongoClient = lambda *a, **k: CLIENTE
    import agente                                            # noqa: E402

    def di(t):
        r = agente.procesar("prueba", t)
        print(f"     tú> {t}\n     agente> {r}")
        return r
    http("POST", C + "/cerrar_todo", {"origen": "prueba"})
    r = di("¿cómo está la zona 1?")
    check("Zona 1: humedad" in r and "Modo manual" in r, "consulta de zona resuelta sin LLM")
    r = di("abre la válvula de la zona 2 por 1 minuto")
    check("SÍ o NO" in r and "1 minuto" not in r or "60 segundos" in r, "abrir pide confirmación con la duración")
    r = di("sí")
    check(r.startswith("Listo, abrí la válvula de la zona 2"), "con SÍ, abre y lo confirma el ESP32")
    check(esperar(lambda: http("GET", API + "/estado")[1]["zonas"][1]["valvula_abierta"], 5),
          "la válvula 2 del modelo está abierta")
    r = di("¿la válvula 2 está abierta?")
    check("abierta, se cierra sola" in r, "el estado de la válvula sale del ESP32, no de un dato viejo")
    r = di("pon la zona 3 en PI")
    check("SÍ o NO" in r, "cambiar de modo pide confirmación")
    r = di("sí")
    check("quedó en modo PI" in r, "la zona 3 queda en PI")
    r = di("abre la válvula de la zona 3")
    check("la está controlando PI" in r, "no deja abrir a mano una zona en PI (y no pide un SÍ inútil)")
    r = di("abre la válvula de la zona 1")
    r = di("no")
    check(r == "Listo, no hice nada.", "un NO cancela la orden")
    r = di("cierra la zona 2")
    check(r.startswith("Listo, cerré la válvula de la zona 2"), "cerrar se ejecuta sin confirmación")
    r = di("¿cuánto se ha regado hoy?")
    check("zona 2:" in r and "se regó" in r, "historial de riegos desde Mongo")
    r = di("¿qué comen las lombrices?")
    sistema = llm_recibido[-1]["messages"][0]["content"] if llm_recibido else ""
    check(r == "Las lombrices comen restos de cocina." and "ESTADO ACTUAL: Zona 1" in sistema
          and llm_recibido[-1].get("think") is False,
          "lo que no es orden ni consulta va al LLM con el estado fresco y sin razonamiento")
    r = di("cierra todo")
    check(r.startswith("Listo, cerré todas"), "cerrar todo funciona desde el agente")

    print()
    if fallos:
        print(f"PRUEBA INTEGRADA: {len(fallos)} FALLO(S)")
        return 1
    print("PRUEBA INTEGRADA: todo correcto.")
    return 0


if __name__ == "__main__":
    os._exit(main())
