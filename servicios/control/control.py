#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control.py - CONTROL DE HUMEDAD POR ZONA (RL / PI / MANUAL) Y ARBITRO DEL MODO
=============================================================================

Es el UNICO dueño del modo de cada zona y el UNICO cliente del puente de
actuadores. El agente conversacional y Node-RED no abren valvulas por su
cuenta: se lo piden a este servicio, que decide si la orden procede segun el
modo de la zona. Asi nunca hay dos cerebros peleando por la misma valvula.

    sensores --lecturas--> MongoDB <--lee-- control --HTTP--> actuadores --USB--> ESP32
                                             ^  API :8100
                                    agente / Node-RED

MODOS (por zona, se cambian en caliente y sobreviven a un reinicio)
-----------------------------------------------------------------
  manual : el control no toca la valvula. Solo se abre por orden explicita
           (POST /manual/abrir) y siempre con duracion: el firmware la cierra
           sola. Pasar a manual CIERRA la valvula.
  pi     : PI discreto sobre la humedad en % VWC (setpoint = centro de la
           banda). La salida u en [0,1] se aplica como pulso de u*Ts segundos.
           Anti-windup por integracion condicional y zona muerta con
           decaimiento de la integral (mismo esquema que el .ino del PI).
  rl     : politica tabular entrenada en el simulador v3 (tablas_rl/), en modo
           "controlar" (sin explorar ni aprender). Trabaja en W/W_fc:
           humedad_rel = VWC / vwc_cc.

SEGURIDADES (independientes del algoritmo)
------------------------------------------
  - Sin lectura fiable no se riega: sin dato, dato mas viejo que
    LECTURA_MAX_EDAD_S, o sonda al aire (humedad y EC en 0) -> valvula cerrada.
  - Por encima de banda_alta + MARGEN_ALTO_VWC nunca se abre (RL incluido).
  - El firmware del ESP32 recorta cada pulso, corta tras un tiempo continuo
    maximo y cierra todo si deja de oir al puente.

API HTTP (:8100)
----------------
  GET  /estado                          modo, ultima lectura y decision por zona
  POST /modo           {"zona": 1, "modo": "manual|pi|rl", "origen": "agente"}
  POST /manual/abrir   {"zona": 1, "segundos": 120, "origen": "agente"}
  POST /manual/cerrar  {"zona": 1, "origen": "agente"}     (siempre permitido)
  POST /cerrar_todo    {"origen": "..."}                   pasa todo a manual y cierra

Cada decision se registra en MongoDB (coleccion "decisiones"): es el dato que
permite comparar RL contra PI en la tesis y explicar "por que rego".
"""

import json
import math
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

from pymongo import DESCENDING, MongoClient
from pymongo.errors import PyMongoError

# El modelo y los agentes RL viven en simulador/ (una sola copia en el repo).
SIMULADOR_DIR = os.getenv("SIMULADOR_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                        "..", "..", "simulador"))
sys.path.insert(0, SIMULADOR_DIR)

CONFIG_PATH = os.getenv("CONTROL_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "control.json"))
PUENTE_URL = os.getenv("PUENTE_URL", "http://actuadores:8000").rstrip("/")
PUERTO_HTTP = int(os.getenv("PUERTO_HTTP", "8100"))
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017")
MONGO_DB = os.getenv("MONGO_DB", "vermicompost")
SITIO = os.getenv("SITIO", "huertolab")
TS_S = float(os.getenv("TS_S", "300"))                 # periodo de decision
LECTURA_MAX_EDAD_S = float(os.getenv("LECTURA_MAX_EDAD_S", str(2.5 * TS_S)))
MARGEN_ALTO_VWC = float(os.getenv("MARGEN_ALTO_VWC", "2.0"))
SOLAPE_S = float(os.getenv("SOLAPE_S", "10"))          # ver _segundos_pulso
MANUAL_MAX_S = int(os.getenv("MANUAL_MAX_S", "900"))   # = MAX_PULSO_S del firmware
MODOS = ("manual", "pi", "rl")


def log(t):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {t}", flush=True)


def ahora():
    return datetime.now(timezone.utc)


def _utc(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# =============================================================================
# PUENTE DE ACTUADORES (unico camino hacia el ESP32)
# =============================================================================
class Puente:
    def __init__(self, url=PUENTE_URL, timeout=8.0):
        self.url, self.timeout = url, timeout

    def _pedir(self, metodo, ruta, cuerpo=None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        req = urlreq.Request(self.url + ruta, data=datos, method=metodo,
                             headers={"Content-Type": "application/json"})
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": str(e)}
        except (URLError, OSError, ValueError) as e:
            return 0, {"error": f"puente no disponible: {e}"}

    def estado(self):
        return self._pedir("GET", "/estado")

    def abrir(self, valvula, segundos, origen):
        return self._pedir("POST", f"/valvulas/{valvula}/abrir",
                           {"segundos": int(segundos), "origen": origen})

    def cerrar(self, valvula, origen):
        return self._pedir("POST", f"/valvulas/{valvula}/cerrar", {"origen": origen})


# =============================================================================
# CONTROLADORES
# =============================================================================
class ControladorPI:
    """PI discreto en forma ISA: u = Kc*e + (Kc/Ti) * sum(e*Ts), e en % VWC."""

    def __init__(self, kc=0.08, ti_s=1800.0, zona_muerta=1.0, decaimiento=0.99):
        self.kc, self.ti_s = float(kc), float(ti_s)
        self.zona_muerta, self.decaimiento = float(zona_muerta), float(decaimiento)
        self.integral = 0.0

    def reiniciar(self):
        self.integral = 0.0

    def decidir(self, vwc, banda, ts):
        setpoint = 0.5 * (banda[0] + banda[1])
        e = setpoint - vwc
        if abs(e) < self.zona_muerta:
            self.integral *= self.decaimiento
            return 0.0, f"PI: en zona muerta (error {e:+.1f} %)"
        propuesta = self.integral + e * ts
        u = self.kc * e + (self.kc / self.ti_s) * propuesta
        saturado = (u > 1.0 and e > 0) or (u < 0.0 and e < 0)
        if not saturado:                       # integracion condicional (anti-windup)
            self.integral = propuesta
        u = min(1.0, max(0.0, u))
        return u, f"PI: error {e:+.1f} %, u={u:.2f}"


class ControladorRL:
    """Politica tabular entrenada en el simulador, en modo controlar."""

    def __init__(self, zona_idx, algoritmo="qlearning", tabla=None):
        from rl_agentes import crear_agente
        from rl_common_mz import DiscretizadorZona
        ruta = os.path.join(SIMULADOR_DIR, "tablas_rl", tabla or f"z{zona_idx}_q-learning.json")
        with open(ruta, encoding="utf-8") as f:
            guardado = json.load(f)
        bins = guardado.get("bins") or {}
        filas, cols = bins.get("filas") or {}, bins.get("columnas") or {}
        self.banda = tuple((filas.get("banda") or [0.6, 0.7])[:2])
        disc = DiscretizadorZona(self.banda[0], self.banda[1],
                                 moist_bins=int(filas.get("bins", 20)),
                                 up_bins=int(cols.get("bins", 6)))
        if bins.get("firma") and bins["firma"] != disc.firma():
            raise ValueError(f"la tabla {ruta} usa otra discretizacion ({bins['firma']})")
        self.agente = crear_agente(algoritmo, zona_idx, disc)
        self.agente.path = ruta
        if not self.agente.load(ruta):
            raise ValueError(f"no se pudo cargar la politica {ruta}")
        self.agente.epsilon = 0.0
        self.ruta = ruta
        self.nivel_previo = 0.0

    def reiniciar(self):
        self.nivel_previo = 0.0

    def decidir(self, obs):
        o = dict(obs, band_low=self.banda[0], band_high=self.banda[1],
                 nivel_previo=self.nivel_previo)
        nivel = float(self.agente.acciones[self.agente.act(o, explore=False)])
        m = o["moisture"]
        motivo = f"RL: humedad_rel {m:.3f}, celda {self.agente.disc.key(o)}"
        # Interlock de dos lados, igual que RunnerZona._nivel_seguro del v3.
        if m >= self.banda[1] + 0.05 and nivel > 0:
            nivel, motivo = 0.0, motivo + " -> cierre de seguridad (muy humeda)"
        elif m <= self.banda[0] - 0.15 and nivel <= 0:
            nivel, motivo = 1.0, motivo + " -> apertura de seguridad (muy seca)"
        self.nivel_previo = nivel
        return nivel, motivo


# =============================================================================
# SERVICIO
# =============================================================================
class Control:
    def __init__(self, config, db, puente):
        self.cfg = config
        self.db = db
        self.puente = puente
        self.lock = threading.RLock()
        self.zonas = {int(z["zona"]): dict(z) for z in config["zonas"]}
        self.ctrl = {}
        self.ultimo = {z: {} for z in self.zonas}       # ultima decision por zona
        # Desconocido al arrancar: se asume abierta para que la primera
        # decision de "no regar" envie un cierre (cerrar es idempotente).
        self.valvula_abierta = {z: True for z in self.zonas}
        self._cargar_modos()

    # ---- persistencia de modos ----------------------------------------------
    def _cargar_modos(self):
        guardados = {}
        try:
            for d in self.db.control_modos.find({"sitio": SITIO}):
                guardados[int(d["zona"])] = d.get("modo")
        except PyMongoError as e:
            log(f"[mongo] no se pudieron leer los modos guardados ({e}); se usan los de control.json")
        for z, info in self.zonas.items():
            modo = guardados.get(z) or info.get("modo", "manual")
            if modo not in MODOS:
                modo = "manual"
            if not self._preparar(z, modo):
                modo = "manual"
            info["modo"] = modo
        log("modos: " + ", ".join(f"zona {z}={i['modo']}" for z, i in self.zonas.items()))

    def _preparar(self, zona, modo):
        """Crea el controlador de la zona. Devuelve False si no se puede (p. ej.
        falta la tabla RL): la zona queda en manual en vez de regar a ciegas."""
        info = self.zonas[zona]
        try:
            if modo == "pi":
                self.ctrl[zona] = ControladorPI(**info.get("pi", {}))
            elif modo == "rl":
                rl = info.get("rl", {})
                self.ctrl[zona] = ControladorRL(zona - 1, rl.get("algoritmo", "qlearning"), rl.get("tabla"))
                banda_vwc = [b * info.get("vwc_cc", 35.0) for b in self.ctrl[zona].banda]
                log(f"zona {zona}: politica RL {self.ctrl[zona].ruta} (banda entrenada "
                    f"{banda_vwc[0]:.1f}-{banda_vwc[1]:.1f} % VWC con vwc_cc={info.get('vwc_cc', 35.0)})")
            else:
                self.ctrl.pop(zona, None)
            return True
        except Exception as e:                           # noqa: BLE001
            log(f"zona {zona}: no se pudo activar el modo {modo}: {e}")
            return False

    def cambiar_modo(self, zona, modo, origen):
        if zona not in self.zonas:
            return 400, {"error": f"zona fuera de {sorted(self.zonas)}"}
        if modo not in MODOS:
            return 400, {"error": f"modo debe ser uno de {MODOS}"}
        with self.lock:
            anterior = self.zonas[zona]["modo"]
            if not self._preparar(zona, modo):
                return 409, {"error": f"no se pudo activar {modo} en la zona {zona}; sigue en {anterior}"}
            self.zonas[zona]["modo"] = modo
            try:
                self.db.control_modos.update_one(
                    {"sitio": SITIO, "zona": zona},
                    {"$set": {"modo": modo, "origen": origen, "cambiado": ahora()}}, upsert=True)
            except PyMongoError as e:
                log(f"[mongo] no se guardo el modo: {e}")
            cierre = None
            if modo == "manual" or anterior == "manual":
                # Al cambiar de dueño la valvula arranca cerrada: manual nunca
                # hereda un riego del algoritmo, y el algoritmo decide desde cero.
                cierre = self._cerrar(zona, f"cambio-modo:{origen}")
            log(f"zona {zona}: modo {anterior} -> {modo} (origen {origen})")
            self._registrar(zona, modo, None, 0.0, 0, f"cambio de modo {anterior}->{modo} por {origen}")
            return 200, {"zona": zona, "modo": modo, "anterior": anterior, "cierre": cierre}

    # ---- ordenes manuales ---------------------------------------------------
    def abrir_manual(self, zona, segundos, origen):
        if zona not in self.zonas:
            return 400, {"error": f"zona fuera de {sorted(self.zonas)}"}
        with self.lock:
            modo = self.zonas[zona]["modo"]
            if modo != "manual":
                return 409, {"error": "requiere_manual", "modo": modo,
                             "detalle": f"la zona {zona} la controla {modo.upper()}; pase primero a manual"}
            if not isinstance(segundos, (int, float)) or not 1 <= segundos <= MANUAL_MAX_S:
                return 400, {"error": f"'segundos' debe estar entre 1 y {MANUAL_MAX_S}"}
            codigo, resp = self.puente.abrir(self.zonas[zona]["valvula"], segundos, origen)
            if codigo == 200:
                self.valvula_abierta[zona] = True
                self._registrar(zona, "manual", None, 1.0, int(segundos), f"apertura manual por {origen}")
            return codigo or 503, resp

    def _cerrar(self, zona, origen):
        codigo, resp = self.puente.cerrar(self.zonas[zona]["valvula"], origen)
        if codigo == 200:
            self.valvula_abierta[zona] = False
        return {"codigo": codigo, **resp}

    def cerrar_manual(self, zona, origen):
        """Cerrar siempre esta permitido, en cualquier modo: cortar el agua
        nunca debe esperar. En RL/PI la zona sigue en su modo y el algoritmo
        vuelve a decidir en el siguiente periodo."""
        if zona not in self.zonas:
            return 400, {"error": f"zona fuera de {sorted(self.zonas)}"}
        with self.lock:
            r = self._cerrar(zona, origen)
            self._registrar(zona, self.zonas[zona]["modo"], None, 0.0, 0, f"cierre por {origen}")
            return (200 if r["codigo"] == 200 else (r["codigo"] or 503)), r

    def cerrar_todo(self, origen):
        with self.lock:
            salida = {}
            for z in self.zonas:
                salida[z] = self.cambiar_modo(z, "manual", origen)[1]
            return 200, {"zonas": salida}

    # ---- lecturas -------------------------------------------------------------
    def _lecturas(self, sensor, n=2):
        try:
            return list(self.db.lecturas.find({"sitio": SITIO, "sensor_id": sensor})
                        .sort("timestamp", DESCENDING).limit(n))
        except PyMongoError as e:
            log(f"[mongo] no se pudieron leer las lecturas del sensor {sensor}: {e}")
            return []

    def _lectura_fiable(self, zona):
        """(doc, problema). problema=None si la lectura sirve para decidir."""
        docs = self._lecturas(self.zonas[zona]["sensor"])
        if not docs:
            return None, "sin lecturas del sensor"
        d = docs[0]
        edad = (ahora() - _utc(d["timestamp"])).total_seconds()
        if edad > LECTURA_MAX_EDAD_S:
            def txt(s):
                return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min"
            return d, f"lectura de hace {txt(edad)} (max {txt(LECTURA_MAX_EDAD_S)})"
        if d.get("humedad_pct") == 0 and d.get("conductividad_uScm") == 0:
            return d, "sonda sin contacto (humedad y EC en 0)"
        return d, None

    def _obs_rl(self, zona, doc):
        info = self.zonas[zona]
        cc = float(info.get("vwc_cc", 35.0))

        def rel(z):
            if z not in self.zonas:
                return 0.0                           # sin vecina: como en el entrenamiento
            docs = self._lecturas(self.zonas[z]["sensor"], 1)
            return (docs[0]["humedad_pct"] / float(self.zonas[z].get("vwc_cc", 35.0))) if docs else 0.0

        docs = self._lecturas(info["sensor"], 2)
        m = doc["humedad_pct"] / cc
        trend = 0.0
        if len(docs) == 2:
            dt = (_utc(docs[0]["timestamp"]) - _utc(docs[1]["timestamp"])).total_seconds()
            if dt > 0:
                trend = (docs[0]["humedad_pct"] - docs[1]["humedad_pct"]) / cc / dt
        return {"moisture": m, "moisture_up": rel(zona - 1), "moisture_down": rel(zona + 1),
                "trend": trend, "carga_arriba": 0.0, "carga_abajo": 0.0,
                "pos_norm": (zona - 1) / max(1, len(self.zonas) - 1), "moisture_grad": 0.0}

    # ---- lazo -----------------------------------------------------------------
    @staticmethod
    def _segundos_pulso(nivel, ts):
        """Pulso de nivel*Ts. A nivel 1 se solapa SOLAPE_S con el siguiente
        periodo: la valvula queda abierta sin cortes y el tope de riego
        continuo del firmware (MAX_CONTINUO_S) puede actuar. Sin solape, cada
        pulso cerraria un instante antes del siguiente y ese tope nunca
        llegaria a contar."""
        if nivel <= 0:
            return 0
        s = nivel * ts + (SOLAPE_S if nivel >= 1.0 else 0.0)
        return int(math.ceil(s))

    def paso(self):
        with self.lock:
            for zona, info in self.zonas.items():
                modo = info["modo"]
                if modo == "manual":
                    continue
                doc, problema = self._lectura_fiable(zona)
                vwc = doc.get("humedad_pct") if doc else None
                banda = info.get("banda", [25.0, 32.0])
                if problema:
                    nivel, motivo = 0.0, f"no se riega: {problema}"
                elif vwc >= banda[1] + MARGEN_ALTO_VWC:
                    nivel, motivo = 0.0, f"no se riega: {vwc:.1f} % supera banda alta + margen"
                    if modo == "pi":
                        self.ctrl[zona].decidir(vwc, banda, TS_S)   # mantiene la integral al dia
                elif modo == "pi":
                    nivel, motivo = self.ctrl[zona].decidir(vwc, banda, TS_S)
                else:
                    nivel, motivo = self.ctrl[zona].decidir(self._obs_rl(zona, doc))
                self._aplicar(zona, modo, vwc, nivel, motivo)

    def _aplicar(self, zona, modo, vwc, nivel, motivo):
        segundos = self._segundos_pulso(nivel, TS_S)
        origen = f"{modo}-z{zona}"
        if segundos >= 1:
            codigo, resp = self.puente.abrir(self.zonas[zona]["valvula"], segundos, origen)
            if codigo == 200:
                self.valvula_abierta[zona] = True
            else:
                motivo += f" | puente {codigo}: {resp.get('error')}"
                segundos = 0
        elif self.valvula_abierta.get(zona, True):
            codigo, resp = self.puente.cerrar(self.zonas[zona]["valvula"], origen)
            if codigo == 200:
                self.valvula_abierta[zona] = False
            else:
                motivo += f" | puente {codigo}: {resp.get('error')}"
        self._registrar(zona, modo, vwc, nivel, segundos, motivo)
        log(f"zona {zona} [{modo}] vwc={vwc if vwc is None else round(vwc, 1)} "
            f"nivel={nivel:.2f} pulso={segundos}s | {motivo}")

    def _registrar(self, zona, modo, vwc, nivel, segundos, motivo):
        doc = {"timestamp": ahora(), "sitio": SITIO, "zona": zona, "modo": modo,
               "vwc_pct": vwc, "nivel": round(float(nivel), 3), "segundos": int(segundos),
               "motivo": motivo}
        self.ultimo[zona] = dict(doc)
        try:
            self.db.decisiones.insert_one(doc)
        except PyMongoError as e:
            log(f"[mongo] no se registro la decision: {e}")

    def estado(self):
        with self.lock:
            zonas = []
            for z, info in self.zonas.items():
                doc, problema = self._lectura_fiable(z)
                ult = dict(self.ultimo.get(z) or {})
                if "timestamp" in ult:
                    ult["timestamp"] = ult["timestamp"].isoformat()
                zonas.append({
                    "zona": z, "nombre": info.get("nombre", f"Zona {z}"), "modo": info["modo"],
                    "sensor": info["sensor"], "valvula": info["valvula"], "banda_vwc": info.get("banda"),
                    "lectura": None if not doc else {
                        "vwc_pct": doc.get("humedad_pct"), "temperatura_C": doc.get("temperatura_C"),
                        "edad_s": round((ahora() - _utc(doc["timestamp"])).total_seconds())},
                    "problema_lectura": problema, "ultima_decision": ult})
        codigo, puente = self.puente.estado()
        return {"ts_s": TS_S, "zonas": zonas,
                "puente": puente if codigo == 200 else {"error": puente.get("error"), "codigo": codigo}}


# =============================================================================
# API HTTP
# =============================================================================
CONTROL = None


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False, default=str).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        if self.path.rstrip("/") == "/estado":
            return self._responder(200, CONTROL.estado())
        self._responder(404, {"error": "ruta desconocida"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError
        except ValueError:
            return self._responder(400, {"error": "el cuerpo no es JSON valido"})
        origen = re.sub(r"[^\w.:-]", "", str(d.get("origen", "api")))[:40] or "api"
        try:
            zona = int(d["zona"]) if "zona" in d else None
        except (TypeError, ValueError):
            return self._responder(400, {"error": "zona invalida"})
        ruta = self.path.rstrip("/")
        if ruta == "/modo" and zona is not None:
            return self._responder(*CONTROL.cambiar_modo(zona, str(d.get("modo", "")).lower(), origen))
        if ruta == "/manual/abrir" and zona is not None:
            return self._responder(*CONTROL.abrir_manual(zona, d.get("segundos"), origen))
        if ruta == "/manual/cerrar" and zona is not None:
            return self._responder(*CONTROL.cerrar_manual(zona, origen))
        if ruta == "/cerrar_todo":
            return self._responder(*CONTROL.cerrar_todo(origen))
        self._responder(404, {"error": "ruta desconocida o falta 'zona'"})


def cargar_config(ruta=CONFIG_PATH):
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)
    vistos = set()
    for z in cfg["zonas"]:
        for k in ("zona", "sensor", "valvula"):
            if (k, z[k]) in vistos:
                raise SystemExit(f"control.json: {k} {z[k]} repetido")
            vistos.add((k, z[k]))
        b = z.get("banda", [25, 32])
        if not b[1] > b[0]:
            raise SystemExit(f"control.json: banda invalida en la zona {z['zona']}")
    return cfg


def bucle(control, parar):
    # La primera decision espera un periodo: da tiempo a que lleguen lecturas
    # frescas tras un reinicio en vez de decidir con un dato viejo.
    while not parar.wait(TS_S):
        try:
            control.paso()
        except Exception as e:                                   # noqa: BLE001
            log(f"error en el lazo de control (sigue): {e}")


def main():
    global CONTROL
    cfg = cargar_config()
    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)[MONGO_DB]
    CONTROL = Control(cfg, db, Puente())
    log(f"control: {len(CONTROL.zonas)} zonas, Ts={TS_S:g} s, puente {PUENTE_URL}, API :{PUERTO_HTTP}")
    parar = threading.Event()
    threading.Thread(target=bucle, args=(CONTROL, parar), daemon=True, name="lazo").start()
    ThreadingHTTPServer(("0.0.0.0", PUERTO_HTTP), Manejador).serve_forever()


if __name__ == "__main__":
    main()
