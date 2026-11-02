"""
Receptor de laboratorio - Proyecto Minciencias 113982 (UAO)
Version con interfaz grafica (mismo diseno del dashboard de la Pi5).

Conserva todo lo del receptor original (receptor_csv_mqtt3.py):
  1. TIEMPO REAL (uao/vermicompost/huertolab/datos): cada lectura completa se
     muestra en pantalla para seguimiento en vivo. No se guarda en disco.
  2. LOTE (uao/vermicompost/huertolab/lote): el CSV completo del dia que publica
     la Pi. Se guarda en la carpeta lotes_respaldo como respaldo verificado.

Y agrega:
  - Ultimas 5 lecturas en tiempo real, con el estado de cada variable.
  - "Solicitar lote": pide a la Pi el lote de una fecha (respuesta por
    uao/vermicompost/huertolab/lote/solicitado, guardada en la misma carpeta).
  - "Graficar lote": grafica cualquier lote guardado dentro de la misma ventana.

Como funciona: el programa abre un servidor solo para este PC (127.0.0.1) y
muestra la interfaz en una ventana de aplicacion de Edge o Chrome. Al cerrar la
ventana, el receptor se detiene.

Instalacion previa (una sola vez):
    pip install paho-mqtt
Uso:
    python receptor_vermicompost.py
"""
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# CONFIGURACION (igual que el receptor original)
# ---------------------------------------------------------------------------
BROKER = os.environ.get("RECEPTOR_BROKER", "broker.hivemq.com")
PUERTO = 1883
# "--sim" (o RECEPTOR_SIM=1) escucha la planta simulada de la Pi, que publica
# con otro prefijo para no mezclarse nunca con los lotes reales.
SIMULACION = "--sim" in sys.argv or os.environ.get("RECEPTOR_SIM") == "1"
PREFIJO = os.environ.get("RECEPTOR_PREFIJO") or (
    "uao/vermicompost/sim" if SIMULACION else "uao/vermicompost/huertolab")
TOPICO_TIEMPO_REAL = f"{PREFIJO}/datos"
TOPICO_LOTE = f"{PREFIJO}/lote"
TOPICO_SOLICITUD = f"{PREFIJO}/solicitud"
TOPICO_LOTE_SOLICITADO = f"{PREFIJO}/lote/solicitado"

# Debe coincidir exactamente con el orden de NOMBRES_VARIABLES del emisor.
ENCABEZADOS = [
    "timestamp", "sensor_id", "humedad_pct", "temperatura_C", "conductividad_uScm",
    "ph", "nitrogeno_mgkg", "fosforo_mgkg", "potasio_mgkg",
]

# Misma carpeta que usaba el receptor al correrlo desde C:\Users\<usuario>.
CARPETA_LOTES = os.path.join(os.path.expanduser("~"),
                             "lotes_respaldo_sim" if SIMULACION else "lotes_respaldo")

# 8766 y no 8765: el 8765 es el servicio del simulador v3 (gui_mz.py). Si los
# dos corrian en el mismo PC, el receptor creia que "ya habia otro receptor"
# y abria una ventana contra el simulador.
PUERTO_INTERFAZ = int(os.environ.get("RECEPTOR_PUERTO", "8766"))
ULTIMAS_LECTURAS = 5
ESPERA_RESPUESTA_S = 60
SIN_VENTANA_S = 180             # si la ventana no da senales en este tiempo, el receptor se cierra

MODO_PRUEBA = os.environ.get("RECEPTOR_PRUEBA") == "1"   # solo para pruebas sin broker


# ---------------------------------------------------------------------------
# ESTADO COMPARTIDO (hilo MQTT <-> servidor de la interfaz)
# ---------------------------------------------------------------------------
class Estado:
    def __init__(self):
        self.lock = threading.Lock()
        self.conectado = False
        self.ultimas = deque(maxlen=ULTIMAS_LECTURAS)
        self.registro = deque(maxlen=400)
        self.seq = 0
        self.avisos = deque(maxlen=20)
        self.aviso_seq = 0
        self.ultimo_lote = None
        self.solicitud = None            # {"id", "fecha", "t"}
        self.auto_graficar = None        # lote solicitado que la interfaz debe graficar
        self.ultimo_pulso = None
        self.cerrar_en = None

    def log(self, texto):
        print(texto)
        with self.lock:
            self.seq += 1
            self.registro.append({"seq": self.seq, "hora": time.strftime("%H:%M:%S"), "texto": texto})

    def aviso(self, texto, tipo="info"):
        with self.lock:
            self.aviso_seq += 1
            self.avisos.append({"seq": self.aviso_seq, "texto": texto, "tipo": tipo})


E = Estado()
cliente = None


# ---------------------------------------------------------------------------
# LOTES
# ---------------------------------------------------------------------------
def fecha_de_lote(contenido):
    for linea in contenido.splitlines()[1:]:
        if linea.strip():
            return linea.split(",")[0][:10]
    return None


def guardar_lote(contenido, solicitado=False):
    os.makedirs(CARPETA_LOTES, exist_ok=True)
    marca = time.strftime("%Y-%m-%d_%H-%M-%S")
    if solicitado:
        nombre = f"lote_solicitado_{fecha_de_lote(contenido) or 'sin-fecha'}_recibido_{marca}.csv"
    else:
        nombre = f"lote_{marca}.csv"
    ruta = os.path.join(CARPETA_LOTES, nombre)
    n = 2
    while os.path.exists(ruta):               # dos lotes en el mismo segundo: no se sobrescriben
        ruta = os.path.join(CARPETA_LOTES, nombre[:-4] + f"_{n}.csv")
        n += 1
    with open(ruta, "w", newline="") as f:
        f.write(contenido)
    return ruta


def ya_guardado(contenido):
    """True si ya existe en la carpeta un lote con exactamente el mismo contenido."""
    if not os.path.isdir(CARPETA_LOTES):
        return False
    datos = contenido.encode()
    for nombre in os.listdir(CARPETA_LOTES):
        ruta = os.path.join(CARPETA_LOTES, nombre)
        try:
            if nombre.lower().endswith(".csv") and os.path.getsize(ruta) == len(datos):
                with open(ruta, "rb") as f:
                    if f.read() == datos:
                        return True
        except OSError:
            continue
    return False


def listar_lotes():
    if not os.path.isdir(CARPETA_LOTES):
        return []
    lotes = []
    for nombre in os.listdir(CARPETA_LOTES):
        if not nombre.lower().endswith(".csv"):
            continue
        ruta = os.path.join(CARPETA_LOTES, nombre)
        try:
            with open(ruta, newline="") as f:
                contenido = f.read()
        except OSError:
            continue
        lineas = [l for l in contenido.splitlines()[1:] if l.strip()]
        lotes.append({
            "nombre": nombre,
            "fecha": fecha_de_lote(contenido),
            "lecturas": len(lineas),
            "solicitado": nombre.startswith("lote_solicitado_"),
            "modificado": os.path.getmtime(ruta),
        })
    lotes.sort(key=lambda l: l["modificado"], reverse=True)
    return lotes


def leer_lote(nombre):
    nombre = os.path.basename(nombre)                     # nunca sale de la carpeta
    ruta = os.path.join(CARPETA_LOTES, nombre)
    filas = []
    with open(ruta, newline="") as f:
        for fila in csv.DictReader(f):
            try:
                filas.append([fila["timestamp"][:19], int(float(fila["sensor_id"]))]
                             + [float(fila[k]) for k in ENCABEZADOS[2:]])
            except (KeyError, ValueError, TypeError):
                continue
    return {"nombre": nombre, "columnas": ENCABEZADOS, "filas": filas}


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def al_conectar(client, userdata, flags, reason_code, properties=None):
    client.subscribe(TOPICO_TIEMPO_REAL)
    client.subscribe(TOPICO_LOTE, qos=1)
    client.subscribe(TOPICO_LOTE_SOLICITADO, qos=1)
    E.conectado = True
    E.log(f"Conectado al broker (codigo: {reason_code})")
    E.log(f"Suscrito a tiempo real: {TOPICO_TIEMPO_REAL}")
    E.log(f"Suscrito a lotes: {TOPICO_LOTE}")
    E.log(f"Suscrito a lotes solicitados: {TOPICO_LOTE_SOLICITADO}")
    E.log(f"Guardando lotes de respaldo en: {CARPETA_LOTES}")
    E.log("Esperando datos...")


def al_desconectar(client, userdata, *args):
    # paho 2.x llama (client, userdata, flags, reason_code, properties);
    # paho 1.x -el python3-paho-mqtt de Ubuntu 22.04- llama (client, userdata, rc).
    E.conectado = False
    codigo = args[1] if len(args) >= 2 else (args[0] if args else "?")
    E.log(f"Conexion con el broker perdida (codigo: {codigo}); reintentando...")


def al_recibir_mensaje(client, userdata, msg):
    try:
        contenido = msg.payload.decode()
    except Exception as e:
        E.log(f"AVISO: mensaje ilegible: {e}")
        return
    manejar_mensaje(msg.topic, contenido, bool(msg.retain))


def manejar_mensaje(topico, contenido, retenido=False):
    try:
        if topico == TOPICO_TIEMPO_REAL:
            partes = contenido.split(",")
            if len(partes) == len(ENCABEZADOS):
                E.log(f"[Tiempo real] {contenido}")
                try:
                    lectura = {"timestamp": partes[0], "sensor_id": int(float(partes[1])),
                               "recibida": time.strftime("%H:%M:%S")}
                    for k, v in zip(ENCABEZADOS[2:], partes[2:]):
                        lectura[k] = float(v)
                    with E.lock:
                        E.ultimas.appendleft(lectura)
                except ValueError:
                    pass
            else:
                E.log(f"[Tiempo real] Formato inesperado ({len(partes)} campos, "
                      f"se esperaban {len(ENCABEZADOS)}): {contenido}")
        elif topico == TOPICO_LOTE:
            if retenido and ya_guardado(contenido):
                # Al conectarse, el broker reenvia el ultimo lote. Si ya esta en la carpeta, no se duplica.
                E.log("[LOTE del broker] Ya estaba guardado en la carpeta; no se guardo de nuevo.")
                return
            ruta = guardar_lote(contenido)
            origen = " (ultimo lote guardado por el broker)" if retenido else ""
            E.log(f"[LOTE recibido{origen}] Guardado como respaldo: {ruta} (~{contenido.count(chr(10))} lineas)")
            E.log("Este archivo es el respaldo completo y verificado del dia.")
            E.ultimo_lote = os.path.basename(ruta)
            if not retenido:
                E.aviso(f"Llegó el lote del {fecha_de_lote(contenido) or '—'}.", "ok")
        elif topico == TOPICO_LOTE_SOLICITADO:
            if contenido.startswith("SIN_DATOS"):
                fecha = contenido.split(",")[1] if "," in contenido else "?"
                E.log(f"[LOTE solicitado] La Pi no tiene lecturas del {fecha}.")
                E.aviso(f"La Pi no tiene lecturas del {fecha}.", "atencion")
            else:
                ruta = guardar_lote(contenido, solicitado=True)
                E.log(f"[LOTE solicitado recibido] Guardado: {ruta} (~{contenido.count(chr(10))} lineas)")
                E.ultimo_lote = os.path.basename(ruta)
                E.auto_graficar = E.ultimo_lote
                E.aviso(f"Llegó el lote solicitado del {fecha_de_lote(contenido)}.", "ok")
            E.solicitud = None
    except PermissionError:
        E.log("AVISO: no se pudo escribir el archivo ahora mismo (probablemente abierto en otro programa "
              "como Excel). Este dato en particular no se guardo, pero el receptor sigue funcionando. "
              f"Mensaje: {contenido[:100]}")
        E.aviso("No se pudo guardar un lote: el archivo está abierto en otro programa.", "critico")
    except Exception as e:
        E.log(f"AVISO: error inesperado guardando el mensaje, el receptor continua: {e}")


def iniciar_mqtt():
    global cliente
    if hasattr(mqtt, "CallbackAPIVersion"):              # paho-mqtt 2.x
        cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    else:                                                # paho-mqtt 1.x (apt de Ubuntu 22.04)
        cliente = mqtt.Client()
    cliente.on_connect = al_conectar
    cliente.on_disconnect = al_desconectar
    cliente.on_message = al_recibir_mensaje
    cliente.reconnect_delay_set(min_delay=2, max_delay=60)
    cliente.connect_async(BROKER, PUERTO, keepalive=60)
    cliente.loop_start()


def solicitar_lote(fecha):
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except (TypeError, ValueError):
        return {"ok": False, "error": "Fecha inválida."}
    if MODO_PRUEBA:
        E.solicitud = {"id": "prueba", "fecha": fecha, "t": time.time()}
        return {"ok": True}
    if not cliente or not cliente.is_connected():
        return {"ok": False, "error": "No hay conexión con el broker. Intenta de nuevo en un momento."}
    ident = uuid.uuid4().hex[:8]
    cliente.publish(TOPICO_SOLICITUD, json.dumps({"fecha": fecha, "id": ident}), qos=1)
    E.solicitud = {"id": ident, "fecha": fecha, "t": time.time()}
    E.log(f"[Solicitud] Pedido el lote del {fecha} a la Pi (id {ident}).")
    return {"ok": True}


def vigilar():
    """Vence solicitudes sin respuesta y cierra el receptor si se cerro la ventana."""
    inicio = time.time()
    while True:
        time.sleep(1)
        s = E.solicitud
        if s and time.time() - s["t"] > ESPERA_RESPUESTA_S:
            E.solicitud = None
            E.log(f"[Solicitud] Sin respuesta de la Pi para el lote del {s['fecha']} en {ESPERA_RESPUESTA_S} s. "
                  "Puede estar sin señal o apagada; intenta de nuevo más tarde.")
            E.aviso(f"La Pi no respondió la solicitud del {s['fecha']}. Puede estar sin señal o apagada.", "critico")
        if MODO_PRUEBA:
            continue
        ahora = time.time()
        if E.cerrar_en and ahora >= E.cerrar_en:
            salir()
        referencia = E.ultimo_pulso or inicio
        if ahora - referencia > SIN_VENTANA_S:
            salir()


def salir():
    # El ejecutable de PyInstaller reenvia Ctrl+C al proceso de Python, que ya lo recibio de la
    # terminal: llegan DOS SIGINT y el segundo interrumpia esta salida con un traceback.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:            # llamada desde un hilo (cierre de la ventana): no hace falta
        pass
    print("\nReceptor detenido por el usuario.")
    try:
        if cliente:
            cliente.loop_stop()
            cliente.disconnect()
    finally:
        os._exit(0)


# ---------------------------------------------------------------------------
# SERVIDOR DE LA INTERFAZ (solo 127.0.0.1)
# ---------------------------------------------------------------------------
class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, datos, codigo=200):
        cuerpo = json.dumps(datos).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            modo = " · <b style='color:#b26a00'>SIMULACIÓN</b>" if SIMULACION else ""
            cuerpo = PAGINA.replace("__CARPETA__", json.dumps(CARPETA_LOTES)).replace("__MODO__", modo).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif url.path == "/api/estado":
            E.ultimo_pulso = time.time()
            E.cerrar_en = None
            desde = int(q.get("desde", ["0"])[0] or 0)
            desde_aviso = int(q.get("aviso", ["0"])[0] or 0)
            with E.lock:
                datos = {
                    "conectado": E.conectado or MODO_PRUEBA,
                    "ultimas": list(E.ultimas),
                    "registro": [r for r in E.registro if r["seq"] > desde],
                    "avisos": [a for a in E.avisos if a["seq"] > desde_aviso],
                    "ultimoLote": E.ultimo_lote,
                    "solicitud": E.solicitud and {"fecha": E.solicitud["fecha"]},
                    "autoGraficar": E.auto_graficar,
                }
                E.auto_graficar = None
            self._json(datos)
        elif url.path == "/api/lotes":
            self._json(listar_lotes())
        elif url.path == "/api/lote":
            try:
                self._json(leer_lote(q.get("nombre", [""])[0]))
            except OSError:
                self._json({"error": "No se encontró el lote."}, 404)
        else:
            self.send_error(404)

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", 0) or 0)
        try:
            datos = json.loads(self.rfile.read(largo) or b"{}")
        except ValueError:
            datos = {}
        if self.path == "/api/solicitar":
            self._json(solicitar_lote(datos.get("fecha")))
        elif self.path == "/api/abrir_carpeta":
            os.makedirs(CARPETA_LOTES, exist_ok=True)
            try:
                if sys.platform.startswith("win"):
                    os.startfile(CARPETA_LOTES)
                else:
                    subprocess.Popen(["xdg-open", CARPETA_LOTES])
            except Exception:
                pass
            self._json({"ok": True})
        elif self.path == "/api/cerrar":
            E.cerrar_en = time.time() + 4          # si es solo una recarga, el siguiente pulso lo cancela
            self._json({"ok": True})
        else:
            self.send_error(404)


def abrir_ventana(url):
    """Abre la interfaz como ventana de aplicacion (sin barra de direcciones)."""
    # Windows (msedge/chrome) y Linux (google-chrome, chromium). Si no hay
    # ninguno -p. ej. Ubuntu solo con Firefox- se abre una pestana normal.
    candidatos = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "google-chrome-stable",
                                             "microsoft-edge", "chromium", "chromium-browser")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidatos += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    perfil = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ReceptorVermicompost", "ventana")
    for exe in candidatos:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,900", f"--user-data-dir={perfil}",
                                  "--no-first-run", "--no-default-browser-check"])
                return
            except OSError:
                continue
    webbrowser.open(url)


def main():
    url = f"http://127.0.0.1:{PUERTO_INTERFAZ}/"
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO_INTERFAZ), Manejador)
    except OSError:
        # El puerto esta ocupado. Si es otro receptor, solo se muestra su ventana
        # (para no duplicar lotes); si es otro programa, se dice en vez de abrir
        # una ventana contra algo que no es el receptor.
        try:
            from urllib.request import urlopen
            with urlopen(url + "api/estado", timeout=3) as r:
                json.loads(r.read())
            abrir_ventana(url)
        except Exception:
            print(f"El puerto {PUERTO_INTERFAZ} lo usa otro programa. Cierra ese programa o abre el "
                  f"receptor con otro puerto: RECEPTOR_PUERTO=8767 python3 receptor_vermicompost.py")
        return
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    if not MODO_PRUEBA:
        iniciar_mqtt()
        abrir_ventana(url)
    threading.Thread(target=vigilar, daemon=True).start()
    print(f"Interfaz del receptor en {url}  |  topicos {PREFIJO}/...  |  lotes en {CARPETA_LOTES}")
    # Ctrl+C va directo a salir(): si llega dos veces (PyInstaller la reenvia), se sale igual.
    signal.signal(signal.SIGINT, lambda *_: salir())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        salir()


# ---------------------------------------------------------------------------
# INTERFAZ (HTML + CSS + JS, mismo estilo del dashboard de la Pi5)
# ---------------------------------------------------------------------------
PAGINA = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receptor de laboratorio · Vermicompostaje UAO</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2024%2024%22%20%3E%20%3Crect%20x%3D%220%22%20y%3D%220%22%20width%3D%2224%22%20height%3D%2224%22%20rx%3D%225%22%20fill%3D%22%232e7d32%22/%3E%20%3Cg%20transform%3D%22translate%283.6%2C3.2%29%20scale%280.7%29%22%3E%3Cpath%20d%3D%22M2%2C22V20C2%2C20%207%2C18%2012%2C18C17%2C18%2022%2C20%2022%2C20V22H2M11.3%2C9.1C10.1%2C5.2%204%2C6.1%204%2C6.1C4%2C6.1%204.2%2C13.9%209.9%2C12.7C9.5%2C9.8%208%2C9%208%2C9C10.8%2C9%2011%2C12.4%2011%2C12.4V17C11.3%2C17%2011.7%2C17%2012%2C17C12.3%2C17%2012.7%2C17%2013%2C17V12.8C13%2C12.8%2013%2C8.9%2016%2C7.9C16%2C7.9%2014%2C10.9%2014%2C12.9C21%2C13.6%2021%2C4%2021%2C4C21%2C4%2012.1%2C3%2011.3%2C9.1Z%22%20fill%3D%22%23ffffff%22/%3E%3C/g%3E%3C/svg%3E">
<style>
:root {
  --verde:#2e7d32; --verde-osc:#1b5e20; --fondo:#eef2ee; --tarjeta:#ffffff; --borde:#dfe6df;
  --texto:#223322; --suave:#6b7a6b; --muy-suave:#9aa89a; --tile:#f8faf8;
  --ok-bg:#f1f8f1; --ok-badge:#c8e6c9; --ok-txt:#1b5e20;
  --at-bg:#fffaf0; --at-badge:#ffe0b2; --at-txt:#a35200;
  --cr-bg:#fff4f4; --cr-badge:#ffcdd2; --cr-txt:#b71c1c;
}
* { box-sizing:border-box; }
html, body { margin:0; background:var(--fondo); color:var(--texto);
  font-family:"Segoe UI", Roboto, system-ui, -apple-system, sans-serif; font-size:14px; }
.pagina { max-width:1480px; margin:0 auto; padding:16px; display:flex; flex-direction:column; gap:16px; }
.tarjeta { background:var(--tarjeta); border:1px solid var(--borde); border-radius:14px; padding:16px 18px; }
.tarjeta > h2 { margin:0 0 12px; font-size:1.05rem; font-weight:600; color:var(--texto); display:flex; align-items:center; gap:10px; }
.tarjeta > h2 .extra { margin-left:auto; font-size:.82rem; font-weight:400; color:var(--suave); }

/* encabezado */
.cab { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:14px; }
.marca { display:flex; align-items:center; gap:12px; }
.marca svg { flex:none; }
.h1 { font-size:1.35rem; font-weight:700; color:var(--verde-osc); line-height:1.2; }
.sub { font-size:.85rem; color:var(--suave); }
.estado { display:inline-flex; align-items:center; gap:7px; font-size:.75rem; font-weight:700;
  letter-spacing:.06em; padding:5px 12px; border-radius:999px; }
.estado .punto { width:8px; height:8px; border-radius:50%; background:currentColor; }
.estado.on { background:#e8f5e9; color:var(--verde); }
.estado.on .punto { animation:latido 1.6s infinite; }
.estado.off { background:#ffebee; color:#c62828; }
@keyframes latido { 0%,100% { opacity:1 } 50% { opacity:.25 } }
.carpeta { display:flex; align-items:center; gap:8px; color:var(--suave); font-size:.82rem; }

/* botones */
.btn { border:0; border-radius:8px; padding:8px 14px; font:inherit; font-weight:600; cursor:pointer;
  display:inline-flex; align-items:center; gap:7px; transition:background .15s, opacity .15s; }
.btn svg { width:17px; height:17px; }
.btn.lleno { background:var(--verde); color:#fff; }
.btn.lleno:hover { background:var(--verde-osc); }
.btn.tono { background:#e8f5e9; color:var(--verde-osc); }
.btn.tono:hover { background:#d7ecd8; }
.btn.link { background:transparent; color:var(--verde); padding:4px 8px; }
.btn:disabled { opacity:.5; cursor:default; }
.campo { border:1px solid #c8d3c8; border-radius:8px; padding:7px 10px; font:inherit; color:var(--verde-osc); background:#fff; }
select.campo { max-width:460px; }

/* lecturas */
.lectura-cab { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-bottom:12px; }
.chip { background:var(--verde-osc); color:#fff; font-weight:600; font-size:.85rem; padding:3px 12px; border-radius:999px; }
.hora { color:var(--suave); font-size:.88rem; display:inline-flex; align-items:center; gap:5px; }
.alerta { display:inline-flex; align-items:center; gap:5px; font-size:.8rem; font-weight:600; padding:3px 10px; border-radius:8px; }
.alerta.ambar { background:#fff8e1; color:#b26a00; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:12px; }
.tile { background:var(--tile); border:1px solid #e3e9e3; border-top:4px solid; border-radius:12px; padding:10px 12px; }
.tile.ok { background:var(--ok-bg); } .tile.atencion { background:var(--at-bg); } .tile.critico { background:var(--cr-bg); }
.tile-cab { display:flex; align-items:center; gap:6px; font-size:.8rem; color:#5b6b5b; font-weight:600; }
.tile-cab i { width:9px; height:9px; border-radius:50%; display:inline-block; }
.valor { font-size:1.6rem; font-weight:700; margin-top:4px; }
.unidad { font-size:.8rem; font-weight:500; color:#7d8b7d; margin-left:4px; }
.rango { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:4px; }
.badge { font-size:.7rem; font-weight:700; padding:1px 8px; border-radius:999px; }
.ok .badge { background:var(--ok-badge); color:var(--ok-txt); }
.atencion .badge { background:var(--at-badge); color:var(--at-txt); }
.critico .badge { background:var(--cr-badge); color:var(--cr-txt); }
.ref { font-size:.72rem; color:#7d8b7d; }
.vacio { display:flex; flex-direction:column; align-items:center; gap:8px; color:#7d8b7d; padding:22px; text-align:center; }
table.hist { width:100%; border-collapse:collapse; margin-top:16px; font-size:.86rem; }
table.hist th { text-align:center; font-weight:600; color:var(--verde-osc); background:#f1f6f1; padding:7px 6px; }
table.hist th:first-child { border-radius:8px 0 0 8px; } table.hist th:last-child { border-radius:0 8px 8px 0; }
table.hist td { text-align:center; padding:7px 6px; border-bottom:1px solid #edf1ed; font-variant-numeric:tabular-nums; }
table.hist tr:first-child td { font-weight:600; }

/* lotes */
.fila-lotes { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.sep { width:1px; align-self:stretch; background:var(--borde); margin:0 6px; }
.lbl { color:var(--suave); font-size:.85rem; }
.info-lote { margin-top:10px; color:var(--suave); font-size:.85rem; }
.giro { width:14px; height:14px; border:2px solid rgba(255,255,255,.5); border-top-color:#fff; border-radius:50%; animation:giro .8s linear infinite; }
@keyframes giro { to { transform:rotate(360deg) } }

/* graficas */
.graficas { display:grid; grid-template-columns:1fr; gap:18px; }
@media (min-width:1000px) { .graficas { grid-template-columns:1fr 1fr; } .graf.ancho { grid-column:1 / -1; } }
.graf { background:#fbfcfb; border:1px solid #e3e9e3; border-radius:12px; padding:12px 14px 8px; position:relative; min-width:0; }
.graf-cab { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; margin-bottom:4px; }
.graf-tit { font-weight:700; color:var(--verde-osc); }
.graf-sub { font-size:.76rem; color:#7d8b7d; }
.leyenda { display:flex; flex-wrap:wrap; gap:12px; font-size:.78rem; color:#5b6b5b; }
.leyenda i { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:5px; vertical-align:-1px; }
.lienzo { position:relative; }
.lienzo svg { display:block; cursor:crosshair; }
.lienzo .grid-l { stroke:#e9eee9; } .lienzo .grid-v { stroke:#e9eee9; stroke-dasharray:3 4; }
.lienzo .eje { fill:#7d8b7d; font-size:12px; }
.lienzo .cursor { stroke:#90a490; stroke-dasharray:4 3; }
.tip { position:absolute; top:6px; transform:translateX(-50%); background:#fff; border:1px solid #dfe6df;
  box-shadow:0 4px 14px rgba(0,0,0,.08); border-radius:8px; padding:6px 10px; font-size:.8rem; pointer-events:none;
  white-space:nowrap; display:none; z-index:2; }
.tip b.t { display:block; color:var(--verde-osc); margin-bottom:2px; }
.tip i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; }
.sin-datos { color:var(--muy-suave); text-align:center; padding:36px 0; }

/* registro */
details.registro summary { cursor:pointer; list-style:none; display:flex; align-items:center; gap:10px; font-size:1.05rem; font-weight:600; }
details.registro summary::-webkit-details-marker { display:none; }
details.registro summary .extra { margin-left:auto; font-size:.82rem; font-weight:400; color:var(--suave); text-align:right; }
details.registro summary .flecha { transition:transform .15s; color:var(--suave); }
details.registro[open] summary .flecha { transform:rotate(90deg); }
#registro { margin-top:12px; background:#fbfcfb; border:1px solid #e3e9e3; border-radius:10px; padding:10px 12px;
  height:210px; overflow:auto; font-family:Consolas, "Cascadia Mono", monospace; font-size:.8rem; color:#334433; }
#registro div { padding:1px 0; white-space:pre-wrap; word-break:break-all; }
#registro .h { color:var(--muy-suave); margin-right:8px; }
#registro .lote { color:var(--verde-osc); font-weight:600; }
#registro .aviso { color:#b26a00; }

/* avisos flotantes */
#avisos { position:fixed; top:16px; right:16px; display:flex; flex-direction:column; gap:8px; z-index:10; }
.toast { min-width:260px; max-width:380px; background:#fff; border:1px solid var(--borde); border-left:5px solid var(--verde);
  border-radius:10px; padding:10px 14px; box-shadow:0 6px 20px rgba(0,0,0,.12); font-size:.88rem; animation:entra .25s ease-out; }
.toast.atencion { border-left-color:#fb8c00; } .toast.critico { border-left-color:#c62828; }
@keyframes entra { from { transform:translateY(-8px); opacity:0 } }
</style>
</head>
<body>
<div id="avisos"></div>
<div class="pagina">

  <div class="tarjeta cab">
    <div class="marca">
      <svg width="38" height="38" viewBox="0 0 24 24" fill="#2e7d32" aria-hidden="true"><path d="M2,22V20C2,20 7,18 12,18C17,18 22,20 22,20V22H2M11.3,9.1C10.1,5.2 4,6.1 4,6.1C4,6.1 4.2,13.9 9.9,12.7C9.5,9.8 8,9 8,9C10.8,9 11,12.4 11,12.4V17C11.3,17 11.7,17 12,17C12.3,17 12.7,17 13,17V12.8C13,12.8 13,8.9 16,7.9C16,7.9 14,10.9 14,12.9C21,13.6 21,4 21,4C21,4 12.1,3 11.3,9.1Z"/></svg>
      <div>
        <div class="h1">Monitoreo de vermicompostaje · Laboratorio</div>
        <div class="sub">HuertoLab UAO · Proyecto Minciencias 113982 · datos recibidos por MQTT__MODO__</div>
      </div>
    </div>
    <div style="display:flex; flex-wrap:wrap; align-items:center; gap:14px">
      <span class="carpeta" id="carpeta"></span>
      <button class="btn link" onclick="post('/api/abrir_carpeta')" title="Abrir la carpeta de lotes">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M10,4H4C2.89,4 2,4.89 2,6V18A2,2 0 0,0 4,20H20A2,2 0 0,0 22,18V8C22,6.89 21.1,6 20,6H12L10,4Z"/></svg>Abrir carpeta
      </button>
      <span class="estado off" id="estado"><span class="punto"></span><span>CONECTANDO…</span></span>
    </div>
  </div>

  <div class="tarjeta">
    <h2>Últimas lecturas en tiempo real <span class="extra" id="recibida"></span></h2>
    <div id="lecturas"></div>
  </div>

  <div class="tarjeta">
    <h2>Lotes</h2>
    <div class="fila-lotes">
      <span class="lbl">Fecha</span>
      <input type="date" class="campo" id="fecha">
      <button class="btn lleno" id="btnSolicitar" onclick="solicitar()">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z"/></svg>Solicitar lote
      </button>
      <span class="sep"></span>
      <select class="campo" id="lista"></select>
      <button class="btn tono" onclick="graficarSeleccion()">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M16,11.78L20.24,4.45L21.97,5.45L16.74,14.5L10.23,10.75L5.46,19H22V21H2V3H4V17.54L9.5,8L16,11.78Z"/></svg>Graficar
      </button>
    </div>
    <div class="info-lote" id="infoLote">Elige una fecha y solicita el lote a la Pi, o grafica uno de los lotes guardados.</div>
  </div>

  <details class="tarjeta registro" id="tarjetaGraf" style="display:none" open>
    <summary><span class="flecha">▶</span>Gráficas del lote <span class="extra" id="infoGraf"></span></summary>
    <div class="graficas" id="graficas" style="margin-top:14px"></div>
  </details>

  <details class="tarjeta registro">
    <summary><span class="flecha">▶</span>Registro <span class="extra">mensajes del receptor</span></summary>
    <div id="registro"></div>
  </details>
</div>

<script>
const CARPETA = __CARPETA__;
const VARS = [
  { k:'humedad_pct',        nombre:'Humedad',        unidad:'%',     color:'#1e88e5' },
  { k:'temperatura_C',      nombre:'Temperatura',    unidad:'°C',    color:'#e53935' },
  { k:'ph',                 nombre:'pH',             unidad:'',      color:'#8e24aa' },
  { k:'conductividad_uScm', nombre:'Conductividad',  unidad:'µS/cm', color:'#fb8c00' },
  { k:'nitrogeno_mgkg',     nombre:'Nitrógeno (N)',  unidad:'mg/kg', color:'#43a047' },
  { k:'fosforo_mgkg',       nombre:'Fósforo (P)',    unidad:'mg/kg', color:'#00897b' },
  { k:'potasio_mgkg',       nombre:'Potasio (K)',    unidad:'mg/kg', color:'#6d4c41' },
];
// Mismos rangos del dashboard de la Pi5
const RANGOS = {
  humedad_pct:   { optimo:[70, 85],   aceptable:[60, 90],  nota:'Óptimo 70–85 %. <60 %: estrés hídrico. >90 %: anaerobiosis y lixiviados.' },
  temperatura_C: { optimo:[15, 25],   aceptable:[10, 35],  nota:'Óptimo 15–25 °C, ideal ≈20 °C. <10 °C: letargo. >35 °C: aumenta la mortalidad.' },
  ph:            { optimo:[6.5, 7.5], aceptable:[6, 8.5],  nota:'Óptimo 6,5–7,5, ideal 7,0. <6: mala digestión. >8,5: inhibe la actividad microbiana.' },
};
const COLORES = ['#1565c0', '#e65100', '#6a1b9a', '#00838f', '#c62828', '#558b2f'];
let seq = 0, avisoSeq = 0, loteActual = null, ultimoLoteVisto = null;

const $ = id => document.getElementById(id);
const num = (x, d = 1) => (typeof x === 'number' && isFinite(x)) ? x.toLocaleString('es-CO', { maximumFractionDigits: d }) : '—';
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
const hoy = () => { const d = new Date(), p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`; };
const fechaLarga = f => { if (!f) return '—'; const [a, m, d] = f.split('-').map(Number);
  return new Date(a, m - 1, d).toLocaleDateString('es-CO', { weekday:'long', day:'numeric', month:'long', year:'numeric' }); };

async function get(url) { const r = await fetch(url, { cache:'no-store' }); return r.json(); }
async function post(url, datos) { const r = await fetch(url, { method:'POST', headers:{ 'Content-Type':'application/json' }, body: JSON.stringify(datos || {}) }); return r.json(); }

function estado(k, v) {
  const r = RANGOS[k]; if (!r || typeof v !== 'number') return 'info';
  if (v >= r.optimo[0] && v <= r.optimo[1]) return 'ok';
  if (v >= r.aceptable[0] && v <= r.aceptable[1]) return 'atencion';
  return 'critico';
}
const etiqueta = e => ({ ok:'Óptimo', atencion:'Atención', critico:'Fuera de rango' }[e] || '');
const txtRango = (k) => { const r = RANGOS[k], u = VARS.find(v => v.k === k).unidad;
  return `óptimo ${num(r.optimo[0])}–${num(r.optimo[1])}${u ? ' ' + u : ''}`; };

function aviso(texto, tipo) {
  const t = document.createElement('div'); t.className = 'toast ' + (tipo || ''); t.textContent = texto;
  $('avisos').appendChild(t); setTimeout(() => t.remove(), 6000);
}

/* ---------------- tiempo real ---------------- */
function pintarLecturas(ultimas) {
  if (!ultimas.length) {
    $('lecturas').innerHTML = `<div class="vacio"><svg width="40" height="40" viewBox="0 0 24 24" fill="#9aa89a"><path d="M12,3C7.58,3 4,4.79 4,7C4,9.21 7.58,11 12,11C16.42,11 20,9.21 20,7C20,4.79 16.42,3 12,3M4,9V12C4,14.21 7.58,16 12,16C16.42,16 20,14.21 20,12V9C20,11.21 16.42,13 12,13C7.58,13 4,11.21 4,9M4,14V17C4,19.21 7.58,21 12,21C16.42,21 20,19.21 20,17V14C20,16.21 16.42,18 12,18C7.58,18 4,16.21 4,14Z"/></svg>Aún no llegan lecturas en tiempo real.<br><span style="font-size:.8rem">Aparecen aquí cada vez que la Pi publica una medición.</span></div>`;
    $('recibida').textContent = ''; return;
  }
  const u = ultimas[0];
  const alAire = u.humedad_pct === 0 && u.conductividad_uScm === 0;
  let h = `<div class="lectura-cab"><span class="chip">Sensor ${u.sensor_id}</span>
    <span class="hora">🕑 medida ${esc(u.timestamp.replace('T', ' '))}</span>
    ${alAire ? '<span class="alerta ambar">⚠ Humedad y conductividad en 0: el sensor podría estar fuera del sustrato</span>' : ''}
    ${u.humedad_pct >= 99.9 ? '<span class="alerta ambar">⚠ Humedad en 100 %: posible saturación o falta de calibración</span>' : ''}</div><div class="grid">`;
  for (const v of VARS) {
    const e = estado(v.k, u[v.k]);
    h += `<div class="tile ${e}" style="border-top-color:${v.color}">
      <div class="tile-cab"><i style="background:${v.color}"></i>${v.nombre}</div>
      <div class="valor">${num(u[v.k])}<span class="unidad">${v.unidad}</span></div>
      ${RANGOS[v.k] ? `<div class="rango"><span class="badge">${etiqueta(e)}</span><span class="ref">${txtRango(v.k)}</span></div>` : ''}
    </div>`;
  }
  h += `</div><table class="hist"><thead><tr><th>Hora</th><th>Sensor</th><th>Humedad (%)</th><th>Temp (°C)</th><th>pH</th>
        <th>CE (µS/cm)</th><th>N (mg/kg)</th><th>P (mg/kg)</th><th>K (mg/kg)</th></tr></thead><tbody>`;
  for (const l of ultimas) {
    h += `<tr><td>${esc(l.timestamp.slice(11, 19))}</td><td>${l.sensor_id}</td><td>${num(l.humedad_pct)}</td><td>${num(l.temperatura_C)}</td>
          <td>${num(l.ph)}</td><td>${num(l.conductividad_uScm)}</td><td>${num(l.nitrogeno_mgkg)}</td><td>${num(l.fosforo_mgkg)}</td><td>${num(l.potasio_mgkg)}</td></tr>`;
  }
  $('lecturas').innerHTML = h + '</tbody></table>';
  $('recibida').textContent = `última recibida a las ${u.recibida}`;
}

/* ---------------- lotes ---------------- */
async function cargarLista(seleccionar) {
  const lotes = await get('/api/lotes');
  const sel = $('lista'), previo = seleccionar || sel.value;
  sel.innerHTML = lotes.length ? '' : '<option value="">No hay lotes guardados</option>';
  for (const l of lotes) {
    const o = document.createElement('option');
    o.value = l.nombre;
    const f = l.fecha ? l.fecha.split('-').reverse().join('/') : 'sin lecturas';
    const llegada = new Date(l.modificado * 1000).toLocaleString('es-CO', { day:'numeric', month:'short', hour:'2-digit', minute:'2-digit' });
    o.textContent = `${f} · ${l.lecturas} lecturas · ${l.solicitado ? 'solicitado' : 'automático'} · recibido ${llegada}`;
    sel.appendChild(o);
  }
  if (previo && lotes.some(l => l.nombre === previo)) sel.value = previo;
}

async function solicitar() {
  const f = $('fecha').value;
  if (!f) { aviso('Elige una fecha.', 'atencion'); return; }
  const r = await post('/api/solicitar', { fecha: f });
  if (!r.ok) aviso(r.error, 'critico');
}

function botonSolicitud(pend) {
  const b = $('btnSolicitar');
  if (pend) { b.disabled = true; b.innerHTML = `<span class="giro"></span>Esperando lote del ${pend.fecha.split('-').reverse().join('/')}…`; }
  else if (b.disabled) { b.disabled = false; b.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z"/></svg>Solicitar lote`; }
}

function graficarSeleccion() { if ($('lista').value) graficar($('lista').value); }

async function graficar(nombre) {
  const d = await get('/api/lote?nombre=' + encodeURIComponent(nombre));
  if (d.error) { aviso(d.error, 'critico'); return; }
  loteActual = d;
  $('lista').value = nombre;
  $('tarjetaGraf').style.display = '';
  $('tarjetaGraf').open = true;              // al graficar un lote, la seccion se abre
  dibujarLote();
  $('tarjetaGraf').scrollIntoView({ behavior:'smooth', block:'start' });
}

function dibujarLote() {
  const d = loteActual; if (!d) return;
  const filas = d.filas.map(f => ({ t: new Date(f[0]).getTime(), sensor: f[1], v: Object.fromEntries(d.columnas.slice(2).map((c, i) => [c, f[i + 2]])) }));
  const fecha = d.filas.length ? d.filas[0][0].slice(0, 10) : null;
  $('infoGraf').textContent = `${fechaLarga(fecha)} · ${filas.length} lecturas · ${d.nombre}`;
  $('infoLote').textContent = `Mostrando: ${d.nombre}`;
  const sensores = [...new Set(filas.map(f => f.sensor))].sort((a, b) => a - b);
  const serie = (k, nombre, color, dash) => sensores.map((s, i) => ({
    nombre: nombre ? (sensores.length > 1 ? `${nombre} · S${s}` : nombre) : 'Sensor ' + s,
    color: color || COLORES[i % COLORES.length], dash: (color && i) ? '6 4' : null,
    puntos: filas.filter(f => f.sensor === s).map(f => [f.t, f.v[k]]) }));
  const defs = [
    { id:'hum', titulo:'Humedad del sustrato (%)',      unidad:'%',     ancho:true, banda:RANGOS.humedad_pct,   series: serie('humedad_pct') },
    { id:'tem', titulo:'Temperatura del sustrato (°C)', unidad:'°C',    ancho:true, banda:RANGOS.temperatura_C, series: serie('temperatura_C') },
    { id:'ph',  titulo:'pH del sustrato',               unidad:'',      ancho:true, banda:RANGOS.ph,            series: serie('ph') },
    { id:'ce',  titulo:'Conductividad eléctrica (µS/cm)', unidad:'µS/cm', series: serie('conductividad_uScm') },
    { id:'npk', titulo:'Nitrógeno, fósforo y potasio (mg/kg)', unidad:'mg/kg',
      series: [...serie('nitrogeno_mgkg', 'N', '#43a047'), ...serie('fosforo_mgkg', 'P', '#00897b'), ...serie('potasio_mgkg', 'K', '#6d4c41')] },
  ];
  const cont = $('graficas'); cont.innerHTML = '';
  for (const g of defs) {
    const caja = document.createElement('div'); caja.className = 'graf' + (g.ancho ? ' ancho' : '');
    caja.innerHTML = `<div class="graf-cab"><div><div class="graf-tit">${g.titulo}</div><div class="graf-sub">${g.banda ? g.banda.nota : ''}</div></div>
      <div class="leyenda">${g.series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.nombre)}</span>`).join('')}
      ${g.banda ? '<span><i style="background:rgba(76,175,80,.35)"></i>Óptimo</span>' : ''}</div></div><div class="lienzo"></div>`;
    cont.appendChild(caja);
    dibujarGrafica(caja.querySelector('.lienzo'), g);
  }
}

/* ---------------- grafica SVG (misma logica del dashboard) ---------------- */
function pasoBonito(r) { const b = Math.pow(10, Math.floor(Math.log10(r / 5))), x = r / 5 / b; return (x > 5 ? 10 : x > 2 ? 5 : x > 1 ? 2 : 1) * b; }
function escala(vals) {
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (hi - lo < 1e-9) { const d = Math.max(1, Math.abs(lo) * .1); lo -= d; hi += d; }
  else { const p = (hi - lo) * .08; lo = lo >= 0 ? Math.max(0, lo - p) : lo - p; hi += p; }
  const s = Math.max(.1, pasoBonito(hi - lo));
  lo = Math.floor(lo / s) * s; hi = Math.ceil(hi / s) * s;
  const ticks = []; for (let t = lo; t <= hi + s / 2; t += s) ticks.push(+t.toFixed(6));
  return { lo, hi, ticks, dec: Math.max(0, -Math.floor(Math.log10(s) + 1e-9)) };
}
function dibujarGrafica(el, g) {
  const pts = g.series.flatMap(s => s.puntos.filter(p => typeof p[1] === 'number'));
  if (!pts.length) { el.innerHTML = '<div class="sin-datos">Sin datos</div>'; return; }
  const W = Math.max(300, el.clientWidth), H = 250, P = { l:56, r:16, t:12, b:30 };
  const ys = escala(pts.map(p => p[1]));
  let x0 = Math.min(...pts.map(p => p[0])), x1 = Math.max(...pts.map(p => p[0]));
  if (x1 - x0 < 60000) { x0 -= 15 * 60000; x1 += 15 * 60000; }
  const sx = x => P.l + (x - x0) / (x1 - x0) * (W - P.l - P.r);
  const sy = y => { const c = Math.min(Math.max(y, ys.lo), ys.hi); return H - P.b - (c - ys.lo) / (ys.hi - ys.lo) * (H - P.t - P.b); };
  const fmtY = t => t.toLocaleString('es-CO', { minimumFractionDigits: ys.dec, maximumFractionDigits: ys.dec });
  const hhmm = t => new Date(t).toLocaleTimeString('es-CO', { hour:'2-digit', minute:'2-digit', hour12:false });
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
  if (g.banda) s += `<rect x="${P.l}" width="${W - P.l - P.r}" y="${sy(g.banda.optimo[1])}" height="${Math.max(0, sy(g.banda.optimo[0]) - sy(g.banda.optimo[1]))}" fill="rgba(76,175,80,.18)"/>`;
  for (const t of ys.ticks) s += `<line class="grid-l" x1="${P.l}" x2="${W - P.r}" y1="${sy(t)}" y2="${sy(t)}"/><text class="eje" x="${P.l - 8}" y="${sy(t) + 4}" text-anchor="end">${fmtY(t)}</text>`;
  const paso = [5, 10, 15, 30, 60, 120, 180, 360].map(m => m * 60000).find(st => (x1 - x0) / st <= 8) || 6 * 36e5;
  const d0 = new Date(x0); d0.setSeconds(0, 0);
  const minPaso = paso / 60000; let m = Math.ceil((d0.getHours() * 60 + d0.getMinutes()) / minPaso) * minPaso; d0.setHours(0, m);
  for (let t = d0.getTime(); t <= x1; t += paso) s += `<line class="grid-v" x1="${sx(t)}" x2="${sx(t)}" y1="${P.t}" y2="${H - P.b}"/><text class="eje" x="${sx(t)}" y="${H - P.b + 19}" text-anchor="middle">${hhmm(t)}</text>`;
  const marcas = pts.length <= 40 * g.series.length;
  for (const se of g.series) {
    let d = '', prev = null;
    for (const p of se.puntos) {
      if (typeof p[1] !== 'number') { prev = null; continue; }
      d += (prev === null || p[0] - prev > 15 * 60000 ? 'M' : 'L') + sx(p[0]).toFixed(1) + ',' + sy(p[1]).toFixed(1); prev = p[0];
    }
    s += `<path d="${d}" fill="none" stroke="${se.color}" stroke-width="2.3" stroke-linejoin="round" stroke-linecap="round"${se.dash ? ` stroke-dasharray="${se.dash}"` : ''}/>`;
    if (marcas) for (const p of se.puntos) if (typeof p[1] === 'number') s += `<circle cx="${sx(p[0])}" cy="${sy(p[1])}" r="3.2" fill="${se.color}"/>`;
  }
  s += '<g class="hover"></g></svg><div class="tip"></div>';
  el.innerHTML = s;
  const svg = el.querySelector('svg'), capa = el.querySelector('.hover'), tip = el.querySelector('.tip');
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect(), fx = ev.clientX - r.left;
    if (fx < P.l || fx > W - P.r) { capa.innerHTML = ''; tip.style.display = 'none'; return; }
    const xv = x0 + (fx - P.l) / (W - P.l - P.r) * (x1 - x0), vals = [];
    for (const se of g.series) {
      let mejor = null;
      for (const p of se.puntos) if (typeof p[1] === 'number' && (!mejor || Math.abs(p[0] - xv) < Math.abs(mejor[0] - xv))) mejor = p;
      if (mejor && Math.abs(mejor[0] - xv) <= 20 * 60000) vals.push({ se, p: mejor });
    }
    if (!vals.length) { capa.innerHTML = ''; tip.style.display = 'none'; return; }
    const px = sx(vals[0].p[0]);
    capa.innerHTML = `<line class="cursor" x1="${px}" x2="${px}" y1="${P.t}" y2="${H - P.b}"/>` +
      vals.map(v => `<circle cx="${sx(v.p[0])}" cy="${sy(v.p[1])}" r="5" fill="${v.se.color}" stroke="#fff" stroke-width="2"/>`).join('');
    tip.innerHTML = `<b class="t">${hhmm(vals[0].p[0])}</b>` + vals.map(v => `<div><i style="background:${v.se.color}"></i>${esc(v.se.nombre)}: <b>${num(v.p[1])}</b> ${g.unidad}</div>`).join('');
    tip.style.left = Math.min(85, Math.max(15, px / W * 100)) + '%'; tip.style.display = 'block';
  });
  svg.addEventListener('mouseleave', () => { capa.innerHTML = ''; tip.style.display = 'none'; });
}

/* ---------------- ciclo de actualizacion ---------------- */
function pintarRegistro(nuevas) {
  if (!nuevas.length) return;
  const box = $('registro'), abajo = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  for (const r of nuevas) {
    const div = document.createElement('div');
    const cls = r.texto.includes('LOTE') ? 'lote' : r.texto.startsWith('AVISO') || r.texto.includes('Sin respuesta') ? 'aviso' : '';
    div.innerHTML = `<span class="h">${r.hora}</span><span class="${cls}">${esc(r.texto)}</span>`;
    box.appendChild(div);
  }
  while (box.children.length > 400) box.firstChild.remove();
  if (abajo) box.scrollTop = box.scrollHeight;
}

async function ciclo() {
  try {
    const e = await get(`/api/estado?desde=${seq}&aviso=${avisoSeq}`);
    const est = $('estado');
    est.className = 'estado ' + (e.conectado ? 'on' : 'off');
    est.lastElementChild.textContent = e.conectado ? 'CONECTADO AL BROKER' : 'SIN CONEXIÓN · REINTENTANDO';
    pintarLecturas(e.ultimas);
    pintarRegistro(e.registro);
    if (e.registro.length) seq = e.registro[e.registro.length - 1].seq;
    for (const a of e.avisos) { aviso(a.texto, a.tipo); avisoSeq = a.seq; }
    botonSolicitud(e.solicitud);
    if (e.ultimoLote !== ultimoLoteVisto) { ultimoLoteVisto = e.ultimoLote; await cargarLista(); }
    if (e.autoGraficar) await graficar(e.autoGraficar);
  } catch (err) {
    const est = $('estado'); est.className = 'estado off'; est.lastElementChild.textContent = 'RECEPTOR DETENIDO';
  }
}

$('carpeta').textContent = CARPETA;
$('fecha').value = hoy(); $('fecha').max = hoy();
cargarLista();
ciclo(); setInterval(ciclo, 1000);
let ancho = window.innerWidth;
window.addEventListener('resize', () => { if (Math.abs(window.innerWidth - ancho) > 20) { ancho = window.innerWidth; dibujarLote(); } });
// al volver a abrir la seccion se redibuja con el ancho real
$('tarjetaGraf').addEventListener('toggle', e => { if (e.target.open) dibujarLote(); });
window.addEventListener('pagehide', () => navigator.sendBeacon('/api/cerrar', '{}'));
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
