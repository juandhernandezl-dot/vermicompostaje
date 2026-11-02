#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
planta.py  -  APLICACION DE OPERACION DE LA CAMA DE VERMICOMPOSTAJE
===================================================================

Esto NO es el simulador. Es el sistema en funcionamiento: lee los sensores
reales, decide, y abre o cierra las electrovalvulas. Sin modelo fisico, sin
aprendizaje, sin datos inventados. Lo que se ve en pantalla es lo que hay en el
lecho.

    python3 planta.py                 # interfaz
    python3 planta.py --demo          # interfaz con un lecho de mentira, para
                                      # probar sin hardware
    python3 planta.py --test          # autopruebas del control (sin interfaz)
    python3 planta.py --consola COM7 --dirs 4,5,6    # operacion sin interfaz

QUE HACE
--------
  * Conecta con los sensores SN-3002-TR por RS-485 (Modbus RTU / adaptador USB).
  * Conecta con el ESP32 de las electrovalvulas (mismo bus o puerto propio).
  * Cada zona tiene su MODO: apagado, manual, automatico o externo.
  * Sinoptico de la planta con el estado real de cada zona.

TODO EN % DE HUMEDAD VOLUMETRICA
--------------------------------
El simulador trabaja en humedad relativa a capacidad de campo. Aqui NO: se
trabaja en el % VWC que da el sensor, que es lo que el operador puede medir y
comprobar. La capacidad de campo se guarda solo como referencia informativa
("0,72 de capacidad de campo"), no como parte del lazo. Una calibracion mal
puesta ya no puede desplazar el control.

SEGURIDADES (independientes del modo)
-------------------------------------
  1. Sin lectura fiable no se riega. Si un sensor deja de responder, o su sonda
     no hace contacto, la zona se cierra y queda en FALLO: regar a ciegas es
     como inundar el lecho con los ojos cerrados.
  2. Tope de riego continuo por zona. Una valvula pegada o un sensor que no
     sube nunca acabarian regando para siempre.
  3. Descanso minimo entre riegos: el agua tarda en infiltrarse, y sin esa
     espera el control abre y cierra sin parar persiguiendo su propia estela.
  4. Al cerrar la aplicacion, o al perder el bus, todas las valvulas se cierran.
  5. El ESP32 tiene ademas su propio tiempo de guarda por firmware, que no
     depende de este programa.
"""

import argparse
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict

from hardware_mz import (BusCampo, ConfigActuador, ConfigCampo, ConfigSensor,
                         SensorSuelo, TransporteSerie)
from modbus_rtu import EsclavoSimulado, ErrorModbus

try:
    import tkinter as tk
    from tkinter import ttk
    _HAY_TK = True
except ImportError:                                   # pragma: no cover
    _HAY_TK = False

    class _SinTk:
        def __init__(self, *a, **k):
            raise RuntimeError("Tkinter no esta disponible: instala python3-tk")

    class tk:                                          # noqa: N801
        Canvas = Text = Button = Label = Spinbox = Tk = Frame = _SinTk
        StringVar = IntVar = DoubleVar = _SinTk
        TclError = Exception

    class ttk:                                         # noqa: N801
        Frame = Label = Button = Combobox = Checkbutton = Notebook = _SinTk
        Style = Scrollbar = Entry = _SinTk


RUTA = "planta.json"

MODOS = ["apagado", "manual", "auto", "externo"]
ETIQUETA_MODO = {"apagado": "Apagado", "manual": "Manual",
                 "auto": "Automatico", "externo": "Externo"}

UI = {
    "bg": "#0d1117", "panel": "#161b22", "panel2": "#1f2630", "borde": "#2b323c",
    "borde2": "#3a434f", "texto": "#e6edf3", "tenue": "#8b949e",
    "agua": "#38bdf8", "agua_osc": "#0c4a6e", "temp": "#f4795b",
    "ok": "#4ade80", "aviso": "#ffd166", "fallo": "#f87171", "seco": "#a97142",
}
COLOR_ZONA = ["#38bdf8", "#a78bfa", "#4ade80", "#facc15", "#f472b6", "#fb923c",
              "#22d3ee", "#c084fc"]


# =============================================================================
# CONFIGURACION
# =============================================================================

@dataclass
class ZonaPlanta:
    """Una zona de riego: un sensor, una electrovalvula y su modo de control."""
    id: int = 0
    nombre: str = "Zona 1"
    # --- sensor ---
    sensor_dir: int = 1
    vwc_capacidad_campo: float = 35.0   # solo informativo
    offset_humedad: float = 0.0
    tiene_ec: bool = True
    modo_lectura: str = "auto"
    # --- actuador ---
    valvula: int = 0                    # numero de bobina en el ESP32
    # --- control ---
    modo: str = "auto"
    banda_baja: float = 25.0            # % VWC: por debajo, regar
    banda_alta: float = 32.0            # % VWC: por encima, cerrar
    max_riego_min: float = 10.0         # tope de riego continuo
    descanso_min: float = 5.0           # espera minima entre riegos
    activa: bool = True


@dataclass
class ConfigPlanta:
    puerto: str = ""
    baudios: int = 4800
    timeout: float = 1.0
    reintentos: int = 2
    pausa_entre_sensores: float = 0.05
    tiempo_muestreo: float = 30.0       # s entre rondas de lectura
    # --- actuador ---
    esp_puerto: str = ""                # vacio = mismo bus que los sensores
    esp_baudios: int = 4800
    esp_direccion: int = 10
    esp_invertido: bool = False
    esp_verificar: bool = True
    # --- seguridad ---
    fallos_para_cerrar: int = 3         # lecturas seguidas malas antes de FALLO
    zonas: list = field(default_factory=list)

    def guardar(self, ruta=RUTA):
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        os.replace(tmp, ruta)
        return ruta

    @classmethod
    def cargar(cls, ruta=RUTA):
        if not os.path.exists(ruta):
            return None
        with open(ruta, "r", encoding="utf-8") as f:
            d = json.load(f)
        cfg = cls(**{k: v for k, v in d.items() if k != "zonas"})
        cfg.zonas = [ZonaPlanta(**z) for z in d.get("zonas", [])]
        return cfg

    @classmethod
    def por_defecto(cls, n=3):
        cfg = cls()
        cfg.zonas = [ZonaPlanta(id=i, nombre=f"Zona {i + 1}", sensor_dir=i + 1,
                                valvula=i) for i in range(n)]
        return cfg

    def comprobar(self):
        problemas = []
        vistas, valvulas = {}, {}
        for z in self.zonas:
            if not 1 <= z.sensor_dir <= 254:
                problemas.append(f"{z.nombre}: direccion {z.sensor_dir} fuera de 1..254")
            if z.sensor_dir in vistas:
                problemas.append(f"la direccion {z.sensor_dir} esta repetida en "
                                 f"{vistas[z.sensor_dir]} y {z.nombre}")
            vistas[z.sensor_dir] = z.nombre
            if z.valvula in valvulas:
                problemas.append(f"la valvula {z.valvula} esta asignada a "
                                 f"{valvulas[z.valvula]} y a {z.nombre}")
            valvulas[z.valvula] = z.nombre
            if z.banda_alta <= z.banda_baja:
                problemas.append(f"{z.nombre}: la banda alta ({z.banda_alta}) debe "
                                 f"ser mayor que la baja ({z.banda_baja})")
            if z.banda_alta - z.banda_baja < 2.0:
                problemas.append(f"{z.nombre}: la banda es de solo "
                                 f"{z.banda_alta - z.banda_baja:.1f} puntos; con la "
                                 f"resolucion del sensor (0,1 %) y el retardo del "
                                 f"suelo, una banda tan estrecha hace que la valvula "
                                 f"no pare de conmutar")
        mismo_bus = not (self.esp_puerto and self.esp_puerto != self.puerto)
        if mismo_bus and self.esp_direccion in vistas:
            problemas.append(f"el ESP32 usa la direccion {self.esp_direccion}, la "
                             f"misma que {vistas[self.esp_direccion]}, y comparten bus")
        return problemas

    # ---- puente con los drivers de hardware_mz -------------------------------
    def a_config_campo(self):
        c = ConfigCampo(puerto=self.puerto, baudios=self.baudios,
                        timeout=self.timeout, reintentos=self.reintentos,
                        tiempo_muestreo=self.tiempo_muestreo)
        c.pausa_entre_sensores = self.pausa_entre_sensores
        c.sensores = [ConfigSensor(zona=z.id, direccion=z.sensor_dir,
                                   etiqueta=z.nombre, activo=z.activa,
                                   vwc_capacidad_campo=z.vwc_capacidad_campo,
                                   offset_humedad=z.offset_humedad,
                                   tiene_ec=z.tiene_ec,
                                   modo_lectura=z.modo_lectura)
                      for z in self.zonas]
        c.actuador = ConfigActuador(puerto=self.esp_puerto, baudios=self.esp_baudios,
                                    direccion=self.esp_direccion,
                                    invertido=self.esp_invertido,
                                    verificar=self.esp_verificar)
        return c


# =============================================================================
# CONTROL DE UNA ZONA
# =============================================================================

class ControlZona:
    """
    Histeresis con memoria de tiempos y bloqueo por fallo.

    Devuelve True (regar) o False (cerrar) y, sobre todo, POR QUE: en operacion
    real, saber por que la valvula esta como esta importa tanto como el estado.
    """

    def __init__(self, cfg: ZonaPlanta, fallos_para_cerrar=3):
        self.cfg = cfg
        self.fallos_para_cerrar = fallos_para_cerrar
        self.abierta = False
        self.manual = False
        self.fallo = ""
        self.fallos_seguidos = 0
        self.motivo = "arranque"
        self.desde = time.time()          # instante del ultimo cambio de estado
        self.fin_riego = 0.0              # instante en que termino el ultimo riego
        self.riegos = 0
        self.segundos_riego = 0.0

    # ---- utilidades ----------------------------------------------------------
    def _cambiar(self, abierta, motivo):
        ahora = time.time()
        if self.abierta and not abierta:
            self.segundos_riego += ahora - self.desde
            self.fin_riego = ahora
        if abierta and not self.abierta:
            self.riegos += 1
        if abierta != self.abierta:
            self.desde = ahora
        self.abierta = abierta
        self.motivo = motivo
        return abierta

    @property
    def tiempo_en_estado(self):
        return time.time() - self.desde

    @property
    def descanso_restante(self):
        if self.abierta or not self.fin_riego:
            return 0.0
        return max(0.0, self.cfg.descanso_min * 60.0 - (time.time() - self.fin_riego))

    # ---- decision ------------------------------------------------------------
    def decidir(self, lectura):
        """lectura: dict del sensor, o None si no hubo respuesta."""
        c = self.cfg

        # --- 1. seguridad: sin medida fiable no se riega ---
        if lectura is None:
            self.fallos_seguidos += 1
            if self.fallos_seguidos >= self.fallos_para_cerrar:
                self.fallo = (f"el sensor no responde ({self.fallos_seguidos} "
                              f"lecturas seguidas)")
                return self._cambiar(False, "FALLO: sensor sin respuesta")
            return self._cambiar(self.abierta, "sin lectura, se mantiene")
        if lectura.get("sin_contacto"):
            self.fallos_seguidos += 1
            if self.fallos_seguidos >= self.fallos_para_cerrar:
                self.fallo = "la sonda no hace contacto con el sustrato"
                return self._cambiar(False, "FALLO: sonda sin contacto")
            return self._cambiar(self.abierta, "lectura dudosa, se mantiene")
        self.fallos_seguidos = 0
        self.fallo = ""
        vwc = lectura["vwc"]

        # --- 2. modos que no calculan nada ---
        if not c.activa or c.modo == "apagado":
            return self._cambiar(False, "zona apagada")
        if c.modo == "externo":
            return self.abierta        # manda otro proceso; aqui no se toca
        if c.modo == "manual":
            if self.manual and vwc >= c.banda_alta:
                return self._cambiar(False, f"manual, pero {vwc:.1f} % supera la "
                                            f"banda alta: cierre de seguridad")
            return self._cambiar(self.manual, "manual")

        # --- 3. automatico: tope de riego continuo ---
        if self.abierta and self.tiempo_en_estado > c.max_riego_min * 60.0:
            self.fallo = (f"llevaba {c.max_riego_min:.0f} min regando sin alcanzar "
                          f"{c.banda_alta:.1f} %: revisa la valvula o el sensor")
            return self._cambiar(False, "FALLO: tope de riego continuo")

        # --- 4. histeresis ---
        if self.abierta:
            if vwc >= c.banda_alta:
                return self._cambiar(False, f"alcanzada la banda alta "
                                            f"({vwc:.1f} >= {c.banda_alta:.1f} %)")
            return self._cambiar(True, f"regando hasta {c.banda_alta:.1f} %")
        if vwc <= c.banda_baja:
            resto = self.descanso_restante
            if resto > 0:
                return self._cambiar(False, f"seca, pero en descanso "
                                            f"({resto / 60.0:.1f} min)")
            return self._cambiar(True, f"por debajo de la banda "
                                       f"({vwc:.1f} <= {c.banda_baja:.1f} %)")
        return self._cambiar(False, "dentro de la banda")

    def estado(self, lectura):
        """Clasificacion para el sinoptico."""
        if self.fallo:
            return "fallo"
        if lectura is None:
            return "sin_datos"
        v = lectura["vwc"]
        if v < self.cfg.banda_baja:
            return "seca"
        if v > self.cfg.banda_alta:
            return "humeda"
        return "en_banda"


# =============================================================================
# MOTOR DE LA PLANTA
# =============================================================================

class MotorPlanta:
    """
    Hilo unico duenno del bus: lee, decide y actua. La interfaz solo mira.

    Todo el estado compartido se publica en 'instantanea()' bajo candado, de modo
    que la interfaz nunca toca el puerto serie ni bloquea esperando al bus.
    """

    HISTORIAL = 720          # muestras guardadas por zona para la grafica
    TS_MINIMO = 1.0          # s: no tiene sentido sondear el suelo mas rapido

    def __init__(self, cfg: ConfigPlanta, log=None, esclavos_simulados=None):
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.bus = BusCampo(cfg.a_config_campo(), log=self.log,
                            esclavos_simulados=esclavos_simulados)
        self.controles = {z.id: ControlZona(z, cfg.fallos_para_cerrar)
                          for z in cfg.zonas}
        self.lecturas = {}
        self.historial = {z.id: deque(maxlen=self.HISTORIAL) for z in cfg.zonas}
        self.eventos = deque(maxlen=400)
        self._candado = threading.Lock()
        self._corriendo = threading.Event()
        self._forzar = threading.Event()
        self._hilo = None
        self.rondas = 0
        self.ultima_ronda = 0.0
        self.proxima_ronda = 0.0
        self.duracion_ronda = 0.0
        self.error_bus = ""

    # ---- ciclo de vida -------------------------------------------------------
    def arrancar(self):
        if self._hilo is not None and self._hilo.is_alive():
            return self
        self.bus.conectar()
        self._corriendo.set()
        self._hilo = threading.Thread(target=self._bucle, name="motor-planta",
                                      daemon=True)
        self._hilo.start()
        self.evento("Planta en marcha.")
        return self

    def parar(self):
        self._corriendo.clear()
        if self._hilo is not None:
            self._hilo.join(timeout=4.0)
        for c in self.controles.values():
            c._cambiar(False, "planta detenida")
        try:
            self.bus.cerrar(n_zonas=len(self.cfg.zonas))
        except Exception:
            pass
        self.evento("Planta detenida; valvulas cerradas.")

    @property
    def activo(self):
        return self._hilo is not None and self._hilo.is_alive()

    def leer_ahora(self):
        self._forzar.set()

    def evento(self, texto):
        with self._candado:
            self.eventos.append((time.time(), texto))
        self.log(texto)

    # ---- mando manual --------------------------------------------------------
    def set_manual(self, zona, abierta):
        c = self.controles.get(zona)
        if c is None:
            return
        c.manual = bool(abierta)
        self.evento(f"{c.cfg.nombre}: mando manual -> "
                    f"{'ABIERTA' if abierta else 'cerrada'}")
        self._forzar.set()

    def set_modo(self, zona, modo):
        c = self.controles.get(zona)
        if c is None or modo not in MODOS:
            return
        c.cfg.modo = modo
        if modo != "manual":
            c.manual = False
        self.evento(f"{c.cfg.nombre}: modo -> {ETIQUETA_MODO[modo]}")
        self._forzar.set()

    def reconocer_fallo(self, zona):
        c = self.controles.get(zona)
        if c is None:
            return
        c.fallo = ""
        c.fallos_seguidos = 0
        self.evento(f"{c.cfg.nombre}: fallo reconocido.")

    def cerrar_todo(self):
        for c in self.controles.values():
            c.manual = False
            c._cambiar(False, "cierre general")
        try:
            self.bus.aplicar_valvulas(self._niveles())
        except Exception:
            pass
        self.evento("CIERRE GENERAL de todas las valvulas.")

    # ---- lazo ----------------------------------------------------------------
    def _niveles(self):
        n = max([z.valvula for z in self.cfg.zonas] + [0]) + 1
        niveles = [0.0] * n
        for z in self.cfg.zonas:
            c = self.controles[z.id]
            niveles[z.valvula] = 1.0 if c.abierta else 0.0
        return niveles

    def _ronda(self):
        t0 = time.monotonic()
        lecturas = self.bus.ciclo()
        cambios = []
        with self._candado:
            self.lecturas = lecturas
            for z in self.cfg.zonas:
                c = self.controles[z.id]
                antes, fallo_antes = c.abierta, c.fallo
                lec = lecturas.get(z.id)
                c.decidir(lec)
                self.historial[z.id].append(
                    (time.time(), lec["vwc"] if lec else None, c.abierta))
                if c.abierta != antes:
                    cambios.append(f"{z.nombre}: valvula "
                                   f"{'ABIERTA' if c.abierta else 'cerrada'} "
                                   f"({c.motivo})")
                if c.fallo and c.fallo != fallo_antes:
                    cambios.append(f"{z.nombre}: FALLO - {c.fallo}")
            niveles = self._niveles()
        if not self.bus.aplicar_valvulas(niveles):
            err = self.bus.actuador.ultimo_error
            if err != self.error_bus:
                self.error_bus = err
                cambios.append(f"Las valvulas no confirman el mando: {err}")
        else:
            self.error_bus = ""
        for m in cambios:
            self.evento(m)
        self.rondas += 1
        self.ultima_ronda = time.time()
        self.duracion_ronda = time.monotonic() - t0

    def _bucle(self):
        # Suelo de 1 s: sondear mas rapido no aporta nada -el suelo no cambia a
        # ese ritmo- y solo castiga el bus y el desgaste de las electrovalvulas.
        Ts = max(self.TS_MINIMO, self.cfg.tiempo_muestreo)
        proximo = time.monotonic()
        self.proxima_ronda = time.time()
        while self._corriendo.is_set():
            if time.monotonic() >= proximo or self._forzar.is_set():
                self._forzar.clear()
                proximo = time.monotonic() + Ts
                self.proxima_ronda = time.time() + Ts
                try:
                    self._ronda()
                except Exception as e:
                    self.evento(f"Error en la ronda: {e}")
            time.sleep(0.05)

    # ---- lectura para la interfaz --------------------------------------------
    def instantanea(self):
        with self._candado:
            zonas = []
            for z in self.cfg.zonas:
                c = self.controles[z.id]
                lec = self.lecturas.get(z.id)
                zonas.append({
                    "id": z.id, "nombre": z.nombre, "cfg": z,
                    "lectura": lec, "abierta": c.abierta, "modo": z.modo,
                    "manual": c.manual, "fallo": c.fallo, "motivo": c.motivo,
                    "estado": c.estado(lec),
                    "tiempo_en_estado": c.tiempo_en_estado,
                    "descanso": c.descanso_restante,
                    "riegos": c.riegos, "minutos_riego": c.segundos_riego / 60.0,
                })
            return {
                "zonas": zonas, "rondas": self.rondas,
                "ultima_ronda": self.ultima_ronda,
                "proxima_ronda": self.proxima_ronda,
                "duracion_ronda": self.duracion_ronda,
                "activo": self.activo, "error_bus": self.error_bus,
                "eventos": list(self.eventos)[-60:],
            }

    def serie(self, zona):
        with self._candado:
            return list(self.historial.get(zona, ()))


# =============================================================================
# LECHO DE MENTIRA (solo para --demo, no interviene en operacion real)
# =============================================================================

class LechoFalso(EsclavoSimulado):
    """Sensor de pega con un modelo elemental: se seca solo y se moja al regar.

    No pretende ser fisica, solo dar algo vivo con que probar la interfaz y el
    control cuando no hay hardware delante.
    """

    def __init__(self, unidad, valvulas, indice, vwc=28.0, temp=24.0):
        super().__init__(unidad, {0x07D0: unidad, 0x07D1: 1})
        self.valvulas, self.indice = valvulas, indice
        self.vwc, self.temp = vwc, temp
        self.t = time.monotonic()

    def antes_de_leer(self, direccion, cantidad):
        import random
        ahora = time.monotonic()
        dt, self.t = ahora - self.t, ahora
        abierta = (self.indice < len(self.valvulas.bobinas)
                   and self.valvulas.bobinas[self.indice])
        self.vwc += (2.0 if abierta else -0.25) * dt      # %/s
        self.vwc = max(0.5, min(60.0, self.vwc))
        self.temp += (24.0 - self.temp) * 0.01 * dt
        self.registros[0x0000] = int(round(self.vwc * 10))
        self.registros[0x0001] = int(round(self.temp * 10)) & 0xFFFF
        self.registros[0x0002] = int(20 * self.vwc)
        self.registros[0x0003] = 68
        for r, v in ((0x0004, 110), (0x0005, 40), (0x0006, 190), (0x0007, 120),
                     (0x0008, 300), (0x000C, 800)):
            self.registros.setdefault(r, v)
        for r in (0x0050, 0x0051, 0x0052, 0x0053):
            self.registros.setdefault(r, 0)
        for r in (0x0022, 0x0023, 0x0024):
            self.registros.setdefault(r, 0)


def banco_demo(cfg: ConfigPlanta):
    esp = EsclavoSimulado(cfg.esp_direccion, {i: 0 for i in range(8)},
                          bobinas=max(4, len(cfg.zonas)))
    esclavos = {cfg.esp_direccion: esp}
    for i, z in enumerate(cfg.zonas):
        esclavos[z.sensor_dir] = LechoFalso(z.sensor_dir, esp, z.valvula,
                                            vwc=30.0 - 6.0 * i, temp=23.0 + i)
    return esclavos


# =============================================================================
# INTERFAZ
# =============================================================================

def _mezcla(c1, c2, t):
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    t = max(0.0, min(1.0, t))
    r, g, b = (a + (x - a) * t for a, x in zip(rgb(c1), rgb(c2)))
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def color_humedad(vwc, baja, alta):
    if vwc < baja:
        return _mezcla(UI["seco"], UI["agua"], max(0.0, vwc / max(1e-6, baja)) * 0.7)
    if vwc <= alta:
        return _mezcla(UI["agua"], "#0ea5e9", (vwc - baja) / max(1e-6, alta - baja))
    return _mezcla("#0ea5e9", "#1e3a8a", min(1.0, (vwc - alta) / 15.0))


class AppPlanta:
    """Ventana de operacion: sinoptico, control por zona y configuracion."""

    REFRESCO_MS = 500
    ESCALA_MAX = 60.0          # tope del eje de humedad en el sinoptico (% VWC)

    def __init__(self, root, cfg: ConfigPlanta, demo=False):
        self.root, self.cfg, self.demo = root, cfg, demo
        self.motor = None
        self.filas = []
        self.mensajes = deque(maxlen=500)
        root.title("Cama de vermicompostaje - operacion")
        root.configure(bg=UI["bg"])
        root.geometry("1180x760")
        root.minsize(900, 560)
        self._estilo()
        self._construir()
        root.protocol("WM_DELETE_WINDOW", self._cerrar)
        self._refrescar()

    # ---- estilo --------------------------------------------------------------
    def _estilo(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=UI["bg"], foreground=UI["texto"])
        st.configure("TFrame", background=UI["bg"])
        st.configure("Card.TFrame", background=UI["panel"])
        st.configure("TLabel", background=UI["bg"], foreground=UI["texto"])
        st.configure("Card.TLabel", background=UI["panel"], foreground=UI["texto"])
        st.configure("Tenue.TLabel", background=UI["panel"], foreground=UI["tenue"])
        st.configure("TButton", background=UI["panel2"], foreground=UI["texto"],
                     borderwidth=0, padding=6)
        st.map("TButton", background=[("active", UI["borde2"])])
        st.configure("TCombobox", fieldbackground=UI["panel2"],
                     background=UI["panel2"], foreground=UI["texto"],
                     arrowcolor=UI["texto"])
        st.map("TCombobox", fieldbackground=[("readonly", UI["panel2"])],
               foreground=[("readonly", UI["texto"])],
               selectbackground=[("readonly", UI["panel2"])],
               selectforeground=[("readonly", UI["texto"])])
        st.configure("TNotebook", background=UI["bg"], borderwidth=0)
        st.configure("TNotebook.Tab", background=UI["panel"], foreground=UI["tenue"],
                     padding=(18, 8), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", UI["panel2"])],
               foreground=[("selected", UI["texto"])])
        st.configure("TCheckbutton", background=UI["panel"], foreground=UI["texto"])

    # ---- estructura ----------------------------------------------------------
    def _construir(self):
        barra = ttk.Frame(self.root)
        barra.pack(fill="x", padx=10, pady=(10, 4))
        self.btn_marcha = ttk.Button(barra, text="Arrancar planta",
                                     command=self._arrancar_parar)
        self.btn_marcha.pack(side="left")
        tk.Button(barra, text="CERRAR TODO", command=self._cerrar_todo,
                  bg="#7f1d1d", fg="#fee2e2", relief="flat", padx=14, pady=5,
                  font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=8)
        ttk.Button(barra, text="Leer ahora", command=self._leer_ahora
                   ).pack(side="left", padx=4)
        self.lbl_estado = ttk.Label(barra, text="Detenida", foreground=UI["tenue"])
        self.lbl_estado.pack(side="right")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.tab_planta = ttk.Frame(self.nb)
        self.tab_cfg = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        for t, n in ((self.tab_planta, "Planta"), (self.tab_cfg, "Configuracion"),
                     (self.tab_log, "Registro")):
            self.nb.add(t, text=n)
        self._tab_planta()
        self._tab_config()
        self._tab_log()

    # ---- pestana PLANTA ------------------------------------------------------
    def _tab_planta(self):
        self.lienzo = tk.Canvas(self.tab_planta, bg=UI["panel"],
                                highlightthickness=0, height=300)
        self.lienzo.pack(fill="x", pady=(6, 6))

        inf = ttk.Frame(self.tab_planta, style="Card.TFrame")
        inf.pack(side="bottom", fill="x")
        ttk.Label(inf, text="  Control por zona", style="Card.TLabel",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 2))
        cab = ttk.Frame(inf, style="Card.TFrame")
        cab.pack(fill="x", padx=10)
        for txt, w in (("Zona", 12), ("Modo", 12), ("Banda %VWC", 16),
                       ("Valvula", 12), ("Humedad", 10), ("Temp.", 8),
                       ("Motivo / fallo", 40)):
            ttk.Label(cab, text=txt, style="Tenue.TLabel", width=w
                      ).pack(side="left", padx=2)
        marco = ttk.Frame(inf, style="Card.TFrame")
        marco.pack(fill="x", padx=10, pady=(2, 10))
        self.filas = []
        for z in self.cfg.zonas:
            f = ttk.Frame(marco, style="Card.TFrame")
            f.pack(fill="x", pady=1)
            tk.Label(f, text=f" {z.nombre} ", bg=COLOR_ZONA[z.id % len(COLOR_ZONA)],
                     fg="#0b0f14", font=("TkDefaultFont", 9, "bold"), width=11
                     ).pack(side="left", padx=2)
            v_modo = tk.StringVar(value=ETIQUETA_MODO.get(z.modo, "Automatico"))
            cb = ttk.Combobox(f, textvariable=v_modo, width=10, state="readonly",
                              values=[ETIQUETA_MODO[m] for m in MODOS])
            cb.pack(side="left", padx=2)
            cb.bind("<<ComboboxSelected>>", lambda e, i=z.id: self._cambio_modo(i))
            v_lo = tk.DoubleVar(value=z.banda_baja)
            v_hi = tk.DoubleVar(value=z.banda_alta)
            for var in (v_lo, v_hi):
                tk.Spinbox(f, textvariable=var, from_=1.0, to=80.0, increment=0.5,
                           width=6, bg=UI["panel2"], fg=UI["texto"], relief="flat",
                           justify="center",
                           command=lambda i=z.id: self._cambio_banda(i)
                           ).pack(side="left", padx=2)
            btn = tk.Button(f, text="CERRADA", width=10, relief="flat",
                            bg=UI["panel2"], fg=UI["tenue"],
                            command=lambda i=z.id: self._toggle_valvula(i))
            btn.pack(side="left", padx=6)
            l_hum = ttk.Label(f, text="-", style="Card.TLabel", width=10)
            l_hum.pack(side="left", padx=2)
            l_t = ttk.Label(f, text="-", style="Card.TLabel", width=8)
            l_t.pack(side="left", padx=2)
            l_m = ttk.Label(f, text="-", style="Tenue.TLabel", anchor="w")
            l_m.pack(side="left", fill="x", expand=True, padx=6)
            self.filas.append({"z": z, "modo": v_modo, "lo": v_lo, "hi": v_hi,
                               "btn": btn, "hum": l_hum, "temp": l_t, "motivo": l_m})

        medio = ttk.Frame(self.tab_planta, style="Card.TFrame")
        medio.pack(fill="both", expand=True, pady=(6, 6))
        ttk.Label(medio, text="  Humedad reciente", style="Tenue.TLabel"
                  ).pack(anchor="w", pady=(6, 0))
        self.lienzo_tend = tk.Canvas(medio, bg=UI["panel"], highlightthickness=0,
                                     height=130)
        self.lienzo_tend.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    # ---- pestana CONFIGURACION ----------------------------------------------
    def _tab_config(self):
        raiz = self.tab_cfg
        caja = ttk.Frame(raiz, style="Card.TFrame")
        caja.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(caja, text="  Conexion", style="Card.TLabel",
                  font=("TkDefaultFont", 10, "bold")
                  ).grid(row=0, column=0, columnspan=8, sticky="w", pady=(6, 4))
        self.v_puerto = tk.StringVar(value=self.cfg.puerto or "")
        self.v_baud = tk.StringVar(value=str(self.cfg.baudios))
        self.v_ts = tk.DoubleVar(value=self.cfg.tiempo_muestreo)
        self.v_esp_puerto = tk.StringVar(value=self.cfg.esp_puerto)
        self.v_esp_dir = tk.IntVar(value=self.cfg.esp_direccion)
        self.v_esp_inv = tk.IntVar(value=1 if self.cfg.esp_invertido else 0)

        ttk.Label(caja, text="Puerto sensores", style="Tenue.TLabel"
                  ).grid(row=1, column=0, sticky="e", padx=6)
        self.cb_puerto = ttk.Combobox(caja, textvariable=self.v_puerto, width=18)
        self.cb_puerto.grid(row=1, column=1, sticky="w")
        ttk.Button(caja, text="Buscar", command=self._buscar_puertos
                   ).grid(row=1, column=2, padx=4)
        ttk.Label(caja, text="Baudios", style="Tenue.TLabel").grid(row=1, column=3,
                                                                   sticky="e", padx=6)
        ttk.Combobox(caja, textvariable=self.v_baud, width=7, state="readonly",
                     values=["2400", "4800", "9600"]).grid(row=1, column=4, sticky="w")
        ttk.Label(caja, text="Muestreo (s)", style="Tenue.TLabel"
                  ).grid(row=1, column=5, sticky="e", padx=6)
        tk.Spinbox(caja, textvariable=self.v_ts, from_=2.0, to=3600.0, increment=5.0,
                   width=7, bg=UI["panel2"], fg=UI["texto"], relief="flat",
                   justify="center").grid(row=1, column=6, sticky="w", padx=(0, 10))

        ttk.Label(caja, text="ESP32: puerto", style="Tenue.TLabel"
                  ).grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.cb_esp = ttk.Combobox(caja, textvariable=self.v_esp_puerto, width=18)
        self.cb_esp.grid(row=2, column=1, sticky="w")
        ttk.Label(caja, text="(vacio = mismo bus)", style="Tenue.TLabel"
                  ).grid(row=2, column=2, sticky="w")
        ttk.Label(caja, text="Direccion", style="Tenue.TLabel").grid(row=2, column=3,
                                                                     sticky="e", padx=6)
        tk.Spinbox(caja, textvariable=self.v_esp_dir, from_=1, to=254, width=5,
                   bg=UI["panel2"], fg=UI["texto"], relief="flat", justify="center"
                   ).grid(row=2, column=4, sticky="w")
        ttk.Checkbutton(caja, text="rele invertido", variable=self.v_esp_inv
                        ).grid(row=2, column=5, columnspan=2, sticky="w", padx=6)

        caja2 = ttk.Frame(raiz, style="Card.TFrame")
        caja2.pack(fill="x", padx=8, pady=4)
        ttk.Label(caja2, text="  Zonas: sensor, valvula y calibracion",
                  style="Card.TLabel", font=("TkDefaultFont", 10, "bold")
                  ).pack(anchor="w", pady=(6, 2))
        cab = ttk.Frame(caja2, style="Card.TFrame")
        cab.pack(fill="x", padx=10)
        for txt, w in (("Zona", 12), ("Dir. sensor", 11), ("Valvula", 8),
                       ("Activa", 7), ("Offset %", 9), ("EC?", 5),
                       ("Max riego min", 13), ("Descanso min", 12)):
            ttk.Label(cab, text=txt, style="Tenue.TLabel", width=w
                      ).pack(side="left", padx=2)
        marco = ttk.Frame(caja2, style="Card.TFrame")
        marco.pack(fill="x", padx=10, pady=(2, 8))
        self.filas_cfg = []
        for z in self.cfg.zonas:
            f = ttk.Frame(marco, style="Card.TFrame")
            f.pack(fill="x", pady=1)
            v_nombre = tk.StringVar(value=z.nombre)
            tk.Entry(f, textvariable=v_nombre, width=12, bg=UI["panel2"],
                     fg=UI["texto"], relief="flat").pack(side="left", padx=2)
            campos = {}
            for clave, var, lo, hi, paso, ancho in (
                    ("sensor_dir", tk.IntVar(value=z.sensor_dir), 1, 254, 1, 8),
                    ("valvula", tk.IntVar(value=z.valvula), 0, 63, 1, 6)):
                tk.Spinbox(f, textvariable=var, from_=lo, to=hi, increment=paso,
                           width=ancho, bg=UI["panel2"], fg=UI["texto"],
                           relief="flat", justify="center").pack(side="left", padx=6)
                campos[clave] = var
            v_act = tk.IntVar(value=1 if z.activa else 0)
            ttk.Checkbutton(f, variable=v_act).pack(side="left", padx=14)
            v_off = tk.DoubleVar(value=z.offset_humedad)
            tk.Spinbox(f, textvariable=v_off, from_=-30.0, to=30.0, increment=0.1,
                       width=7, bg=UI["panel2"], fg=UI["texto"], relief="flat",
                       justify="center").pack(side="left", padx=4)
            v_ec = tk.IntVar(value=1 if z.tiene_ec else 0)
            ttk.Checkbutton(f, variable=v_ec).pack(side="left", padx=10)
            v_max = tk.DoubleVar(value=z.max_riego_min)
            tk.Spinbox(f, textvariable=v_max, from_=0.5, to=120.0, increment=0.5,
                       width=8, bg=UI["panel2"], fg=UI["texto"], relief="flat",
                       justify="center").pack(side="left", padx=16)
            v_desc = tk.DoubleVar(value=z.descanso_min)
            tk.Spinbox(f, textvariable=v_desc, from_=0.0, to=240.0, increment=0.5,
                       width=8, bg=UI["panel2"], fg=UI["texto"], relief="flat",
                       justify="center").pack(side="left", padx=12)
            self.filas_cfg.append({"z": z, "nombre": v_nombre, "activa": v_act,
                                   "off": v_off, "ec": v_ec, "max": v_max,
                                   "desc": v_desc, **campos})

        bar = ttk.Frame(raiz)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Guardar", command=self._guardar).pack(side="left")
        ttk.Button(bar, text="Escanear bus", command=self._escanear
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Probar lectura", command=self._probar
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Anadir zona", command=self._anadir_zona
                   ).pack(side="right")
        self.lbl_cfg = ttk.Label(raiz, text="", wraplength=1000, justify="left")
        self.lbl_cfg.pack(anchor="w", padx=12, pady=(0, 8))
        self._buscar_puertos()

    def _tab_log(self):
        self.txt = tk.Text(self.tab_log, bg=UI["panel"], fg=UI["texto"],
                           relief="flat", font=("TkFixedFont", 9), wrap="word")
        self.txt.pack(fill="both", expand=True, pady=8)
        self.txt.configure(state="disabled")

    # ---- acciones ------------------------------------------------------------
    def log(self, m):
        self.mensajes.append(f"{time.strftime('%H:%M:%S')}  {m}")
        if hasattr(self, "txt"):
            self.txt.configure(state="normal")
            self.txt.insert("end", self.mensajes[-1] + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")

    def _buscar_puertos(self):
        puertos = []
        try:
            puertos = [p for p, _ in TransporteSerie.puertos_disponibles()]
        except Exception:
            pass
        if self.demo:
            puertos = ["simulado"] + puertos
        self.cb_puerto["values"] = puertos
        self.cb_esp["values"] = [""] + puertos
        if not self.v_puerto.get() and puertos:
            self.v_puerto.set(puertos[0])

    def _recoger(self):
        c = self.cfg
        c.puerto = self.v_puerto.get().strip()
        c.baudios = int(self.v_baud.get())
        c.tiempo_muestreo = float(self.v_ts.get())
        c.esp_puerto = self.v_esp_puerto.get().strip()
        c.esp_direccion = int(self.v_esp_dir.get())
        c.esp_invertido = bool(self.v_esp_inv.get())
        for f in getattr(self, "filas_cfg", []):
            z = f["z"]
            z.nombre = f["nombre"].get().strip() or z.nombre
            z.sensor_dir = int(f["sensor_dir"].get())
            z.valvula = int(f["valvula"].get())
            z.activa = bool(f["activa"].get())
            z.offset_humedad = float(f["off"].get())
            z.tiene_ec = bool(f["ec"].get())
            z.max_riego_min = float(f["max"].get())
            z.descanso_min = float(f["desc"].get())
        for f in self.filas:
            z = f["z"]
            z.banda_baja = float(f["lo"].get())
            z.banda_alta = float(f["hi"].get())
        return c

    def _guardar(self):
        cfg = self._recoger()
        problemas = cfg.comprobar()
        ruta = cfg.guardar()
        for p in problemas:
            self.log(f"AVISO: {p}")
        self.lbl_cfg.configure(
            text=(f"Guardado en {ruta}. " + " / ".join(problemas)) if problemas
            else f"Guardado en {ruta}.",
            foreground=UI["aviso"] if problemas else UI["ok"])

    def _anadir_zona(self):
        n = len(self.cfg.zonas)
        self.cfg.zonas.append(ZonaPlanta(id=n, nombre=f"Zona {n + 1}",
                                         sensor_dir=n + 1, valvula=n))
        self.lbl_cfg.configure(
            text="Zona anadida. Guarda y reinicia la aplicacion para verla.",
            foreground=UI["aviso"])

    def _bus_puntual(self):
        cfg = self._recoger()
        esclavos = banco_demo(cfg) if (self.demo or cfg.puerto == "simulado") else None
        bus = BusCampo(cfg.a_config_campo(), log=self.log,
                       esclavos_simulados=esclavos)
        bus.conectar()
        return bus

    def _escanear(self):
        if self.motor is not None:
            self.lbl_cfg.configure(text="Para la planta antes de escanear el bus.",
                                   foreground=UI["aviso"])
            return
        try:
            bus = self._bus_puntual()
            hallados = bus.escanear(1, 32)
            bus.cerrar()
        except Exception as e:
            self.lbl_cfg.configure(text=f"No se pudo escanear: {e}",
                                   foreground=UI["fallo"])
            return
        if not hallados:
            self.lbl_cfg.configure(
                text="Nada en el bus. Revisa A/B sin invertir, alimentacion, GND "
                     "comun, 120 ohm en los extremos y la velocidad.",
                foreground=UI["fallo"])
        else:
            self.lbl_cfg.configure(
                text="Encontrado: " + ", ".join(
                    f"{h['direccion']} ({h['tipo']})" for h in hallados),
                foreground=UI["ok"])

    def _probar(self):
        if self.motor is not None:
            self.motor.leer_ahora()
            self.lbl_cfg.configure(text="Lectura forzada.", foreground=UI["ok"])
            return
        try:
            bus = self._bus_puntual()
            lecturas = bus.ciclo()
            bus.cerrar()
        except Exception as e:
            self.lbl_cfg.configure(text=f"Fallo la lectura: {e}",
                                   foreground=UI["fallo"])
            return
        partes = [f"{self.cfg.zonas[i].nombre}: {l['vwc']:.1f} % / "
                  f"{l['temperatura']:.1f} C" for i, l in sorted(lecturas.items())
                  if i < len(self.cfg.zonas)]
        self.lbl_cfg.configure(
            text=(" | ".join(partes) if partes else "Ningun sensor respondio."),
            foreground=UI["ok"] if partes else UI["fallo"])

    def _arrancar_parar(self):
        if self.motor is not None:
            self.motor.parar()
            self.motor = None
            self.btn_marcha.configure(text="Arrancar planta")
            return
        cfg = self._recoger()
        for p in cfg.comprobar():
            self.log(f"AVISO: {p}")
        esclavos = banco_demo(cfg) if (self.demo or cfg.puerto == "simulado") else None
        try:
            self.motor = MotorPlanta(cfg, log=self.log,
                                     esclavos_simulados=esclavos).arrancar()
        except Exception as e:
            self.motor = None
            self.log(f"No se pudo arrancar: {e}")
            self.lbl_estado.configure(text=f"Error: {e}", foreground=UI["fallo"])
            return
        self.btn_marcha.configure(text="Parar planta")

    def _cerrar_todo(self):
        if self.motor is not None:
            self.motor.cerrar_todo()
            for f in self.filas:
                if f["modo"].get() == ETIQUETA_MODO["manual"]:
                    pass
        else:
            try:
                bus = self._bus_puntual()
                bus.actuador.cerrar_todo(bus.maestro_act, len(self.cfg.zonas))
                bus.cerrar()
                self.log("Cierre general enviado (planta parada).")
            except Exception as e:
                self.log(f"No se pudo cerrar: {e}")

    def _leer_ahora(self):
        if self.motor is not None:
            self.motor.leer_ahora()

    def _cambio_modo(self, zona):
        f = self.filas[zona]
        inverso = {v: k for k, v in ETIQUETA_MODO.items()}
        modo = inverso.get(f["modo"].get(), "auto")
        f["z"].modo = modo
        if self.motor is not None:
            self.motor.set_modo(zona, modo)

    def _cambio_banda(self, zona):
        f = self.filas[zona]
        try:
            lo, hi = float(f["lo"].get()), float(f["hi"].get())
        except (ValueError, tk.TclError):
            return
        if hi <= lo:
            hi = lo + 2.0
            f["hi"].set(round(hi, 1))
        f["z"].banda_baja, f["z"].banda_alta = lo, hi

    def _toggle_valvula(self, zona):
        f = self.filas[zona]
        if f["z"].modo != "manual":
            f["modo"].set(ETIQUETA_MODO["manual"])
            self._cambio_modo(zona)
        if self.motor is not None:
            c = self.motor.controles[zona]
            self.motor.set_manual(zona, not c.manual)

    # ---- refresco ------------------------------------------------------------
    def _refrescar(self):
        try:
            snap = self.motor.instantanea() if self.motor else None
            self._pintar(snap)
            self._pintar_tendencia()
            self._pintar_filas(snap)
            self._cabecera(snap)
        except Exception as e:                      # nunca dejar la ventana muerta
            self.log(f"Error dibujando: {e}")
        self.root.after(self.REFRESCO_MS, self._refrescar)

    def _cabecera(self, snap):
        if not snap:
            self.lbl_estado.configure(text="Detenida", foreground=UI["tenue"])
            return
        edad = time.time() - snap["ultima_ronda"] if snap["ultima_ronda"] else None
        falta = max(0.0, snap["proxima_ronda"] - time.time())
        fallos = sum(1 for z in snap["zonas"] if z["fallo"])
        abiertas = sum(1 for z in snap["zonas"] if z["abierta"])
        txt = (f"En marcha | rondas {snap['rondas']} | "
               f"lectura de hace {edad:.0f} s, siguiente en {falta:.0f} s | "
               f"ciclo {snap['duracion_ronda'] * 1000:.0f} ms | "
               f"{abiertas} valvula(s) abiertas")
        if fallos:
            txt += f" | {fallos} ZONA(S) EN FALLO"
        if snap["error_bus"]:
            txt += " | valvulas sin confirmar"
        self.lbl_estado.configure(
            text=txt, foreground=UI["fallo"] if (fallos or snap["error_bus"])
            else UI["ok"])

    def _pintar_filas(self, snap):
        for i, f in enumerate(self.filas):
            z = snap["zonas"][i] if snap and i < len(snap["zonas"]) else None
            if z is None:
                f["btn"].configure(text="—", bg=UI["panel2"], fg=UI["tenue"])
                f["motivo"].configure(text="planta detenida")
                continue
            f["btn"].configure(
                text="ABIERTA" if z["abierta"] else "CERRADA",
                bg=UI["agua_osc"] if z["abierta"] else UI["panel2"],
                fg=UI["agua"] if z["abierta"] else UI["tenue"])
            lec = z["lectura"]
            if lec:
                col = {"seca": UI["aviso"], "en_banda": UI["ok"],
                       "humeda": UI["agua"], "fallo": UI["fallo"],
                       "sin_datos": UI["tenue"]}[z["estado"]]
                f["hum"].configure(text=f"{lec['vwc']:.1f} %", foreground=col)
                f["temp"].configure(text=f"{lec['temperatura']:.1f} C",
                                    foreground=UI["temp"])
            else:
                f["hum"].configure(text="sin dato", foreground=UI["fallo"])
                f["temp"].configure(text="-")
            f["motivo"].configure(
                text=(f"FALLO: {z['fallo']}" if z["fallo"] else z["motivo"]),
                foreground=UI["fallo"] if z["fallo"] else UI["tenue"])

    # ---- sinoptico -----------------------------------------------------------
    def _pintar(self, snap):
        c = self.lienzo
        c.delete("all")
        W = max(400, c.winfo_width())
        H = max(220, c.winfo_height())
        zonas = snap["zonas"] if snap else [
            {"id": z.id, "nombre": z.nombre, "cfg": z, "lectura": None,
             "abierta": False, "modo": z.modo, "fallo": "", "motivo": "detenida",
             "estado": "sin_datos", "descanso": 0.0, "riegos": 0,
             "minutos_riego": 0.0} for z in self.cfg.zonas]
        n = max(1, len(zonas))
        margen, hueco = 16, 12
        ancho = (W - 2 * margen - hueco * (n - 1)) / n
        alto = H - 2 * margen
        for i, z in enumerate(zonas):
            x0 = margen + i * (ancho + hueco)
            self._tarjeta(c, x0, margen, ancho, alto, z)

    def _tarjeta(self, c, x, y, w, h, z):
        cfg = z["cfg"]
        col = COLOR_ZONA[z["id"] % len(COLOR_ZONA)]
        borde = {"fallo": UI["fallo"], "seca": UI["aviso"], "en_banda": UI["ok"],
                 "humeda": UI["agua"], "sin_datos": UI["borde2"]}[z["estado"]]
        c.create_rectangle(x, y, x + w, y + h, fill=UI["panel2"], outline=borde,
                           width=2)
        c.create_rectangle(x, y, x + w, y + 24, fill=col, outline="")
        c.create_text(x + 10, y + 12, anchor="w", fill="#0b0f14",
                      font=("TkDefaultFont", 10, "bold"), text=z["nombre"])
        c.create_text(x + w - 10, y + 12, anchor="e", fill="#0b0f14",
                      font=("TkFixedFont", 8),
                      text=ETIQUETA_MODO.get(z["modo"], z["modo"]))

        # --- columna de humedad ---
        gx0, gx1 = x + 14, x + 54
        gy0, gy1 = y + 44, y + h - 62
        c.create_rectangle(gx0, gy0, gx1, gy1, fill=UI["panel"], outline=UI["borde"])

        def ay(v):
            return gy1 - (gy1 - gy0) * max(0.0, min(1.0, v / self.ESCALA_MAX))
        c.create_rectangle(gx0, ay(cfg.banda_alta), gx1, ay(cfg.banda_baja),
                           fill="#14532d", outline=UI["ok"])
        lec = z["lectura"]
        if lec:
            v = lec["vwc"]
            c.create_rectangle(gx0 + 1, ay(v), gx1 - 1, gy1 - 1,
                               fill=color_humedad(v, cfg.banda_baja, cfg.banda_alta),
                               outline="")
            c.create_line(gx0 - 4, ay(v), gx1 + 4, ay(v), fill=UI["texto"], width=2)
        for marca in (0, self.ESCALA_MAX / 2, self.ESCALA_MAX):
            c.create_text(gx0 - 4, ay(marca), anchor="e", fill=UI["tenue"],
                          font=("TkFixedFont", 7), text=f"{marca:.0f}")

        # --- cifras ---
        tx = gx1 + 16
        if lec:
            c.create_text(tx, y + 56, anchor="w", fill=UI["texto"],
                          font=("TkDefaultFont", 20, "bold"),
                          text=f"{lec['vwc']:.1f} %")
            c.create_text(tx, y + 80, anchor="w", fill=UI["temp"],
                          font=("TkFixedFont", 10),
                          text=f"{lec['temperatura']:.1f} C")
            extra = f"banda {cfg.banda_baja:.0f}-{cfg.banda_alta:.0f} %"
            if cfg.vwc_capacidad_campo > 1:
                extra += (f"   ({lec['vwc'] / cfg.vwc_capacidad_campo:.2f} de cap. "
                          f"de campo)")
            c.create_text(tx, y + 98, anchor="w", fill=UI["tenue"],
                          font=("TkFixedFont", 8), text=extra)
            if lec.get("conductividad") and lec.get("ec_fiable"):
                c.create_text(tx, y + 114, anchor="w", fill=UI["tenue"],
                              font=("TkFixedFont", 8),
                              text=f"EC {lec['conductividad']:.0f} uS/cm   "
                                   f"pH {lec['ph']:.1f}")
        else:
            c.create_text(tx, y + 60, anchor="w", fill=UI["fallo"],
                          font=("TkDefaultFont", 13, "bold"), text="SIN DATO")

        # --- aspersor y valvula ---
        ax = x + w - 42
        ay0 = y + 44
        c.create_line(ax, ay0, ax, ay0 + 22, fill=UI["borde2"], width=3)
        c.create_oval(ax - 8, ay0 - 8, ax + 8, ay0 + 8,
                      fill=UI["agua"] if z["abierta"] else UI["panel"],
                      outline=UI["borde2"], width=2)
        if z["abierta"]:
            for k in range(-2, 3):
                c.create_line(ax, ay0 + 20, ax + k * 11, ay0 + 52,
                              fill=UI["agua"], dash=(2, 5))
            c.create_text(ax, ay0 + 66, fill=UI["agua"],
                          font=("TkFixedFont", 8, "bold"), text="RIEGO")
        elif z["descanso"] > 0:
            c.create_text(ax, ay0 + 40, fill=UI["aviso"], font=("TkFixedFont", 8),
                          text=f"descanso\n{z['descanso'] / 60.0:.0f} min")

        # --- pie ---
        pie = (f"FALLO: {z['fallo']}" if z["fallo"] else z["motivo"])
        c.create_text(x + 10, y + h - 34, anchor="w",
                      fill=UI["fallo"] if z["fallo"] else UI["tenue"],
                      font=("TkFixedFont", 8), text=pie[:60])
        c.create_text(x + 10, y + h - 16, anchor="w", fill=UI["tenue"],
                      font=("TkFixedFont", 8),
                      text=f"riegos: {z['riegos']}   acumulado: "
                           f"{z['minutos_riego']:.1f} min")

    # ---- tendencia -----------------------------------------------------------
    def _pintar_tendencia(self):
        c = self.lienzo_tend
        c.delete("all")
        W = max(320, c.winfo_width())
        H = max(90, c.winfo_height())
        if self.motor is None:
            c.create_text(W / 2, H / 2, fill=UI["tenue"],
                          text="La planta esta detenida.")
            return
        series = {z.id: self.motor.serie(z.id) for z in self.cfg.zonas}
        if not any(len(s) > 1 for s in series.values()):
            c.create_text(W / 2, H / 2, fill=UI["tenue"], text="Tomando datos...")
            return
        x0, x1, y0, y1 = 40, W - 12, 8, H - 20
        c.create_rectangle(x0, y0, x1, y1, outline=UI["borde"], fill=UI["panel2"])
        for k in range(4):
            v = self.ESCALA_MAX * k / 3
            yy = y1 - (y1 - y0) * v / self.ESCALA_MAX
            c.create_line(x0, yy, x1, yy, fill=UI["borde"])
            c.create_text(x0 - 6, yy, anchor="e", fill=UI["tenue"],
                          font=("TkFixedFont", 7), text=f"{v:.0f}")
        t_max = max((s[-1][0] for s in series.values() if s), default=time.time())
        t_min = min((s[0][0] for s in series.values() if s), default=t_max - 1)
        span = max(1.0, t_max - t_min)
        for i, z in enumerate(self.cfg.zonas):
            s = series[z.id]
            pts = []
            for t, v, _ in s:
                if v is None:
                    continue
                pts += [x0 + (x1 - x0) * (t - t_min) / span,
                        y1 - (y1 - y0) * min(1.0, v / self.ESCALA_MAX)]
            if len(pts) >= 4:
                c.create_line(*pts, fill=COLOR_ZONA[i % len(COLOR_ZONA)], width=2)
            c.create_text(x0 + 8 + i * 70, y0 + 10, anchor="w",
                          fill=COLOR_ZONA[i % len(COLOR_ZONA)],
                          font=("TkFixedFont", 8, "bold"), text=z.nombre[:9])

    def _cerrar(self):
        if self.motor is not None:
            self.motor.parar()
            self.motor = None
        self.root.destroy()


# =============================================================================
# AUTOPRUEBAS DEL CONTROL
# =============================================================================

def selftest():
    fallos = []

    def check(c, m):
        if not c:
            fallos.append(m)

    def lec(vwc, sin_contacto=False):
        return {"vwc": vwc, "temperatura": 22.0, "conductividad": 500.0,
                "ph": 6.8, "sin_contacto": sin_contacto, "valido": True,
                "avisos": [], "ec_fiable": True}

    # --- histeresis basica ---
    z = ZonaPlanta(banda_baja=25.0, banda_alta=32.0, descanso_min=0.0)
    c = ControlZona(z)
    check(c.decidir(lec(20.0)) is True, "por debajo de la banda debe abrir")
    check(c.decidir(lec(28.0)) is True, "dentro de la banda, si ya riega, sigue")
    check(c.decidir(lec(32.5)) is False, "al llegar a la banda alta debe cerrar")
    check(c.decidir(lec(28.0)) is False, "dentro de la banda, cerrada, sigue cerrada")
    check(c.decidir(lec(24.0)) is True, "vuelve a abrir al bajar de la banda")

    # --- descanso minimo entre riegos ---
    z2 = ZonaPlanta(banda_baja=25.0, banda_alta=32.0, descanso_min=10.0)
    c2 = ControlZona(z2)
    c2.decidir(lec(20.0))
    c2.decidir(lec(33.0))                      # cierra y arranca el descanso
    check(c2.decidir(lec(20.0)) is False,
          "durante el descanso no debe volver a regar aunque este seca")
    check("descanso" in c2.motivo, f"el motivo debe explicarlo: {c2.motivo}")
    c2.fin_riego = time.time() - 11 * 60
    check(c2.decidir(lec(20.0)) is True, "pasado el descanso debe regar")

    # --- tope de riego continuo ---
    z3 = ZonaPlanta(banda_baja=25.0, banda_alta=32.0, max_riego_min=5.0,
                    descanso_min=0.0)
    c3 = ControlZona(z3)
    c3.decidir(lec(10.0))
    c3.desde = time.time() - 6 * 60            # lleva 6 min regando
    check(c3.decidir(lec(11.0)) is False,
          "debe cortar al superar el tope de riego continuo")
    check("tope" in c3.motivo and c3.fallo, "debe quedar marcado como fallo")

    # --- sensor mudo: cierra tras N fallos, no antes ---
    z4 = ZonaPlanta(banda_baja=25.0, banda_alta=32.0, descanso_min=0.0)
    c4 = ControlZona(z4, fallos_para_cerrar=3)
    c4.decidir(lec(20.0))
    check(c4.abierta, "arranca regando")
    check(c4.decidir(None) is True, "un fallo suelto no debe cortar el riego")
    c4.decidir(None)
    check(c4.decidir(None) is False, "tres fallos seguidos deben cerrar")
    check(c4.fallo, "y dejar la zona en fallo")
    check(c4.decidir(lec(20.0)) is True, "al volver la lectura debe reanudar")
    check(not c4.fallo, "y limpiar el fallo")

    # --- sonda sin contacto: nunca se riega a ciegas ---
    c5 = ControlZona(ZonaPlanta(banda_baja=25.0, banda_alta=32.0), fallos_para_cerrar=2)
    c5.decidir(lec(0.0, sin_contacto=True))
    check(c5.decidir(lec(0.0, sin_contacto=True)) is False,
          "con la sonda sin contacto no debe abrir aunque marque 0 %")
    check("contacto" in c5.fallo, f"y decir por que: {c5.fallo}")

    # --- modos ---
    z6 = ZonaPlanta(banda_baja=25.0, banda_alta=32.0, modo="apagado")
    c6 = ControlZona(z6)
    check(c6.decidir(lec(5.0)) is False, "en apagado nunca riega")
    z6.modo = "manual"
    c6.manual = True
    check(c6.decidir(lec(5.0)) is True, "en manual obedece al operador")
    check(c6.decidir(lec(40.0)) is False,
          "pero en manual tambien corta si se pasa de la banda alta")
    z6.modo = "externo"
    c6._cambiar(True, "x")
    check(c6.decidir(lec(40.0)) is True,
          "en modo externo el control local no toca la valvula")

    # --- configuracion ---
    cfg = ConfigPlanta.por_defecto(3)
    check(cfg.comprobar() == [], f"la config por defecto debe ser valida: "
                                 f"{cfg.comprobar()}")
    cfg.zonas[1].sensor_dir = 1
    check(any("repetida" in p for p in cfg.comprobar()),
          "debe detectar direcciones repetidas")
    cfg2 = ConfigPlanta.por_defecto(3)
    cfg2.zonas[1].valvula = 0
    check(any("valvula" in p for p in cfg2.comprobar()),
          "debe detectar dos zonas con la misma valvula")
    cfg3 = ConfigPlanta.por_defecto(2)
    cfg3.zonas[0].banda_alta = 25.5
    check(any("banda" in p for p in cfg3.comprobar()),
          "debe avisar de una banda demasiado estrecha")
    ruta = "planta_prueba.json"
    cfg.guardar(ruta)
    otra = ConfigPlanta.cargar(ruta)
    check(otra and len(otra.zonas) == 3 and otra.zonas[2].valvula == 2,
          "la configuracion debe sobrevivir a guardar y cargar")
    os.remove(ruta)

    # --- motor completo contra el banco de mentira ---
    cfg4 = ConfigPlanta.por_defecto(3)
    cfg4.puerto = "simulado"
    cfg4.tiempo_muestreo = 0.5
    cfg4.pausa_entre_sensores = 0.0
    for z in cfg4.zonas:
        z.descanso_min = 0.0
        z.banda_baja, z.banda_alta = 25.0, 30.0
    motor = MotorPlanta(cfg4, esclavos_simulados=banco_demo(cfg4)).arrancar()
    time.sleep(2.5)
    snap = motor.instantanea()
    check(snap["rondas"] >= 3,
          f"el motor debe encadenar rondas: solo hizo {snap['rondas']} en 2,5 s "
          f"(ultima ronda {snap['duracion_ronda']*1000:.0f} ms)")
    check(MotorPlanta.TS_MINIMO >= 1.0,
          "debe existir un suelo de muestreo: sondear el suelo mas rapido de 1 s "
          "no aporta nada y castiga el bus")
    check(all(z["lectura"] for z in snap["zonas"]),
          "las tres zonas deben tener lectura")
    seca = [z for z in snap["zonas"] if z["lectura"]["vwc"] < 25.0]
    check(all(z["abierta"] for z in seca),
          "las zonas por debajo de la banda deben estar regando")
    motor.set_modo(0, "apagado")
    time.sleep(0.6)
    check(not motor.instantanea()["zonas"][0]["abierta"],
          "al apagar una zona su valvula debe cerrarse")
    motor.parar()
    check(not any(c.abierta for c in motor.controles.values()),
          "al parar la planta todas las valvulas quedan cerradas")

    if fallos:
        print("PLANTA - FALLOS:")
        for f in fallos:
            print("  -", f)
        return 1
    print("PLANTA - autopruebas correctas.")
    return 0


# =============================================================================
# MODO CONSOLA (operacion sin interfaz)
# =============================================================================

def consola(puerto, dirs, ts, baudios, demo=False):
    cfg = ConfigPlanta.por_defecto(len(dirs))
    cfg.puerto, cfg.baudios, cfg.tiempo_muestreo = puerto, baudios, ts
    for z, d in zip(cfg.zonas, dirs):
        z.sensor_dir = d
    esclavos = banco_demo(cfg) if (demo or puerto == "simulado") else None
    motor = MotorPlanta(cfg, log=lambda m: print(f"  {m}"),
                        esclavos_simulados=esclavos)
    motor.arrancar()
    print(f"Planta en marcha en {puerto}, sensores {dirs}, muestreo {ts:g} s. "
          f"Ctrl+C para parar.\n")
    try:
        while True:
            time.sleep(max(1.0, ts))
            s = motor.instantanea()
            print(f"--- {time.strftime('%H:%M:%S')} (ronda {s['rondas']}) ---")
            for z in s["zonas"]:
                l = z["lectura"]
                estado = "ABIERTA" if z["abierta"] else "cerrada"
                if l:
                    print(f"  {z['nombre']:<10} {l['vwc']:5.1f} %  "
                          f"{l['temperatura']:5.1f} C  valvula {estado:<8} "
                          f"{z['fallo'] or z['motivo']}")
                else:
                    print(f"  {z['nombre']:<10}  sin dato   valvula {estado:<8} "
                          f"{z['fallo'] or z['motivo']}")
    except KeyboardInterrupt:
        print("\nParando...")
    finally:
        motor.parar()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Aplicacion de operacion de la cama de vermicompostaje.")
    ap.add_argument("--test", action="store_true", help="autopruebas del control")
    ap.add_argument("--demo", action="store_true",
                    help="interfaz con un lecho de mentira (sin hardware)")
    ap.add_argument("--consola", metavar="PUERTO", help="operar sin interfaz")
    ap.add_argument("--dirs", default="1,2,3", help="direcciones de los sensores")
    ap.add_argument("--muestreo", type=float, default=30.0)
    ap.add_argument("--baudios", type=int, default=4800)
    ap.add_argument("--config", default=RUTA)
    args = ap.parse_args(argv)

    if args.test:
        return selftest()
    if args.consola:
        dirs = [int(x) for x in args.dirs.split(",") if x.strip()]
        return consola(args.consola, dirs, args.muestreo, args.baudios, args.demo)

    cfg = ConfigPlanta.cargar(args.config)
    if cfg is None:
        dirs = [int(x) for x in args.dirs.split(",") if x.strip()]
        cfg = ConfigPlanta.por_defecto(len(dirs))
        for z, d in zip(cfg.zonas, dirs):
            z.sensor_dir = d
        if args.demo:
            cfg.puerto = "simulado"
    if not _HAY_TK:
        print("Tkinter no esta disponible. Usa --consola PUERTO, o instala "
              "python3-tk.")
        return 1
    root = tk.Tk()
    AppPlanta(root, cfg, demo=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
