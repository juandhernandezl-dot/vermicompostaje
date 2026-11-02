#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rl_common_mz.py  -  INFRAESTRUCTURA RL PARA LA CAMA MULTIZONA
=============================================================

Lo que comparten los tres algoritmos cuando cada ZONA tiene su PROPIO
controlador independiente:

  * ClienteMZ        : cliente TCP/JSON del servicio, con comandos por zona.
  * ObservacionZona  : construye el vector de ENTRADAS del control (ampliado).
  * DiscretizadorZona: (humedad, temperatura, tendencia, carga de arriba) -> celda.
  * recompensa()     : banda objetivo + castigo asimetrico + coordinacion en
                       cascada (castiga inundar la zona de abajo) + coste de agua.
  * PublicadorRL     : vuelca el estado interno del agente a disco para que la
                       ventana de monitorizacion lo vea EN VIVO.
  * RunnerZona       : ciclo de vida (pre-entrenamiento acelerado en un modelo
                       local + aprendizaje/control en tiempo real sobre la GUI).

ENTRADAS DEL CONTROL (mejoradas respecto de la version de una zona)
-------------------------------------------------------------------
Antes el agente veia solo (humedad, temperatura). Ahora ve ademas:

  tendencia        d(humedad)/dt entre muestras -> distingue "0.65 subiendo"
                   de "0.65 bajando", que exigen acciones opuestas.
  carga_arriba     escorrentia que ENTRA desde la zona superior, normalizada
                   por el caudal de un aspersor. Es la variable que hace
                   observable la cascada: la zona 2 puede anticipar el agua
                   que le llega porque la zona 1 esta regando.
  humedad_arriba   estado hidrico de la zona superior (perturbacion futura).
  carga_abajo      escorrentia que la zona ENVIA hacia abajo (para no inundar).
  nivel_previo     accion anterior -> permite penalizar conmutaciones.
  posicion         posicion normalizada en la cama (0 = cabecera, 1 = pie).

Los metodos tabulares discretizan (humedad, temperatura, tendencia, carga
arriba); REINFORCE usa el vector continuo completo, sin discretizar.

INTERFAZ QUE DEBE CUMPLIR UN AGENTE
-----------------------------------
    act(obs, explore) -> int (indice de accion)
    observe(o, a, r, o2, a2, done)
    end_episode() / on_reset()
    save(path) / load(path) -> bool
    snapshot() -> dict   (para la ventana de monitorizacion)
    nombre : str ; acciones : lista de niveles [0.0, ..., 1.0]

Solo libreria estandar.
"""

import json
import math
import os
import random
import socket
import time
from collections import deque
from dataclasses import dataclass, field, asdict

from vermicompost_mz import BedParams, MultiZoneBed, HisteresisZona

HOST = "127.0.0.1"
PORT = 8765

TARGET_LOW = 0.60
TARGET_HIGH = 0.70

DIR_ESTADO = "estado_rl"     # instantaneas para el monitor
DIR_TABLAS = "tablas_rl"     # tablas / politicas persistentes


# =============================================================================
# CLIENTE DEL SERVICIO (con soporte de zona)
# =============================================================================

class ClienteMZ:
    """Cliente TCP/JSON del servicio local. Todos los comandos aceptan zona."""

    def __init__(self, host=HOST, port=PORT, timeout=10.0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self._buf = b""

    def _send(self, msg):
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            while b"\n" not in self._buf:
                data = self.sock.recv(8192)
                if not data:
                    raise ConnectionError("El simulador cerro la conexion.")
                self._buf += data
        except socket.timeout as e:
            raise ConnectionError(
                f"El simulador no respondio a tiempo al comando "
                f"{msg.get('cmd')!r}. Comprueba que la ventana del simulador "
                f"sigue abierta y no esta bloqueada.") from e
        line, self._buf = self._buf.split(b"\n", 1)
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok", False):
            raise RuntimeError(f"El servidor respondio error: {resp.get('error')}")
        resp.pop("ok", None)
        return resp

    def estado(self, zone=None):
        m = {"cmd": "get"}
        if zone is not None:
            m["zone"] = zone
        return self._send(m)

    def zonas(self):
        return self._send({"cmd": "zones"})

    def params(self):
        return self._send({"cmd": "params"})["params"]

    def cadencia(self):
        """Cadencia y estabilidad REALES del simulador (dt, Ts efectivo, pasos
        por decision). Es de donde se deduce el ritmo del control."""
        return self._send({"cmd": "cadencia"})

    def set_param(self, key, value):
        return self._send({"cmd": "set_param", "key": key, "value": value})

    def set_nivel(self, zone, level):
        return self._send({"cmd": "set_valve", "zone": int(zone), "level": float(level)})

    def step(self, n=1, levels=None):
        m = {"cmd": "step", "n": int(n)}
        if levels is not None:
            m["levels"] = list(levels)
        return self._send(m)

    def reset(self):
        return self._send({"cmd": "reset"})

    def muestra(self, zone=None, since=None):
        m = {"cmd": "sample"}
        if zone is not None:
            m["zone"] = int(zone)
        if since is not None:
            m["since"] = int(since)
        return self._send(m)

    def reclamar(self, zone, mode="externo"):
        return self._send({"cmd": "claim", "zone": int(zone), "mode": mode})

    def liberar(self, zone, mode="manual"):
        return self._send({"cmd": "release", "zone": int(zone), "mode": mode})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def conectar_o_avisar(host=HOST, port=PORT, silencioso=False):
    try:
        return ClienteMZ(host, port)
    except (ConnectionRefusedError, OSError) as e:
        if not silencioso:
            print(f"No se pudo conectar a {host}:{port} -> {e}")
            print("Abre antes la interfaz:  python3 gui_mz.py     (o el servicio: "
                  "python3 vermicompost_mz.py --serve)")
        return None


# =============================================================================
# OBSERVACION: las ENTRADAS que ve el controlador
# =============================================================================

CARACTERISTICAS = [
    "sesgo", "error_banda", "error_banda^2", "deficit", "exceso",
    "error_arriba", "error_abajo", "tendencia", "carga_arriba", "carga_abajo",
    "nivel_previo", "posicion", "gradiente_interno",
]


def preparar_obs(zs, low, high, nivel_previo=0.0, pasos_desde_cambio=0):
    """Enriquece el estado bruto de la zona con las entradas derivadas."""
    o = dict(zs)
    # Los flujos laterales se normalizan por la escala de evaporacion de la
    # zona, que es la magnitud con la que compiten en el balance de agua: un
    # "aporte de 1" significa que desde arriba llega tanta agua como la que se
    # evapora. Normalizar por el caudal del aspersor los dejaria en valores
    # diminutos e indistinguibles para el control.
    ref = max(1e-12, float(zs.get("flux_ref", 6e-5)))
    o["band_low"], o["band_high"] = low, high
    o["nivel_previo"] = float(nivel_previo)
    o["pasos_desde_cambio"] = int(pasos_desde_cambio)
    o["carga_arriba"] = float(zs.get("flux_lateral_in", 0.0)) / ref
    o["carga_abajo"] = float(zs.get("flux_lateral_out", 0.0)) / ref
    o.setdefault("moisture_grad", 0.0)
    o.setdefault("homogeneidad", 1.0)
    return o


def features(o, trend_scale=2.0e-4):
    """Vector continuo phi(o) para REINFORCE, EXPRESADO RESPECTO A LA BANDA.

    Antes las caracteristicas eran humedades crudas (0..1,4) y los terminos que
    de verdad deciden -deficit y exceso- valian 0,01-0,1: con un unico paso de
    aprendizaje, los pesos utiles se movian diez veces mas despacio que los
    irrelevantes y la politica tardaba una eternidad en formarse (o no se
    formaba). Ahora todo se mide en SEMIANCHOS DE BANDA:

        e = (humedad - centro) / (semiancho)

    e = -1 es el borde inferior, +1 el superior. Todas las entradas quedan del
    mismo orden de magnitud y el gradiente esta bien condicionado.

    No incluye la temperatura: el riego no se decide por temperatura, y su
    efecto (mas calor -> mas evaporacion) llega a traves de la TENDENCIA.
    """
    low = float(o.get("band_low", TARGET_LOW))
    high = float(o.get("band_high", TARGET_HIGH))
    mid = 0.5 * (low + high)
    semi = max(1e-6, 0.5 * (high - low))

    def err(v):
        return max(-4.0, min(4.0, (float(v) - mid) / semi))

    e = err(o.get("moisture", 0.0))
    return [
        1.0,
        e,
        min(4.0, e * e) * (1.0 if e >= 0 else -1.0),   # curvatura con signo
        max(0.0, -1.0 - e),          # cuanto falta para entrar por abajo
        max(0.0, e - 1.0),           # cuanto sobra por arriba
        err(o.get("moisture_up", 0.0)),
        err(o.get("moisture_down", 0.0)),
        max(-3.0, min(3.0, float(o.get("trend", 0.0)) / trend_scale)),
        min(6.0, float(o.get("carga_arriba", 0.0))) / 2.0,
        min(6.0, float(o.get("carga_abajo", 0.0))) / 2.0,
        float(o.get("nivel_previo", 0.0)),
        float(o.get("pos_norm", 0.0)),
        max(-1.0, min(1.0, float(o.get("moisture_grad", 0.0)) / semi)),
    ]


NF = len(CARACTERISTICAS)


# =============================================================================
# DISCRETIZACION (metodos tabulares)
# =============================================================================

class EjeHumedad:
    """
    Eje de humedad ALINEADO CON LA BANDA OBJETIVO.

    Con bins uniformes, el que contiene el borde de la banda mezcla estados
    dentro y fuera: a 24 bins sobre [0, 1.4] el bin 10 abarca [0.583, 0.641],
    o sea 0.59 (seco) y 0.62 (correcto) son la MISMA celda para el agente, que
    no puede aprender acciones distintas para ellos. Ese solo detalle bastaba
    para que la politica se quedase a 0.59 sin regar mientras creia estar bien.

    Aqui los cortes caen EXACTAMENTE en 'low' y 'high', y la resolucion se
    concentra donde se decide: dentro de la banda y en su entorno.
    """

    def __init__(self, low=0.60, high=0.70, maximo=1.4, n_bajo=6, n_dentro=6, n_alto=8):
        self.low, self.high, self.maximo = low, high, max(maximo, high * 1.05)
        self.n_bajo, self.n_dentro, self.n_alto = n_bajo, n_dentro, n_alto
        self.n = n_bajo + n_dentro + n_alto
        self.cortes = self._cortes()

    def _cortes(self):
        c = [self.low * k / self.n_bajo for k in range(self.n_bajo)]
        c += [self.low + (self.high - self.low) * k / self.n_dentro
              for k in range(self.n_dentro)]
        c += [self.high + (self.maximo - self.high) * k / self.n_alto
              for k in range(self.n_alto)]
        return c + [self.maximo]

    def bin(self, m):
        if m < self.low:
            k = int(m / max(1e-9, self.low) * self.n_bajo)
            return max(0, min(self.n_bajo - 1, k))
        if m <= self.high:
            k = int((m - self.low) / max(1e-9, self.high - self.low) * self.n_dentro)
            return self.n_bajo + max(0, min(self.n_dentro - 1, k))
        k = int((m - self.high) / max(1e-9, self.maximo - self.high) * self.n_alto)
        return self.n_bajo + self.n_dentro + max(0, min(self.n_alto - 1, k))

    def centro(self, b):
        b = max(0, min(self.n - 1, int(b)))
        return 0.5 * (self.cortes[b] + self.cortes[b + 1])

    def firma(self):
        return (f"{self.n_bajo}/{self.n_dentro}/{self.n_alto}"
                f"@[{self.low:g},{self.high:g},{self.maximo:g}]")

    def descriptor(self, nombre):
        return {"nombre": nombre, "min": 0.0, "max": self.maximo, "bins": self.n,
                "cortes": [round(c, 4) for c in self.cortes],
                "banda": [self.low, self.high]}


class DiscretizadorZona:
    """
    Celda = (humedad propia, humedad de la zona de ARRIBA, tendencia,
             humedad de la zona de ABAJO).

    La temperatura ya NO forma parte del estado: no se riega por temperatura, y
    su efecto sobre la evaporacion llega al control a traves de la tendencia.
    En su lugar el agente ve a sus VECINAS, que es lo que condiciona de verdad a
    una zona en una cama inclinada: la de arriba anuncia el agua que va a
    llegarle y la de abajo dice si puede permitirse soltar mas.

    Los dos ejes de humedad estan alineados con la banda objetivo (ver
    EjeHumedad). La zona 0 no tiene vecina arriba (humedad_arriba = 0) y la
    ultima no tiene vecina abajo: cada una usa de forma natural la primera
    columna o la primera rebanada.
    """

    ETIQ_TEND = ["baja", "estable", "sube"]
    ETIQ_ABAJO = ["seca", "en banda", "humeda"]

    def __init__(self, low=TARGET_LOW, high=TARGET_HIGH, moist_max=1.4,
                 moist_bins=20, up_bins=6, umbral_tendencia=1.0e-5, **_compat):
        # 'moist_bins' se reparte 30/30/40 entre por debajo, dentro y por encima
        # de la banda; 'up_bins' hace lo mismo para la vecina de arriba.
        self.low, self.high = low, high
        nb = max(2, int(round(moist_bins * 0.3)))
        nd = max(2, int(round(moist_bins * 0.3)))
        na = max(2, moist_bins - nb - nd)
        self.eje = EjeHumedad(low, high, moist_max, nb, nd, na)
        ub = max(1, int(round(up_bins * 0.35)))
        ud = max(1, int(round(up_bins * 0.3)))
        ua = max(1, up_bins - ub - ud)
        self.eje_arriba = EjeHumedad(low, high, moist_max, ub, ud, ua)
        self.umbral_tendencia = umbral_tendencia

    # --- compatibilidad con el codigo que consultaba estos atributos ---
    @property
    def moist_bins(self):
        return self.eje.n

    @property
    def up_bins(self):
        return self.eje_arriba.n

    @property
    def moist_min(self):
        return 0.0

    @property
    def moist_max(self):
        return self.eje.maximo

    def bin_humedad(self, m):
        return self.eje.bin(m)

    def bin_arriba(self, m):
        return self.eje_arriba.bin(m)

    def bin_tendencia(self, tr):
        u = self.umbral_tendencia
        return 0 if tr < -u else (2 if tr > u else 1)

    def bin_abajo(self, m):
        return 0 if m < self.low else (1 if m <= self.high else 2)

    def key(self, o):
        return (self.bin_humedad(float(o.get("moisture", 0.0))),
                self.bin_arriba(float(o.get("moisture_up", 0.0))),
                self.bin_tendencia(float(o.get("trend", 0.0))),
                self.bin_abajo(float(o.get("moisture_down", 0.0))))

    def centro_humedad(self, mb):
        return self.eje.centro(mb)

    def centro_arriba(self, ub):
        return self.eje_arriba.centro(ub)

    def firma(self):
        return (f"hum{self.eje.firma()}|arriba{self.eje_arriba.firma()}"
                f"|tend3@{self.umbral_tendencia:g}|abajo3")

    def info(self):
        """Descriptor generico de los ejes para la ventana de monitorizacion."""
        return {
            "firma": self.firma(),
            "filas": self.eje.descriptor("humedad de la zona"),
            "columnas": self.eje_arriba.descriptor("humedad de la zona de ARRIBA"),
            "rebanadas": [
                {"nombre": "tendencia", "etiquetas": self.ETIQ_TEND},
                {"nombre": "zona de abajo", "etiquetas": self.ETIQ_ABAJO},
            ],
        }

    @property
    def n_celdas(self):
        return self.eje.n * self.eje_arriba.n * 9


# =============================================================================
# RECOMPENSA (con coordinacion en cascada)
# =============================================================================

@dataclass
class PesosRecompensa:
    peso_defecto: float = 5.0        # castigo cuadratico por quedarse corto
    peso_exceso: float = 25.0        # castigo cuadratico por pasarse
    peso_exceso_lin: float = 6.0     # castigo lineal por pasarse (duele desde el principio)
    penal_riesgo: float = 10.0       # extra al superar el limite de seguridad
    penal_riego_humedo: float = 5.0  # abrir estando ya por encima de la banda
    penal_conmutacion: float = 0.05  # cambiar el estado del actuador
    penal_escorrentia: float = 0.5   # NUEVO: inundar la zona de ABAJO
    escorrentia_libre: float = 1.5   # aporte hacia abajo que se considera normal
    coste_agua: float = 0.10         # NUEVO: agua gastada (eficiencia hidrica)
    margen_seguridad: float = 0.05   # limite duro por arriba = banda alta + margen
    margen_seco: float = 0.15        # limite duro por abajo = banda baja - margen


def limite_seguridad(high, margen=0.05):
    return high + margen


def recompensa(o2, nivel, nivel_previo, low, high, w: PesosRecompensa):
    """
    Recompensa de UNA zona:
      +1                              dentro de la banda
      -peso_defecto*(low-h)^2         por debajo (castigo suave)
      -peso_exceso*(h-high)^2 - lineal por encima (castigo fuerte y asimetrico)
      -penal_riesgo                   por encima del limite de seguridad
      -penal_riego_humedo             regar estando ya humedo
      -penal_conmutacion*|du|         mover el actuador
      -penal_escorrentia*carga_abajo  MANDAR agua a la zona de abajo  <-- cascada
      -coste_agua*nivel               agua consumida
    El termino de escorrentia es el que obliga a la zona alta a tener en cuenta
    a las de abajo: sin el, cada controlador optimizaria su tramo inundando al
    siguiente.
    """
    h = float(o2.get("moisture", 0.0))
    if low <= h <= high:
        r = 1.0
    elif h < low:
        r = -((low - h) ** 2) * w.peso_defecto
    else:
        over = h - high
        r = -(over * over * w.peso_exceso + over * w.peso_exceso_lin)
        if h >= limite_seguridad(high, w.margen_seguridad):
            r -= w.penal_riesgo
    if nivel > 0.0 and h >= high:
        r -= w.penal_riego_humedo * nivel
    r -= w.penal_conmutacion * abs(nivel - nivel_previo)
    # Solo se castiga el agua que se manda hacia abajo POR ENCIMA del goteo
    # natural del lecho: el trasvase normal de una cama inclinada no es un fallo.
    exceso_abajo = max(0.0, float(o2.get("carga_abajo", 0.0)) - w.escorrentia_libre)
    r -= w.penal_escorrentia * min(6.0, exceso_abajo)
    r -= w.coste_agua * nivel
    return r


# =============================================================================
# METRICAS EN VENTANA MOVIL
# =============================================================================

class Metricas:
    def __init__(self, ventana=60, low=TARGET_LOW, high=TARGET_HIGH, margen=0.05):
        self.banda = deque(maxlen=ventana)
        self.sat = deque(maxlen=ventana)
        self.rec = deque(maxlen=ventana)
        self.low, self.high, self.lim = low, high, limite_seguridad(high, margen)

    def push(self, o, r=None):
        m = float(o.get("moisture", 0.0))
        self.banda.append(1 if self.low <= m <= self.high else 0)
        self.sat.append(1 if m >= self.lim else 0)
        if r is not None:
            self.rec.append(r)

    @property
    def pct_banda(self):
        return 100.0 * sum(self.banda) / len(self.banda) if self.banda else float("nan")

    @property
    def pct_sat(self):
        return 100.0 * sum(self.sat) / len(self.sat) if self.sat else float("nan")

    @property
    def r_medio(self):
        return sum(self.rec) / len(self.rec) if self.rec else 0.0

    @property
    def n(self):
        return len(self.banda)


# =============================================================================
# PUBLICADOR PARA LA VENTANA DE MONITORIZACION
# =============================================================================

class PublicadorRL:
    """
    Vuelca periodicamente el estado interno del agente a
    'estado_rl/z<N>_<algoritmo>.json' con escritura atomica (fichero temporal +
    rename), de modo que la ventana de monitorizacion pueda leerlo mientras el
    controlador sigue aprendiendo, sin bloqueos ni datos a medias.
    """

    def __init__(self, zona, agente, carpeta=DIR_ESTADO, cada_s=0.5):
        self.zona, self.agente = zona, agente
        self.carpeta = carpeta
        self.cada_s = cada_s
        os.makedirs(carpeta, exist_ok=True)
        etiqueta = agente.nombre.lower().replace("(", "").replace(")", "").replace(" ", "_")
        self.path = os.path.join(carpeta, f"z{zona}_{etiqueta}.json")
        self._ultimo = 0.0

    def publicar(self, extra=None, forzar=False):
        ahora = time.time()
        if not forzar and (ahora - self._ultimo) < self.cada_s:
            return False
        self._ultimo = ahora
        datos = self.agente.snapshot()
        datos.update({"zona": self.zona, "actualizado": ahora})
        if extra:
            datos.update(extra)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(datos, f)
            os.replace(tmp, self.path)
        except OSError:
            return False
        return True

    def retirar(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


# =============================================================================
# CONFIGURACION DEL LAZO
# =============================================================================

@dataclass
class ConfigZona:
    zona: int = 0
    host: str = HOST
    port: int = PORT
    target_low: float = TARGET_LOW
    target_high: float = TARGET_HIGH
    banda_desde_gui: bool = True
    pesos: PesosRecompensa = field(default_factory=PesosRecompensa)
    # --- pre-entrenamiento acelerado (modelo local, no toca la GUI) ---
    episodios_min: int = 10
    episodios_max: int = 150
    pasos_por_episodio: int = 700
    evaluar_cada: int = 5
    pasos_evaluacion: int = 400
    objetivo_pct: float = 0.85
    paciencia: int = 2               # evaluaciones sin mejora antes de reabrir exploracion
    epsilon_reinicio: float = 0.35
    max_respaldo: float = 0.20   # fraccion maxima de decisiones delegadas a la histeresis
    aleatorizar_inicio: bool = True   # variar humedad/temperatura inicial por episodio
    vecinos_offline: str = "histeresis"  # 'histeresis' | 'aleatorio' | 'cerrado'
    # --- tiempo real ---
    poll_s: float = 0.02
    max_espera_s: float = 30.0
    mini_episodio: int = 64
    guardar_cada: int = 40
    ventana_metrica: int = 60
    imprimir_cada: int = 5
    publicar_cada_s: float = 0.5

    @property
    def limite(self):
        return limite_seguridad(self.target_high, self.pesos.margen_seguridad)

    @property
    def limite_seco(self):
        return max(0.0, self.target_low - self.pesos.margen_seco)


# =============================================================================
# RUNNER DE UNA ZONA
# =============================================================================

class RunnerZona:
    """
    Ciclo de vida del controlador de UNA zona.

      FASE 1 (opcional) PRE-ENTRENAMIENTO ACELERADO
        Se hace sobre una COPIA LOCAL del modelo multizona (mismos parametros
        que la simulacion en marcha, leidos del servicio), con las demas zonas
        gobernadas por histeresis. Asi cada zona puede entrenarse a toda
        velocidad y EN PARALELO sin pelearse por el reloj de la GUI.

      FASE 2 TIEMPO REAL
        Se conecta al servicio, reclama su zona (la GUI la pasa a 'externo') y
        decide una vez por muestra (a Ts), aprendiendo o no segun el modo.
    """

    def __init__(self, agente, cfg: ConfigZona, cliente: ClienteMZ = None,
                 publicador: PublicadorRL = None):
        self.agente = agente
        self.cfg = cfg
        self.cliente = cliente
        self.pub = publicador
        self.nivel_previo = 0.0
        self.pasos_desde_cambio = 0
        self.pasos_totales = 0
        self.forzadas = 0
        self.steps_por_decision = 6
        self._ultimos = deque(maxlen=40)

    # ---- utilidades ----------------------------------------------------------
    def _cadencia(self, params):
        """Pasos dt que abarca UNA decision.

        Se toma tal cual del simulador cuando lo publica ('pasos_por_muestra');
        si no, se deduce con round(Ts/dt). Entrenar con una cadencia distinta de
        la de uso invalida lo aprendido: un episodio en el que cada decision
        dura 6 pasos no dice nada sobre un lazo que decide cada 60."""
        if params and params.get("pasos_por_muestra"):
            return max(1, int(params["pasos_por_muestra"]))
        dt = float((params or {}).get("dt", 10.0) or 10.0)
        ts = float((params or {}).get("sample_time", dt) or dt)
        return max(1, int(round(ts / dt))) if dt > 0 else 1

    def informe_cadencia(self, params=None):
        """Imprime la cadencia real y los avisos de estabilidad del simulador."""
        if self.cliente is None:
            return None
        try:
            d = self.cliente.cadencia()
        except Exception:
            return None
        print(f"  Cadencia real: dt={d['dt']:g} s, Ts={d['sample_time_eff']:g} s "
              f"-> cada decision abarca {d['pasos_por_muestra']} paso(s) dt "
              f"({d['muestras_por_hora']:.0f} decisiones por hora simulada).")
        for aviso in d.get("avisos", []):
            print(f"  AVISO: {aviso}")
        return d

    def _nivel_seguro(self, o, nivel):
        """Interlock de dos lados. Es una barrera fisica, no parte del
        aprendizaje: la politica decide dentro de esos limites.

          - Por ENCIMA del limite alto se cierra: encharcar mata por asfixia.
          - Por DEBAJO del limite bajo se abre: un lecho de vermicompostaje
            reseco mata a las lombrices igual de bien, y una politica a medio
            entrenar puede quedarse cerrada indefinidamente (le pasa a un agente
            que aprendio con las zonas de arriba regando y de pronto dejan de
            hacerlo).
        """
        m = float(o.get("moisture", 0.0))
        if m >= self.cfg.limite and nivel > 0.0:
            self.forzadas += 1
            return 0.0
        if m <= self.cfg.limite_seco and nivel <= 0.0:
            self.forzadas += 1
            return 1.0
        return nivel

    def _sincronizar_banda(self, resp):
        """Toma en vivo la banda objetivo que publica la GUI para ESTA zona."""
        if not self.cfg.banda_desde_gui or not isinstance(resp, dict):
            return
        bands = resp.get("bands")
        lo = hi = None
        if isinstance(bands, list) and self.cfg.zona < len(bands):
            try:
                lo, hi = float(bands[self.cfg.zona][0]), float(bands[self.cfg.zona][1])
            except (TypeError, ValueError, IndexError):
                lo = hi = None
        if lo is None:
            lo, hi = resp.get("band_low"), resp.get("band_high")
        try:
            lo, hi = float(lo), float(hi)
        except (TypeError, ValueError):
            return
        if hi > lo:
            self.cfg.target_low, self.cfg.target_high = lo, hi

    def _registrar(self, o, a, r, info):
        self._ultimos.append({
            "t": round(float(o.get("time_hours", 0.0)), 3),
            "h": round(float(o.get("moisture", 0.0)), 4),
            "h_arriba": round(float(o.get("moisture_up", 0.0)), 4),
            "h_abajo": round(float(o.get("moisture_down", 0.0)), 4),
            "tend": float(o.get("trend", 0.0)),
            "arriba": round(float(o.get("carga_arriba", 0.0)), 3),
            "a": int(a), "r": round(float(r), 3),
            **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in (info or {}).items()},
        })

    def _publicar(self, o, forzar=False, met=None):
        if self.pub is None:
            return
        extra = {
            "banda": [self.cfg.target_low, self.cfg.target_high],
            "limite": self.cfg.limite,
            "observacion": {k: o.get(k) for k in
                            ("moisture", "temperature", "trend", "carga_arriba",
                             "carga_abajo", "moisture_up", "moisture_down", "time_hours",
                             "valve_level", "pos_norm", "x_center",
                             "moisture_grad", "moisture_head", "moisture_tail",
                             "homogeneidad", "subzonas")},
            "ultimos": list(self._ultimos),
            "pasos": self.pasos_totales,
            "forzadas": self.forzadas,
        }
        if met is not None:
            extra["metricas"] = {"pct_banda": met.pct_banda, "pct_sat": met.pct_sat,
                                 "r_medio": met.r_medio, "n": met.n}
        self.pub.publicar(extra, forzar=forzar)

    # ---- FASE 1: pre-entrenamiento acelerado en modelo local -----------------
    def _sim_local(self, params_dict=None):
        campos = {f for f in BedParams.__dataclass_fields__}
        p = BedParams(**{k: v for k, v in (params_dict or {}).items() if k in campos})
        # Condiciones iniciales "de fabrica": la evaluacion siempre arranca de
        # aqui, aunque los episodios de entrenamiento las hayan aleatorizado.
        self._init_base = (p.init_moisture, p.init_moisture_grad, p.init_temperature)
        return MultiZoneBed(p)

    def entrenar_offline(self, params_dict=None, verbose=True):
        cfg = self.cfg
        sim = self._sim_local(params_dict)
        z = min(cfg.zona, sim.n_zones - 1)
        self.steps_por_decision = self._cadencia(sim.get_params())
        self.cadencia_entrenamiento = self.steps_por_decision
        if verbose:
            d = sim.diagnostico_cadencia()
            print(f"[{self.agente.nombre} | zona {z}] Pre-entrenamiento acelerado en "
                  f"modelo local ({sim.n_zones} zonas x {sim.sub} sub-zonas). "
                  f"Vecinos: {cfg.vecinos_offline}.")
            print(f"  Cadencia: dt={d['dt']:g} s, Ts efectivo={d['sample_time_eff']:g} s "
                  f"-> {self.steps_por_decision} paso(s) dt por decision.")
            for aviso in d.get("avisos", []):
                print(f"  AVISO: {aviso}")
        if hasattr(self.agente, "sembrar_histeresis") and not self.agente.q:
            n = self.agente.sembrar_histeresis(cfg.target_low, cfg.target_high)
            print(f"  Tabla sembrada con la histeresis de referencia ({n} celdas): "
                  f"se explora alrededor de un control que ya funciona en vez de "
                  f"partir de cero.")
        mejor, sin_mejora = 0.0, 0
        for ep in range(1, cfg.episodios_max + 1):
            total_r = self._episodio_offline(sim, z, explorar=True)
            if verbose:
                eps = getattr(self.agente, "epsilon", None)
                eps_txt = f"eps={eps:.3f} " if isinstance(eps, (int, float)) else ""
                st = sim.zone_state(z)
                print(f"  ep={ep:3d}/{cfg.episodios_max}  {eps_txt}"
                      f"R={total_r:9.2f}  hum={st['moisture']:.3f}  T={st['temperature']:.1f}C")
            if ep >= cfg.episodios_min and ep % cfg.evaluar_cada == 0:
                pct, detalle, resp = self._evaluar(sim, z)
                partes = "  ".join(
                    f"{modo}: {p*100:5.1f}% (sat {s_*100:.0f}%, seco {sec*100:.0f}%)"
                    for modo, p, s_, sec in detalle)
                print(f"    [eval] {partes}  | respaldo {resp*100:.0f}%  "
                      f"(meta {cfg.objetivo_pct*100:.0f}% en el PEOR escenario)")
                mejor = max(mejor, pct)
                if pct >= cfg.objetivo_pct and resp <= cfg.max_respaldo:
                    print(f"  OBJETIVO ALCANZADO en ep {ep} ({pct*100:.1f}% en el peor "
                          f"escenario, {resp*100:.0f}% de decisiones delegadas).")
                    self.agente.save()
                    return True
                if pct >= cfg.objetivo_pct:
                    print(f"      (buen resultado pero el {resp*100:.0f}% de las "
                          f"decisiones las tomo la histeresis de respaldo: la tabla "
                          f"aun esta vacia, se sigue entrenando)")
                if pct > mejor + 0.02:
                    sin_mejora = 0
                else:
                    sin_mejora += 1
                if sin_mejora >= cfg.paciencia:
                    sin_mejora = 0
                    if hasattr(self.agente, "reiniciar_exploracion"):
                        self.agente.reiniciar_exploracion(cfg.epsilon_reinicio)
                        print(f"      sin avance en {cfg.paciencia} evaluaciones: se "
                              f"reabre la exploracion para salir del atasco")
        print(f"  Tope de {cfg.episodios_max} episodios (mejor {mejor*100:.1f}%). "
              f"Se continua en tiempo real.")
        self.agente.save()
        return False

    def _reset_local(self, sim):
        """Reinicia el modelo local, opcionalmente con condiciones aleatorias
        (aleatorizacion de dominio: cubre mas estados y evita sobreajustar a un
        unico arranque)."""
        cfg = self.cfg
        if cfg.aleatorizar_inicio:
            sim.params.init_moisture = random.uniform(0.25, 1.05)
            sim.params.init_moisture_grad = random.uniform(-0.25, 0.25)
            sim.params.init_temperature = random.uniform(16.0, 32.0)
        else:
            base = getattr(self, "_init_base", None)
            if base:
                (sim.params.init_moisture, sim.params.init_moisture_grad,
                 sim.params.init_temperature) = base
        sim.reset()
        self.agente.on_reset()
        self.nivel_previo = 0.0
        self.pasos_desde_cambio = 0

    def _vecinos(self, sim, z, modo=None):
        """Como se comportan las OTRAS zonas durante un episodio.

        El modo 'mixto' sortea el comportamiento en cada episodio. Es
        imprescindible: si las vecinas riegan siempre bien, las zonas de abajo
        reciben tanta agua por la cascada que 'no abrir nunca' les puntua casi
        el 100 %, aprenden esa politica degenerada y se secan en cuanto la
        cabecera deja de regar. Variando a las vecinas, la zona tiene que
        aprender a valerse sola tambien.
        """
        modo = modo or self.cfg.vecinos_offline
        if modo == "mixto":
            modo = random.choices(["histeresis", "cerrado", "aleatorio"],
                                  weights=[0.5, 0.3, 0.2])[0]
        ctrl = [HisteresisZona(self.cfg.target_low, self.cfg.target_high)
                for _ in range(sim.n_zones)]

        def aplicar(estado):
            for i in range(sim.n_zones):
                if i == z:
                    continue
                if modo == "cerrado":
                    sim.set_level(i, 0.0)
                elif modo == "aleatorio":
                    if random.random() < 0.08:
                        sim.set_level(i, random.choice([0.0, 1.0]))
                else:
                    sim.set_level(i, ctrl[i].decidir(estado["zones"][i]["moisture"]))
        aplicar.modo = modo
        return aplicar

    def _episodio_offline(self, sim, z, explorar=True):
        cfg = self.cfg
        self._reset_local(sim)
        aplicar_vecinos = self._vecinos(sim, z)
        o = preparar_obs(sim.zone_state(z), cfg.target_low, cfg.target_high, 0.0, 0)
        a = self.agente.act(o, explore=explorar)
        nivel = self._nivel_seguro(o, self.agente.acciones[a])
        total = 0.0
        for _ in range(cfg.pasos_por_episodio):
            aplicar_vecinos(sim.get_state())
            sim.set_level(z, nivel)
            for _ in range(self.steps_por_decision):
                sim.step()
            o2 = preparar_obs(sim.zone_state(z), cfg.target_low, cfg.target_high,
                              nivel, self.pasos_desde_cambio)
            r = recompensa(o2, nivel, self.nivel_previo,
                           cfg.target_low, cfg.target_high, cfg.pesos)
            total += r
            a2 = self.agente.act(o2, explore=explorar)
            nivel2 = self._nivel_seguro(o2, self.agente.acciones[a2])
            self.agente.observe(o, a, r, o2, a2, done=False)
            self.pasos_desde_cambio = 0 if nivel2 != nivel else self.pasos_desde_cambio + 1
            self.nivel_previo, o, a, nivel = nivel, o2, a2, nivel2
            self.pasos_totales += 1
        self.agente.end_episode()
        return total

    def _evaluar(self, sim, z):
        """Evalua la politica greedy en DOS escenarios y devuelve el peor.

          1) vecinas regando bien (situacion nominal)
          2) vecinas cerradas (la zona se queda sola)

        Evaluar solo el primero es lo que dejaba pasar politicas degeneradas:
        con la cascada alimentandola, una zona baja puede quedarse SIEMPRE
        cerrada y aun asi marcar el 100 % en banda... hasta que la cabecera para
        y se seca. Se mide ademas cuantas decisiones tuvo que delegar en la
        histeresis de respaldo: si son muchas, la tabla aun esta vacia y el
        merito no es del agente.
        """
        cfg = self.cfg
        peor = None
        detalle = []
        respaldo_total = 0
        for modo in ("histeresis", "cerrado"):
            guardar = cfg.aleatorizar_inicio
            cfg.aleatorizar_inicio = False
            self._reset_local(sim)
            cfg.aleatorizar_inicio = guardar
            aplicar_vecinos = self._vecinos(sim, z, modo=modo)
            antes = getattr(self.agente, "sin_experiencia", 0)
            o = preparar_obs(sim.zone_state(z), cfg.target_low, cfg.target_high, 0.0, 0)
            nivel = self._nivel_seguro(
                o, self.agente.acciones[self.agente.act(o, explore=False)])
            en = sat = seco = 0
            for _ in range(cfg.pasos_evaluacion):
                aplicar_vecinos(sim.get_state(ligero=True))
                sim.set_level(z, nivel)
                for _ in range(self.steps_por_decision):
                    sim.step()
                o = preparar_obs(sim.zone_state(z), cfg.target_low, cfg.target_high,
                                 nivel, 0)
                m = o["moisture"]
                en += 1 if cfg.target_low <= m <= cfg.target_high else 0
                sat += 1 if m >= cfg.limite else 0
                seco += 1 if m <= cfg.limite_seco else 0
                nivel = self._nivel_seguro(
                    o, self.agente.acciones[self.agente.act(o, explore=False)])
            respaldo_total += getattr(self.agente, "sin_experiencia", 0) - antes
            pct = en / cfg.pasos_evaluacion
            detalle.append((modo, pct, sat / cfg.pasos_evaluacion,
                            seco / cfg.pasos_evaluacion))
            peor = pct if peor is None else min(peor, pct)
        frac_respaldo = respaldo_total / max(1, 2 * cfg.pasos_evaluacion)
        return peor, detalle, frac_respaldo

    # ---- FASE 2: tiempo real sobre el servicio -------------------------------
    def _esperar_muestras(self, ultimo_indice):
        cfg = self.cfg
        t0 = time.time()
        avisado = False
        while True:
            r = self.cliente.muestra(zone=cfg.zona, since=ultimo_indice)
            idx = int(r.get("sample_index", ultimo_indice) or ultimo_indice)
            if idx > ultimo_indice:
                nuevas = r.get("nuevas") or []
                if not nuevas and r.get("sample"):
                    nuevas = [r["sample"]]
                return nuevas, idx, r
            if time.time() - t0 > cfg.max_espera_s and not avisado:
                print(f"  [espera] La simulacion no avanza (running={r.get('running')}, "
                      f"velocidad={r.get('speed')}). Pulsa 'Iniciar' en la interfaz.")
                avisado = True
            time.sleep(cfg.poll_s)

    def tiempo_real(self, aprender=True):
        cfg = self.cfg
        cli = self.cliente
        z = cfg.zona
        cli.reclamar(z, "externo")
        d = self.informe_cadencia()
        previa = getattr(self, "cadencia_entrenamiento", None)
        if d and previa and int(d["pasos_por_muestra"]) != int(previa):
            print(f"  AVISO: se entreno con {previa} paso(s) por decision y ahora el "
                  f"simulador va a {d['pasos_por_muestra']}. Han cambiado dt o Ts: "
                  f"la dinamica que ve el control no es la misma con la que aprendio. "
                  f"Conviene reentrenar (--modo reentrenar).")
        if aprender and hasattr(self.agente, "activar_online"):
            self.agente.activar_online()
        met = Metricas(cfg.ventana_metrica, cfg.target_low, cfg.target_high,
                       cfg.pesos.margen_seguridad)
        modo = "APRENDIZAJE CONTINUO" if aprender else "CONTROL (sin aprender)"
        print(f"\n[{self.agente.nombre} | zona {z}] {modo}, sincronizado con el "
              f"muestreo del simulador (una decision por muestra). Ctrl+C para detener.")

        r0 = cli.muestra(zone=z)
        self._sincronizar_banda(r0)
        bruto = r0.get("sample") or cli.estado(zone=z)
        ultimo_indice = int(r0.get("sample_index", 0) or 0)
        o = preparar_obs(bruto, cfg.target_low, cfg.target_high, 0.0, 0)
        a = self.agente.act(o, explore=aprender)
        nivel = self._nivel_seguro(o, self.agente.acciones[a])
        cli.set_nivel(z, nivel)
        contador = 0
        try:
            while True:
                muestras, ultimo_indice, resp = self._esperar_muestras(ultimo_indice)
                self._sincronizar_banda(resp)
                met.low, met.high = cfg.target_low, cfg.target_high
                met.lim = cfg.limite
                for bruto in muestras:
                    o2 = preparar_obs(bruto, cfg.target_low, cfg.target_high,
                                      nivel, self.pasos_desde_cambio)
                    r = recompensa(o2, nivel, self.nivel_previo,
                                   cfg.target_low, cfg.target_high, cfg.pesos)
                    a2 = self.agente.act(o2, explore=aprender)
                    nivel2 = self._nivel_seguro(o2, self.agente.acciones[a2])
                    if aprender:
                        self.agente.observe(o, a, r, o2, a2, done=False)
                    self._registrar(o2, a2, r, self.agente.ultimo_update)
                    met.push(o2, r)
                    self.pasos_desde_cambio = (0 if nivel2 != nivel
                                               else self.pasos_desde_cambio + 1)
                    self.nivel_previo, o, a, nivel = nivel, o2, a2, nivel2
                    contador += 1
                    self.pasos_totales += 1
                    if aprender and contador % cfg.mini_episodio == 0:
                        self.agente.end_episode()
                        self.agente.on_reset()
                cli.set_nivel(z, nivel)
                self._publicar(o, met=met)
                if contador % cfg.imprimir_cada < max(1, len(muestras)):
                    print(f"  Z{z} t={o['time_hours']:7.2f}h  hum={o['moisture']:.3f}  "
                          f"T={o['temperature']:5.2f}C  arriba={o['carga_arriba']:4.2f}  "
                          f"riego={nivel:3.1f}  banda=[{cfg.target_low:.2f},"
                          f"{cfg.target_high:.2f}]  enBanda={met.pct_banda:5.1f}%  "
                          f"sat={met.pct_sat:4.1f}%  r={met.r_medio:6.2f}")
                if aprender and contador % cfg.guardar_cada < max(1, len(muestras)):
                    self.agente.save()
        except (ConnectionError, OSError) as e:
            if aprender:
                self.agente.end_episode()
                self.agente.save()
            print(f"\n[{self.agente.nombre} | zona {z}] Se perdio la conexion con el "
                  f"simulador: {e}")
            print("  Lo aprendido queda guardado. Vuelve a lanzarlo cuando la "
                  "ventana del simulador este abierta y en marcha.")
            return None
        except KeyboardInterrupt:
            if aprender:
                self.agente.end_episode()
                self.agente.save()
            self._publicar(o, forzar=True, met=met)
            try:
                cli.set_nivel(z, 0.0)
                cli.liberar(z, "manual")
            except Exception:
                pass
            print(f"\n[{self.agente.nombre} | zona {z}] Detenido. hum={o['moisture']:.4f}  "
                  f"enBanda={met.pct_banda:.1f}%  cierres de seguridad={self.forzadas}")
            return o

    # ---- ciclo completo ------------------------------------------------------
    def run(self, modo="auto", offline=True):
        """modo: 'controlar' | 'entrenar' | 'reentrenar'."""
        hay_previo = self.agente.existe_guardado()
        if modo == "auto":
            modo = "entrenar" if hay_previo else "reentrenar"
        if modo == "controlar":
            if not hay_previo or not self.agente.load():
                print("No hay entrenamiento previo utilizable para esta zona; "
                      "arranca en modo entrenar.")
                return
            self._publicar_inicial()
            self.tiempo_real(aprender=False)
            return
        if modo == "entrenar" and hay_previo:
            if self.agente.load():
                print("Se continua el entrenamiento sobre lo guardado.")
            else:
                print("Lo guardado no es utilizable con esta configuracion: "
                      "se entrena desde cero.")
        elif modo == "reentrenar":
            print("Entrenamiento desde cero (se ignora lo guardado).")

        params = None
        if self.cliente is not None:
            try:
                params = self.cliente.params()
            except Exception:
                params = None
        self._publicar_inicial()
        if offline:
            self.entrenar_offline(params)
        if self.cliente is not None:
            self.tiempo_real(aprender=True)
        else:
            print("Sin conexion al simulador: solo se hizo el pre-entrenamiento.")

    def _publicar_inicial(self):
        if self.pub is not None:
            self.pub.publicar({"banda": [self.cfg.target_low, self.cfg.target_high],
                               "pasos": 0}, forzar=True)


def ruta_tabla(zona, etiqueta, carpeta=DIR_TABLAS):
    """Ruta de la tabla/politica de una zona. La compatibilidad de lo guardado
    no se resuelve por el nombre sino por la FIRMA que va dentro del archivo
    (numero de bins, umbrales, acciones): si no coincide, no se carga."""
    os.makedirs(carpeta, exist_ok=True)
    return os.path.join(carpeta, f"z{zona}_{etiqueta}.json")
