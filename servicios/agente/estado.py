"""
Acceso al estado de la cama (equivale a telemetria.py del agente del huerto).

Dos fuentes, cada una para lo suyo:
  - ESTADO ACTUAL: GET /estado del servicio de control. Trae el modo de cada
    zona, la ultima lectura con su edad real y lo que el puente sabe de cada
    valvula (abierta y segundos restantes, preguntado al ESP32 en ese momento).
    No depende del ritmo de guardado de Mongo, asi que no repite el problema
    del huerto ("acabo de abrirla y me dice cerrada").
  - HISTORIAL: MongoDB (lecturas, riegos, decisiones) de las ultimas horas.

Nunca lanza excepcion: si algo no responde, devuelve None y el texto lo dice.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import config

log = logging.getLogger(__name__)
_cliente = None


def _db():
    global _cliente
    if _cliente is None:
        _cliente = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=2000, tz_aware=True)
    return _cliente[config.MONGO_DB]


def _utc(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def edad_texto(segundos) -> str:
    s = max(0, int(segundos))
    if s < 90:
        return f"hace {s} segundos"
    if s < 5400:
        return f"hace {s // 60} minutos"
    return f"hace {s // 3600} horas"


def estado_actual() -> dict | None:
    try:
        r = httpx.get(f"{config.CONTROL_URL}/estado", timeout=10)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Control no disponible: %s", e)
        return None


def _valvulas(estado: dict) -> dict:
    """numero de valvula -> {'abierta', 'segundos_restantes'} segun el ESP32."""
    puente = estado.get("puente") or {}
    if puente.get("error") or not puente.get("conectado"):
        return {}
    return {v["valvula"]: v for v in puente.get("valvulas", [])}


def texto_zona(z: dict, valvulas: dict) -> str:
    partes = [f"Zona {z['zona']}:"]
    lec = z.get("lectura")
    if lec:
        partes.append(f"humedad {lec['vwc_pct']:.1f} % y temperatura {lec['temperatura_C']:.1f} °C "
                      f"({edad_texto(lec['edad_s'])}).")
    else:
        partes.append("sin lecturas del sensor.")
    if z.get("problema_lectura") and lec:
        partes.append(f"Ojo: {z['problema_lectura']}.")
    v = valvulas.get(z["valvula"])
    if v is None:
        partes.append("No sé si la válvula está abierta: el sistema de riego no responde.")
    elif v["abierta"]:
        partes.append(f"La válvula está abierta, se cierra sola en {v['segundos_restantes']} segundos.")
    else:
        partes.append("La válvula está cerrada.")
    modo = {"manual": "manual (solo riega si alguien lo pide)",
            "pi": "automático con control PI", "rl": "automático con aprendizaje por refuerzo (RL)"}
    partes.append(f"Modo {modo.get(z['modo'], z['modo'])}.")
    banda = z.get("banda_vwc")
    if banda and z["modo"] == "pi":
        partes.append(f"Busca mantener la humedad entre {banda[0]:g} y {banda[1]:g} %.")
    return " ".join(partes)


def texto_estado(estado: dict | None, zona: int | None = None) -> str:
    if estado is None:
        return "No pude consultar el sistema de la cama en este momento."
    valvulas = _valvulas(estado)
    zonas = [z for z in estado["zonas"] if zona is None or z["zona"] == zona]
    if not zonas:
        return f"La cama tiene las zonas {', '.join(str(z['zona']) for z in estado['zonas'])}."
    return " ".join(texto_zona(z, valvulas) for z in zonas)


def resumen_historial() -> dict | None:
    desde = datetime.now(timezone.utc) - timedelta(hours=config.HISTORIAL_HORAS)
    try:
        db = _db()
        prom = {d["_id"]: d for d in db.lecturas.aggregate([
            {"$match": {"sitio": config.SITIO, "timestamp": {"$gte": desde}}},
            {"$group": {"_id": "$sensor_id", "hum": {"$avg": "$humedad_pct"},
                        "tmin": {"$min": "$temperatura_C"}, "tmax": {"$max": "$temperatura_C"},
                        "n": {"$sum": 1}}}])}
        riegos = {}
        for r in db.riegos.find({"sitio": config.SITIO, "inicio": {"$gte": desde}}):
            v = riegos.setdefault(r["valvula"], {"veces": 0, "segundos": 0})
            v["veces"] += 1
            dur = r.get("duracion_real_s")
            if dur is None:                      # todavia abierta
                dur = (datetime.now(timezone.utc) - _utc(r["inicio"])).total_seconds()
            v["segundos"] += dur
        return {"promedios": prom, "riegos": riegos}
    except PyMongoError as e:
        log.warning("No se pudo leer el historial: %s", e)
        return None


def texto_historial(resumen: dict | None, estado: dict | None) -> str:
    h = config.HISTORIAL_HORAS
    if resumen is None:
        return f"No tengo el historial de las últimas {h} horas disponible."
    sensor_de = {z["zona"]: z["sensor"] for z in (estado or {}).get("zonas", [])} or {1: 3, 2: 4, 3: 6}
    valv_de = {z["zona"]: z["valvula"] for z in (estado or {}).get("zonas", [])} or {1: 1, 2: 2, 3: 3}
    partes = [f"En las últimas {h} horas:"]
    for zona in sorted(sensor_de):
        p = resumen["promedios"].get(sensor_de[zona])
        r = resumen["riegos"].get(valv_de[zona], {"veces": 0, "segundos": 0})
        hum = f"humedad promedio {p['hum']:.1f} %" if p else "sin lecturas"
        if r["veces"] == 0:
            riego = "no se regó"
        else:
            veces = "1 vez" if r["veces"] == 1 else f"{r['veces']} veces"
            seg = r["segundos"]
            total = f"{seg:.0f} segundos" if seg < 90 else f"{seg / 60:.0f} minutos"
            riego = f"se regó {veces}, {total} en total"
        partes.append(f"zona {zona}: {hum}, {riego}.")
    return " ".join(partes)
