#!/usr/bin/env python3
"""
Emisor de sensores - nodo central RPi5 (contenedor Docker)

Lectura periodica de sensores de suelo SN-3002-TR-*-N01 (Ver 2.0)
(Humedad + Temp + EC + pH + N + P + K) por Modbus RTU sobre un conversor
USB-RS485.

Version migrada desde PruebaPilotoUNOEnCama.py (RPi3):
  - Cada lectura se guarda en MongoDB (reemplaza el CSV diario de la Pi3).
  - Si MongoDB no esta disponible, la lectura se guarda en un CSV de
    respaldo local para no perder datos.
  - Publica cada lectura en tiempo real por MQTT (para pruebas).
  - Opcionalmente publica el lote del dia (consultado desde MongoDB) cada
    12 h, con el mismo formato CSV que espera el receptor del laboratorio.
    Cuando Node-RED asuma la publicacion de lotes, se desactiva con
    LOTE_DESDE_SCRIPT=0.

Toda la configuracion se toma de variables de entorno (docker-compose.yml),
con valores por defecto iguales a los de la Pi3.

Proyecto Minciencias 113982 - UAO
"""

import argparse
import csv
import io
import os
import signal
import struct  # noqa: F401  (se conserva por compatibilidad con la version anterior)
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import serial
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# CONFIGURACION (variables de entorno, con valores por defecto)
# ---------------------------------------------------------------------------


def env_bool(nombre, defecto):
    valor = os.getenv(nombre)
    if valor is None:
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "s", "yes", "y")


ZONA_HORARIA = ZoneInfo(os.getenv("TZ", "America/Bogota"))
SITIO = os.getenv("SITIO", "huertolab")

# Puerto serie: dentro del contenedor siempre se ve como /dev/sensor7in1
# (docker-compose mapea el adaptador FTDI real a esta ruta).
PUERTO_SERIE = os.getenv("PUERTO_SERIE", "/dev/sensor7in1")
BAUDRATE = int(os.getenv("BAUDRATE", "4800"))
TIMEOUT_LECTURA = float(os.getenv("TIMEOUT_LECTURA", "1.0"))

# Direcciones Modbus separadas por coma, p. ej. "1,2,3"
DIRECCIONES_SENSORES = [
    int(d) for d in os.getenv("DIRECCIONES_SENSORES", "3").split(",") if d.strip()
]

INTERVALO_MUESTREO_S = int(os.getenv("INTERVALO_MUESTREO_S", "300"))
MODO_LECTURA = os.getenv("MODO_LECTURA", "bloque").strip().lower()   # bloque | individual

# Mapa de registros SN-3002-TR-*-N01 Ver 2.0 (funcion 0x03):
#   0x0000 Humedad (/10 %RH) | 0x0001 Temperatura (/10 C, con signo)
#   0x0002 Conductividad (uS/cm) | 0x0003 pH (/10)
#   0x0004 N (mg/kg) | 0x0005 P (mg/kg) | 0x0006 K (mg/kg)
REGISTRO_INICIAL = 0x0000
CANTIDAD_REGISTROS = 7
NOMBRES_VARIABLES = [
    "humedad_pct",
    "temperatura_C",
    "conductividad_uScm",
    "ph",
    "nitrogeno_mgkg",
    "fosforo_mgkg",
    "potasio_mgkg",
]
FACTORES_ESCALA = [10.0, 10.0, 1.0, 10.0, 1.0, 1.0, 1.0]
INDICES_CON_SIGNO = [1]  # temperatura

# MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017")
MONGO_DB = os.getenv("MONGO_DB", "vermicompost")
MONGO_COLECCION = os.getenv("MONGO_COLECCION", "lecturas")

# Respaldo local si MongoDB falla
CARPETA_RESPALDO = os.getenv("CARPETA_RESPALDO", "/datos")

# MQTT
ENVIAR_TIEMPO_REAL = env_bool("ENVIAR_TIEMPO_REAL", True)
LOTE_DESDE_SCRIPT = env_bool("LOTE_DESDE_SCRIPT", True)
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PUERTO = int(os.getenv("MQTT_PUERTO", "1883"))
MQTT_TOPICO_TIEMPO_REAL = os.getenv(
    "MQTT_TOPICO_TIEMPO_REAL", "uao/vermicompost/huertolab/datos"
)
MQTT_TOPICO_LOTE = os.getenv("MQTT_TOPICO_LOTE", "uao/vermicompost/huertolab/lote")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "rpi5-huertolab-sensores")
MQTT_TIMEOUT_PUBLICACION_S = int(os.getenv("MQTT_TIMEOUT_PUBLICACION_S", "10"))
INTERVALO_LOTE_S = int(os.getenv("INTERVALO_LOTE_S", str(12 * 60 * 60)))


def log(mensaje):
    print(mensaje, flush=True)


def ahora_local():
    return datetime.now(ZONA_HORARIA)


def texto_hora(marca):
    """Hora local sin zona, mismo formato que la Pi3 (2026-10-21T16:11:01)."""
    return marca.replace(tzinfo=None).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# MODBUS RTU (sin cambios respecto a la version de la Pi3)
# ---------------------------------------------------------------------------


def calcular_crc16(datos):
    crc = 0xFFFF
    for byte in datos:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def construir_trama_lectura(direccion_esclavo, registro_inicial, cantidad_registros):
    trama = bytes([
        direccion_esclavo,
        0x03,
        (registro_inicial >> 8) & 0xFF,
        registro_inicial & 0xFF,
        (cantidad_registros >> 8) & 0xFF,
        cantidad_registros & 0xFF,
    ])
    return trama + calcular_crc16(trama)


def validar_crc_respuesta(respuesta):
    if len(respuesta) < 3:
        return False
    return respuesta[-2:] == calcular_crc16(respuesta[:-2])


def parsear_respuesta(respuesta, cantidad_registros, registro_inicial=0):
    byte_count = respuesta[2]
    esperado = cantidad_registros * 2
    if byte_count != esperado:
        raise ValueError(
            f"Cantidad de bytes de datos inesperada: {byte_count} (se esperaban {esperado})"
        )
    datos = respuesta[3:3 + byte_count]
    valores = []
    for i in range(cantidad_registros):
        valor = (datos[2 * i] << 8) | datos[2 * i + 1]
        if (registro_inicial + i) in INDICES_CON_SIGNO and valor >= 0x8000:
            valor -= 0x10000
        valores.append(valor)
    return valores


def _leer_registros(puerto, direccion_esclavo, registro_inicial, cantidad):
    """Una transaccion Modbus 0x03. Devuelve los valores crudos o None."""
    trama = construir_trama_lectura(direccion_esclavo, registro_inicial, cantidad)
    puerto.reset_input_buffer()
    puerto.write(trama)

    longitud_esperada = 3 + (cantidad * 2) + 2
    respuesta = puerto.read(longitud_esperada)

    if len(respuesta) != longitud_esperada:
        log(f"  [sensor {direccion_esclavo}] respuesta incompleta "
            f"({len(respuesta)}/{longitud_esperada} bytes) - sin datos")
        return None
    if not validar_crc_respuesta(respuesta):
        log(f"  [sensor {direccion_esclavo}] CRC invalido - descartando lectura")
        return None
    if respuesta[0] != direccion_esclavo:
        log(f"  [sensor {direccion_esclavo}] direccion de respuesta no coincide - descartando")
        return None
    try:
        return parsear_respuesta(respuesta, cantidad, registro_inicial)
    except ValueError as error:
        log(f"  [sensor {direccion_esclavo}] error al interpretar respuesta: {error}")
        return None


def leer_sensor(puerto, direccion_esclavo):
    """MODO_LECTURA=bloque: los 7 registros en una trama (menos trafico).
    MODO_LECTURA=individual: un registro por trama. Hay unidades del SN-3002
    que devuelven 0 en algunos canales al leerlas en bloque y responden bien
    de uno en uno (visto en el montaje del laboratorio con planta.py)."""
    if MODO_LECTURA == "individual":
        crudos = []
        for i in range(CANTIDAD_REGISTROS):
            valor = _leer_registros(puerto, direccion_esclavo, REGISTRO_INICIAL + i, 1)
            if valor is None:
                return None
            crudos.extend(valor)
            time.sleep(0.05)
    else:
        crudos = _leer_registros(puerto, direccion_esclavo, REGISTRO_INICIAL, CANTIDAD_REGISTROS)
        if crudos is None:
            return None

    return {
        nombre: crudo / factor
        for nombre, crudo, factor in zip(NOMBRES_VARIABLES, crudos, FACTORES_ESCALA)
    }


# ---------------------------------------------------------------------------
# MONGODB
# ---------------------------------------------------------------------------


def conectar_mongo():
    """Devuelve la coleccion de lecturas. La conexion real es perezosa:
    si MongoDB aun no esta listo, los inserts fallaran y caeran al respaldo."""
    cliente = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
    coleccion = cliente[MONGO_DB][MONGO_COLECCION]
    try:
        coleccion.create_index([("timestamp", ASCENDING)])
        coleccion.create_index([("fecha", ASCENDING), ("sensor_id", ASCENDING)])
        log(f"MongoDB listo: {MONGO_URI} -> {MONGO_DB}.{MONGO_COLECCION}")
    except PyMongoError as error:
        log(f"[mongo] aun no disponible al iniciar ({error}); se reintentara en cada lectura")
    return coleccion


def construir_documento(sensor_id, lectura, marca, modo, corrida):
    documento = {
        "timestamp": marca.astimezone(timezone.utc),
        "fecha": marca.strftime("%Y-%m-%d"),
        "sitio": SITIO,
        "sensor_id": sensor_id,
        "modo": modo,
        "corrida": corrida,
    }
    documento.update(lectura)
    return documento


def guardar_mongo(coleccion, documento):
    try:
        coleccion.insert_one(documento)
        return True
    except PyMongoError as error:
        log(f"  [mongo] no se pudo guardar la lectura: {error}")
        return False


def guardar_respaldo_csv(sensor_id, lectura, marca):
    """Respaldo local por si MongoDB no responde (no se pierde la lectura)."""
    os.makedirs(CARPETA_RESPALDO, exist_ok=True)
    ruta = os.path.join(CARPETA_RESPALDO, f"respaldo_{marca.strftime('%Y-%m-%d')}.csv")
    nuevo = not os.path.exists(ruta)
    with open(ruta, "a", newline="") as f:
        escritor = csv.writer(f)
        if nuevo:
            escritor.writerow(["timestamp", "sensor_id"] + NOMBRES_VARIABLES)
        escritor.writerow(
            [texto_hora(marca), sensor_id]
            + [lectura[n] for n in NOMBRES_VARIABLES]
        )
    log(f"  [respaldo] lectura guardada en {ruta}")


# ---------------------------------------------------------------------------
# MQTT (conexiones cortas, igual que en la Pi3)
# ---------------------------------------------------------------------------


def publicar_mqtt(topico, mensaje, qos=1, retain=False):
    """Conecta, publica, espera confirmacion y cierra. Se mantiene el modelo
    de conexion corta porque el operador movil corta las sesiones largas."""
    cliente = None
    try:
        cliente = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
        )
        cliente.connect(MQTT_BROKER, MQTT_PUERTO, keepalive=15)
        cliente.loop_start()
        resultado = cliente.publish(topico, mensaje, qos=qos, retain=retain)
        resultado.wait_for_publish(timeout=MQTT_TIMEOUT_PUBLICACION_S)
        return resultado.is_published()
    except Exception as error:
        log(f"  [mqtt] error publicando en {topico}: {error}")
        return False
    finally:
        if cliente is not None:
            cliente.loop_stop()
            cliente.disconnect()


def publicar_lectura_mqtt(sensor_id, lectura, marca):
    """Misma linea CSV que en la Pi3: timestamp,sensor_id,7 variables."""
    valores = ",".join(str(lectura[n]) for n in NOMBRES_VARIABLES)
    mensaje = f"{texto_hora(marca)},{sensor_id},{valores}"
    if not publicar_mqtt(MQTT_TOPICO_TIEMPO_REAL, mensaje, qos=1):
        log(f"  [sensor {sensor_id}] no se pudo confirmar el envio por MQTT")


def construir_lote_csv(coleccion, fecha):
    """Arma el CSV del dia a partir de MongoDB, con el mismo encabezado y
    formato que el archivo diario de la Pi3 (compatible con el receptor)."""
    salida = io.StringIO()
    escritor = csv.writer(salida, lineterminator="\n")
    escritor.writerow(["timestamp", "sensor_id"] + NOMBRES_VARIABLES)
    for doc in coleccion.find({"fecha": fecha, "sitio": SITIO}).sort("timestamp", ASCENDING):
        marca = doc["timestamp"].astimezone(ZONA_HORARIA)
        escritor.writerow(
            [texto_hora(marca), doc["sensor_id"]]
            + [doc.get(n) for n in NOMBRES_VARIABLES]
        )
    return salida.getvalue()


def publicar_lote_mqtt(coleccion):
    fecha = ahora_local().strftime("%Y-%m-%d")
    try:
        contenido = construir_lote_csv(coleccion, fecha)
    except PyMongoError as error:
        log(f"  [lote] no se pudo consultar MongoDB: {error}")
        return
    if publicar_mqtt(MQTT_TOPICO_LOTE, contenido, qos=1, retain=True):
        log(f"  [lote] lote del {fecha} enviado y confirmado por MQTT")
    else:
        log(f"  [lote] no se pudo confirmar el envio del lote del {fecha}")


# ---------------------------------------------------------------------------
# PROGRAMA PRINCIPAL
# ---------------------------------------------------------------------------


def manejar_senal(signum, frame):
    # docker stop envia SIGTERM: se convierte en salida ordenada
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="Emisor de sensores 7-en-1 (RPi5).")
    parser.add_argument("--modo", choices=["prueba", "produccion"],
                        default=os.getenv("MODO", "produccion"))
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, manejar_senal)

    modo = args.modo
    corrida = ahora_local().strftime("%Y-%m-%d_%H%M")
    log(f"Modo {modo.upper()} (corrida {corrida}), sitio '{SITIO}'")

    coleccion = conectar_mongo()

    log(f"Abriendo puerto {PUERTO_SERIE} a {BAUDRATE} baudios...")
    try:
        # serial_for_url: /dev/sensor7in1 en el huerto, socket://... con la
        # planta simulada (docker-compose.sim.yml).
        puerto = serial.serial_for_url(
            PUERTO_SERIE,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=TIMEOUT_LECTURA,
        )
    except serial.SerialException as error:
        log(f"No se pudo abrir el puerto serie: {error}")
        sys.exit(1)  # Docker lo reinicia (restart: unless-stopped)

    log(f"Sensores: {DIRECCIONES_SENSORES} | muestreo cada {INTERVALO_MUESTREO_S} s "
        f"| lectura en {MODO_LECTURA}")
    log(f"MQTT tiempo real: {'si' if ENVIAR_TIEMPO_REAL else 'no'} | "
        f"lote desde script: {'si' if LOTE_DESDE_SCRIPT else 'no'}\n")

    ultimo_envio_lote = time.time()

    try:
        while True:
            log(f"--- Ciclo de lectura: {texto_hora(ahora_local())} ---")

            for direccion in DIRECCIONES_SENSORES:
                lectura = leer_sensor(puerto, direccion)
                if lectura is None:
                    continue

                marca = ahora_local()
                documento = construir_documento(direccion, lectura, marca, modo, corrida)
                if not guardar_mongo(coleccion, documento):
                    guardar_respaldo_csv(direccion, lectura, marca)

                if ENVIAR_TIEMPO_REAL:
                    publicar_lectura_mqtt(direccion, lectura, marca)

                log(f"  [sensor {direccion}] " + ", ".join(
                    f"{n}={lectura[n]:.2f}" for n in NOMBRES_VARIABLES))
                time.sleep(0.3)  # pausa entre sensores en el bus RS485

            if LOTE_DESDE_SCRIPT and time.time() - ultimo_envio_lote >= INTERVALO_LOTE_S:
                publicar_lote_mqtt(coleccion)
                ultimo_envio_lote = time.time()

            time.sleep(INTERVALO_MUESTREO_S)

    except KeyboardInterrupt:
        log("\nLectura detenida.")
    finally:
        puerto.close()
        log("Puerto serie cerrado.")


if __name__ == "__main__":
    main()
