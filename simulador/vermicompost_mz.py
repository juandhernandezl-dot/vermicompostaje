#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vermicompost_mz.py  -  NUCLEO MULTIZONA + SERVICIO LOCAL
========================================================

Evolucion del simulador de una sola zona a una CAMA DE VERMICOMPOSTAJE REAL:
larga, con PENDIENTE, y con VARIOS ASPERSORES a lo largo de su longitud.

MALLA: ZONAS DE CONTROL Y SUB-ZONAS DE CALCULO
----------------------------------------------
La cama de longitud L se divide en N ZONAS de longitud 'zone_length' (una por
aspersor: es la unidad de CONTROL), y cada zona en M SUB-ZONAS o celdas (la
unidad de CALCULO). El estado (agua y temperatura) vive en las celdas; lo que
mide y controla cada zona es el PROMEDIO de sus celdas.

Con M=1 la zona es un tanque de mezcla perfecta: el riego se reparte al instante
por sus 2 m, que es una idealizacion fuerte. Con M>1 el agua avanza celda a
celda y aparece un FRENTE DE HUMEDAD: mojar la cabecera de una zona tarda en
notarse en su pie, la respuesta deja de ser instantanea y el control tiene que
lidiar con un retardo de transporte real.

Los coeficientes de transporte se reescalan con la malla (el flujo entre celdas
va como 1/dx, la difusion como 1/dx^2), de modo que refinar NO cambia cuanto
riego entra en cada zona ni el transporte macroscopico: solo resuelve mejor el
reparto interno. La zona 0 es la MAS ALTA y la N-1 la MAS BAJA:

    [Z0]  --escorrentia-->  [Z1]  --escorrentia-->  [Z2]  --> lixiviado
     ^                       ^                       ^
    asp.0                   asp.1                   asp.2

Cada CELDA es un volumen finito con dos estados (agua W_i, temperatura T_i);
los indices i recorren celdas, no zonas:

    dW_i/dt = riego_i + lateral_(i-1) + difusion_i
              - evaporacion_i - lateral_i - percolacion_i

    riego_i     = SUM_k A[i][k] * caudal * u_k     (reparto geometrico del aspersor)
    lateral_i   = (pendiente/pend_ref) * [ k_lat * W_fc * theta_i^p        <- no saturado
                                         + k_run * max(0, W_i - W_fc_i) ] <- agua libre
                                                                        -> va a i+1
    percolacion_i = k_drain * max(0, W_i - W_fc_i)                      -> se pierde
    difusion_i  = k_dif * (theta_(i-1) + theta_(i+1) - 2*theta_i)       (capilaridad)

    C_i dT_i/dt = G*(T_amb - T_i) + riego_i*c_w*(T_agua - T_i)
                  + lateral_(i-1)*c_w*(T_(i-1) - T_i) - evap_i*Lv
                  + k_cond*(T_(i-1) - T_i) + k_cond*(T_(i+1) - T_i)

Con pendiente 0 y difusion 0 las zonas quedan DESACOPLADAS hidraulicamente:
el modelo se reduce a N copias independientes del simulador original.

ACOPLE POR ASPERSION
--------------------
Un aspersor de radio r moja mas alla de su zona. A[i][k] es la fraccion del
caudal del aspersor k que cae en la zona i, calculada con un nucleo triangular
(mas agua cerca del centro) integrado sobre cada zona. Con r=2 m y zonas de
2 m, cada aspersor entrega ~62% a su zona y ~19% a cada vecina: los actuadores
NO son independientes, y el control tiene que aprenderlo.

CAPAS
-----
  1) MultiZoneBed      : nucleo numerico puro (sin Tk ni red).
  2) MultiZoneService  : socket TCP 127.0.0.1:8765, JSON por linea, con
                         comandos por zona (retro-compatible con el protocolo
                         de una zona: si no se indica 'zone' se usa la 0).

Solo libreria estandar.

Uso rapido:
    python3 vermicompost_mz.py --headless 2000     # simula y resume
    python3 vermicompost_mz.py --serve             # solo servicio, sin GUI
    python3 vermicompost_mz.py --test              # autopruebas
"""

import argparse
import json
import math
import socket
import threading
import time
from dataclasses import dataclass, asdict, fields

HOST = "127.0.0.1"
PORT = 8765


# =============================================================================
# PARAMETROS
# =============================================================================

@dataclass
class BedParams:
    """Parametros del lecho completo. Las magnitudes 'por metro' hacen que el
    modelo escale de forma coherente cuando se cambia la longitud L."""
    # --- Geometria de la cama (NUEVO) ---
    bed_length: float = 6.0            # m   longitud total de la cama
    bed_width: float = 1.2             # m   ancho (solo informativo/dibujo)
    bed_slope: float = 2.0             # %   pendiente longitudinal
    spray_radius: float = 2.0          # m   radio de aspersion de un actuador
    zone_length: float = 2.0           # m   separacion entre aspersores = long. de zona
    subzonas: float = 4.0              # -   celdas de calculo dentro de cada zona
    slope_ref: float = 2.0             # %   pendiente de referencia para k_lat
    # --- Sustrato ---
    soil_dry_mass_per_m: float = 25.0  # kg/m  masa seca por metro de cama
    soil_specific_heat: float = 1200.0 # J/kg.K
    # --- Agua ---
    field_capacity: float = 0.45       # kg/kg
    saturation_capacity: float = 0.60  # kg/kg (> field_capacity)
    drainage_coeff: float = 1.0e-3     # 1/s   percolacion vertical (se pierde)
    lateral_coeff: float = 8.0e-6      # 1/s   flujo lateral NO saturado (siempre activo)
    lateral_exponent: float = 3.0      # -     no linealidad del flujo no saturado
    runoff_coeff: float = 1.5e-3       # 1/s   escorrentia rapida del agua libre
    diffusion_coeff: float = 1.0e-4    # 1/s   difusion capilar entre vecinas
    inflow_rate: float = 4.0e-3        # kg/s  caudal de UN aspersor abierto
    water_temperature: float = 18.0    # C
    water_specific_heat: float = 4186.0
    # --- Evaporacion ---
    evap_base_rate: float = 3.0e-5     # kg/s por METRO de cama
    evap_temp_ref: float = 20.0        # C
    evap_temp_sensitivity: float = 0.06
    latent_heat: float = 2.26e6        # J/kg
    # --- Termico y ambiente ---
    thermal_conductance: float = 10.0  # W/K por METRO de cama
    lateral_conductance: float = 5.0   # W/K conduccion entre zonas contiguas
    ambient_mean: float = 24.0         # C
    ambient_amp: float = 4.0           # C
    ambient_period: float = 86400.0    # s
    # --- Numerico e inicial ---
    dt: float = 10.0                   # s
    sample_time: float = 60.0          # s
    init_moisture: float = 0.60        # fraccion de capacidad de campo
    init_moisture_grad: float = 0.0    # dif. de humedad inicial entre zona alta y baja
    init_temperature: float = 22.0     # C


PARAM_SCHEMA = [
    # clave, grupo, etiqueta, unidad, min, max, resolucion, descripcion
    ("bed_length",           "Geometria de la cama", "Longitud de la cama L",   "m",      1.0,    24.0,    0.5,    "Longitud total del lecho. Junto con la separacion entre aspersores determina CUANTAS zonas hay (N = round(L / zone_length)). Cambiarla reconstruye las zonas."),
    ("zone_length",          "Geometria de la cama", "Separacion aspersores",   "m",      0.5,    6.0,     0.25,   "Distancia entre aspersores = longitud de cada zona de control. Con L=6 m y 2 m -> 3 zonas."),
    ("spray_radius",         "Geometria de la cama", "Radio de aspersion",      "m",      0.25,   6.0,     0.25,   "Radio mojado por un aspersor. Si supera la mitad de la zona, el aspersor riega tambien a las zonas vecinas (acople de actuadores)."),
    ("subzonas",             "Geometria de la cama", "Sub-zonas por zona",      "celdas", 1.0,    16.0,    1.0,    "Numero de celdas de calculo en que se divide cada zona. Con 1 la zona es un tanque de mezcla perfecta (el riego se reparte al instante por sus 2 m). Con mas celdas el agua tarda en recorrer la zona: aparece un frente de humedad y la lectura de la zona es el PROMEDIO de sus celdas. Mas celdas = mas realismo y mas coste de calculo."),
    ("bed_width",            "Geometria de la cama", "Ancho de la cama",        "m",      0.3,    3.0,     0.1,    "Ancho del lecho. Informativo y para el dibujo en planta."),
    ("bed_slope",            "Geometria de la cama", "Pendiente",               "%",      0.0,    12.0,    0.25,   "Inclinacion longitudinal. Escala la escorrentia de cada zona hacia la de abajo: con 0% las zonas quedan hidraulicamente independientes."),
    ("slope_ref",            "Geometria de la cama", "Pendiente de referencia", "%",      0.25,   12.0,    0.25,   "Pendiente a la que el coeficiente lateral vale su valor nominal. Solo normaliza."),
    ("soil_dry_mass_per_m",  "Sustrato",             "Masa seca por metro",     "kg/m",   2.0,    120.0,   1.0,    "Masa seca de sustrato por metro de cama. La masa de una zona es este valor por su longitud: define W_fc, W_sat y la inercia termica."),
    ("soil_specific_heat",   "Sustrato",             "Calor esp. sustrato",     "J/kg.K", 500.0,  2500.0,  10.0,   "Calor especifico del sustrato seco. Interviene en C = m*c_s + W*c_w."),
    ("field_capacity",       "Agua",                 "Capacidad de campo",      "frac",   0.10,   0.80,    0.01,   "Agua retenida por kg de sustrato seco. Umbral W_fc: por encima hay percolacion y escorrentia. 'moisture' = W / W_fc."),
    ("saturation_capacity",  "Agua",                 "Capacidad de saturacion", "frac",   0.20,   0.95,    0.01,   "Agua a saturacion por kg seco (debe ser > cap. de campo). Tope fisico de W."),
    ("drainage_coeff",       "Agua",                 "Coef. percolacion",       "1/s",    0.0,    5.0e-3,  1.0e-4, "Perdida vertical del exceso sobre W_fc (lixiviado que sale del sistema)."),
    ("lateral_coeff",        "Agua",                 "Coef. flujo lateral",     "1/s",    0.0,    1.0e-4,  5.0e-7, "Flujo que baja a la zona siguiente en regimen NO saturado (por debajo de capacidad de campo). Es el acople permanente de la cascada: crece con la pendiente y con la humedad elevada a 'lateral_exponent'."),
    ("lateral_exponent",     "Agua",                 "Exponente flujo lateral", "-",      1.0,    8.0,     0.25,   "No linealidad de la conductividad no saturada: con exponente 3 un lecho al 90% de capacidad de campo transfiere unas 8 veces mas agua que al 45%."),
    ("runoff_coeff",         "Agua",                 "Coef. escorrentia libre", "1/s",    0.0,    1.0e-2,  2.0e-4, "Escorrentia RAPIDA del agua libre (por encima de capacidad de campo) hacia la zona de abajo. Es la que provoca encharcamientos en cadena."),
    ("diffusion_coeff",      "Agua",                 "Difusion capilar",        "1/s",    0.0,    2.0e-3,  5.0e-5, "Redistribucion capilar simetrica entre zonas contiguas (independiente de la pendiente)."),
    ("inflow_rate",          "Agua",                 "Caudal por aspersor",     "kg/s",   0.0,    2.0e-2,  2.0e-4, "Agua que entrega UN aspersor abierto, repartida entre zonas segun el radio de aspersion."),
    ("water_temperature",    "Agua",                 "Temp. del agua",          "C",      0.0,    40.0,    0.5,    "Temperatura del agua de riego. Si es menor que T el riego enfria el lecho."),
    ("water_specific_heat",  "Agua",                 "Calor esp. agua",         "J/kg.K", 3000.0, 5000.0,  10.0,   "Calor especifico del agua. Afecta a C y al calor transportado por riego y escorrentia."),
    ("evap_base_rate",       "Evaporacion",          "Tasa evap. base",         "kg/s/m", 0.0,    3.0e-4,  5.0e-6, "Evaporacion por metro de cama a la temperatura de referencia y suelo humedo."),
    ("evap_temp_ref",        "Evaporacion",          "Temp. ref. evap.",        "C",      0.0,    40.0,    0.5,    "Temperatura de referencia del factor exp(k*(T-T_ref))."),
    ("evap_temp_sensitivity","Evaporacion",          "Sens. evap. a T",         "1/C",    0.0,    0.20,    0.005,  "Sensibilidad exponencial de la evaporacion a la temperatura."),
    ("latent_heat",          "Evaporacion",          "Calor latente vap.",      "J/kg",   1.0e6,  3.0e6,   1.0e4,  "Calor latente de vaporizacion: define el enfriamiento evaporativo."),
    ("thermal_conductance",  "Termico",              "Conductancia amb.",       "W/K/m",  0.0,    50.0,    0.5,    "Acoplamiento termico con el ambiente por metro de cama: q = G*Lz*(T_amb - T)."),
    ("lateral_conductance",  "Termico",              "Conduccion entre zonas",  "W/K",    0.0,    50.0,    0.5,    "Conduccion termica entre zonas contiguas. Suaviza las diferencias de temperatura a lo largo de la cama."),
    ("ambient_mean",         "Termico",              "Temp. amb. media",        "C",      0.0,    40.0,    0.5,    "Valor medio diario de la temperatura ambiente."),
    ("ambient_amp",          "Termico",              "Amplitud amb.",           "C",      0.0,    20.0,    0.5,    "Amplitud de la oscilacion diaria del ambiente."),
    ("ambient_period",       "Termico",              "Periodo amb.",            "s",      3600.0, 172800.0,3600.0, "Periodo de la oscilacion ambiente (86400 s = 1 dia)."),
    ("dt",                   "Numerico",             "Paso de integracion dt",  "s",      1.0,    60.0,    1.0,    "Paso del integrador de Euler. Menor = mas preciso y estable."),
    ("sample_time",          "Numerico",             "Tiempo de muestreo Ts",   "s",      1.0,    3600.0,  1.0,    "Cada cuanto se registra telemetria y decide el control (retencion de orden cero). Conviene Ts >= dt."),
    ("init_moisture",        "Numerico",             "Humedad inicial",         "frac",   0.05,   1.20,    0.05,   "Humedad de arranque (fraccion de cap. de campo). Solo aplica al reiniciar."),
    ("init_moisture_grad",   "Numerico",             "Gradiente inicial",       "frac",  -0.60,   0.60,    0.05,   "Diferencia de humedad inicial entre la zona mas alta y la mas baja (positivo = abajo mas humedo). Solo al reiniciar."),
    ("init_temperature",     "Numerico",             "Temp. inicial",           "C",      5.0,    40.0,    0.5,    "Temperatura de arranque. Solo aplica al reiniciar."),
]

PARAM_INFO = {e[0]: {"key": e[0], "group": e[1], "label": e[2], "unit": e[3],
                     "min": e[4], "max": e[5], "res": e[6], "description": e[7]}
              for e in PARAM_SCHEMA}


# =============================================================================
# NUCLEO MULTIZONA
# =============================================================================

class MultiZoneBed:
    """
    Cama de vermicompostaje dividida en N zonas en cascada.

    Estados por zona: W_i (kg de agua) y T_i (C).
    Entradas: un nivel de apertura u_i en [0, 1] por aspersor (0 = cerrado,
    1 = caudal nominal; los valores intermedios permiten control modulado).
    """

    HISTORY_CAP = 5000
    DENSE_CAP = 12000
    SPRAY_SLICES = 400      # resolucion de la integracion del patron de aspersion

    def __init__(self, params: BedParams = None):
        self.params = params if params is not None else BedParams()
        self._construir_geometria()
        self.reset()

    # ---- geometria y reparto de aspersion ------------------------------------
    def _construir_geometria(self):
        """Malla de calculo.

        La cama se divide en N ZONAS (una por aspersor, es la unidad de CONTROL)
        y cada zona en M SUB-ZONAS o celdas (es la unidad de CALCULO). El estado
        vive en las celdas; lo que ve el control es el promedio de las celdas de
        su zona. Con M=1 se recupera exactamente el modelo de tanque por zona.
        """
        p = self.params
        zl = max(0.05, float(p.zone_length))
        self.n_zones = max(1, int(round(float(p.bed_length) / zl)))
        self.zone_len = float(p.bed_length) / self.n_zones
        self.sub = max(1, int(round(float(p.subzonas))))
        self.n_cells = self.n_zones * self.sub
        self.cell_len = self.zone_len / self.sub
        self.x_cells = [(c + 0.5) * self.cell_len for c in range(self.n_cells)]
        self.x_centers = [(i + 0.5) * self.zone_len for i in range(self.n_zones)]
        # celdas de cada zona (contiguas y ordenadas de cabecera a pie)
        self.celdas_de = [list(range(i * self.sub, (i + 1) * self.sub))
                          for i in range(self.n_zones)]
        self.zona_de = [c // self.sub for c in range(self.n_cells)]
        self.spray = self._matriz_aspersion()

    def _matriz_aspersion(self):
        """A[c][k] = fraccion del caudal del aspersor k que cae en la CELDA c.

        Nucleo triangular w(x) = max(0, 1 - |x - x_k| / r) integrado por
        rebanadas y normalizado por columnas (todo el caudal del aspersor acaba
        dentro del lecho). Al trabajar por celdas, el patron de mojado queda
        mucho mejor resuelto: el centro del alcance recibe mas agua que el borde
        incluso dentro de una misma zona."""
        nc, L = self.n_cells, float(self.params.bed_length)
        r = max(1e-6, float(self.params.spray_radius))
        A = [[0.0] * self.n_zones for _ in range(nc)]
        for k in range(self.n_zones):
            xk = self.x_centers[k]
            x0, x1 = max(0.0, xk - r), min(L, xk + r)
            col = [0.0] * nc
            if x1 > x0:
                m = max(self.SPRAY_SLICES, 8 * nc)
                dx = (x1 - x0) / m
                for si in range(m):
                    x = x0 + (si + 0.5) * dx
                    w = max(0.0, 1.0 - abs(x - xk) / r)
                    if w <= 0.0:
                        continue
                    c = min(nc - 1, max(0, int(x / self.cell_len)))
                    col[c] += w * dx
            tot = sum(col)
            if tot <= 0.0:
                col = [0.0] * nc
                col[self.celdas_de[k][self.sub // 2]] = 1.0
                tot = 1.0
            for c in range(nc):
                A[c][k] = col[c] / tot
        return A

    # ---- magnitudes derivadas -------------------------------------------------
    def masa_celda(self):
        return max(1e-9, self.params.soil_dry_mass_per_m * self.cell_len)

    def masa_zona(self):
        return self.masa_celda() * self.sub

    def _W_fc(self):
        """Capacidad de campo de UNA CELDA (unidad de estado del modelo)."""
        return self.params.field_capacity * self.masa_celda()

    def _W_sat(self):
        return max(self._W_fc() * 1.001,
                   self.params.saturation_capacity * self.masa_celda())

    def _ambient(self, t):
        p = self.params
        per = p.ambient_period if p.ambient_period > 0 else 86400.0
        return p.ambient_mean + p.ambient_amp * math.sin(2.0 * math.pi * t / per)

    def _actualizar_cadencia(self):
        """Cuantos pasos dt abarca UNA muestra.

        El muestreo solo puede ocurrir en instantes multiplos de dt, asi que un
        Ts que no sea multiplo se redondea al numero entero de pasos mas
        cercano. Se guarda el Ts EFECTIVO resultante, que es el que usan la
        tendencia, el control y el pre-entrenamiento: usar el Ts nominal cuando
        difiere del real descuadra la cadencia (era el punto delicado del
        modelo original, que derivaba la cadencia de round(Ts/dt))."""
        p = self.params
        dt = max(1e-9, float(p.dt))
        self.pasos_por_muestra = max(1, int(round(float(p.sample_time) / dt)))
        self.sample_time_eff = self.pasos_por_muestra * dt
        return self.pasos_por_muestra

    def diagnostico_cadencia(self):
        """Informe de cadencia y estabilidad numerica: lo que hay que mirar
        antes de fiarse de un entrenamiento."""
        p = self.params
        dt = float(p.dt)
        self._actualizar_cadencia()
        M = self.sub
        # Mayor coeficiente de decaimiento explicito: si k*dt se acerca a 1 el
        # Euler oscila, y por encima de 2 diverge.
        k_max = max(min(p.lateral_coeff * M, 0.35 / dt) + min(p.runoff_coeff * M, 0.35 / dt),
                    min(p.diffusion_coeff * M * M, 0.35 / dt) * 2.0,
                    p.drainage_coeff)
        cfl = k_max * dt
        # Cuanto sube la humedad de una celda en un solo paso con el riego abierto
        W_fc = self._W_fc()
        salto = (p.inflow_rate * dt / W_fc) if W_fc > 0 else 0.0
        avisos = []
        if abs(self.sample_time_eff - p.sample_time) > 1e-9:
            avisos.append(
                f"Ts={p.sample_time:g} s no es multiplo de dt={dt:g} s: se muestrea "
                f"cada {self.pasos_por_muestra} pasos, o sea cada "
                f"{self.sample_time_eff:g} s reales.")
        if self.pasos_por_muestra == 1 and p.sample_time < dt:
            avisos.append(
                f"Ts pedido ({p.sample_time:g} s) es menor que dt ({dt:g} s): no se "
                f"puede muestrear mas rapido que el paso de integracion.")
        if cfl > 0.5:
            avisos.append(
                f"dt es grande para la dinamica (k*dt={cfl:.2f}): el integrador "
                f"puede oscilar. Baja dt o los coeficientes de transporte.")
        if salto > 0.15:
            avisos.append(
                f"Con el riego abierto la humedad de una celda sube {salto*100:.0f}% "
                f"por paso: el control apenas puede dosificar. Baja dt o el caudal.")
        if self.pasos_por_muestra > 60:
            avisos.append(
                f"Cada decision abarca {self.pasos_por_muestra} pasos: el control "
                f"actua muy pocas veces por hora y le costara seguir la banda.")
        return {
            "dt": dt,
            "sample_time": float(p.sample_time),
            "sample_time_eff": self.sample_time_eff,
            "pasos_por_muestra": self.pasos_por_muestra,
            "muestras_por_hora": 3600.0 / max(1e-9, self.sample_time_eff),
            "k_dt": cfl,
            "salto_riego": salto,
            "avisos": avisos,
        }

    def _factor_pendiente(self):
        """Escala del transporte longitudinal segun la pendiente real. Con
        pendiente 0 no hay componente de bajada y las zonas se desacoplan."""
        p = self.params
        ref = p.slope_ref if p.slope_ref > 0 else 2.0
        return max(0.0, p.bed_slope / ref)

    # ---- ciclo de vida --------------------------------------------------------
    def reset(self):
        p = self.params
        self._construir_geometria()
        nc = self.n_cells
        W_fc, W_sat = self._W_fc(), self._W_sat()
        self.t = 0.0
        self.W, self.T = [], []
        for c in range(nc):
            frac = (c / (nc - 1)) if nc > 1 else 0.0
            m0 = p.init_moisture + p.init_moisture_grad * (frac - 0.5)
            self.W.append(min(W_sat, max(0.0, m0 * W_fc)))
            self.T.append(float(p.init_temperature))
        self.levels = [0.0] * self.n_zones   # apertura de cada aspersor [0,1]
        self.flux_spray = [0.0] * nc         # agua recibida por celda (kg/s)
        self.flux_lat = [0.0] * nc           # caudal que sale de cada celda hacia la de abajo
        self.flux_evap = [0.0] * nc
        self.flux_drain = [0.0] * nc
        self.flux_diff = [0.0] * nc
        self._prev_moisture = [0.0] * self.n_zones
        for i in range(self.n_zones):
            self._prev_moisture[i] = sum(self.W[c] for c in self.celdas_de[i]) / (
                self.sub * W_fc)
        self._trend = [0.0] * self.n_zones   # d(moisture medio)/dt por muestra (1/s)
        # Muestreo / historiales
        self._actualizar_cadencia()
        self._pasos_desde_muestra = 0
        self._sample_count = 0
        self.history = []
        self._pending = []
        self.dense = []
        self._dense_pending = []
        self._steps = 0
        self.leachate = 0.0                  # kg acumulados que salen de la cama

    # ---- integracion ----------------------------------------------------------
    def step(self, levels=None):
        """Avanza un paso dt integrando TODAS las celdas.

        El transporte se calcula celda a celda, asi que el agua tarda en
        recorrer una zona: aparece un frente de humedad que avanza y la lectura
        de la zona (promedio de sus celdas) responde de forma gradual, no
        instantanea."""
        p = self.params
        nc = self.n_cells
        if levels is not None:
            self.set_levels(levels)

        dt = p.dt
        W_fc, W_sat = self._W_fc(), self._W_sat()
        c_w = p.water_specific_heat
        M = self.sub
        pend = self._factor_pendiente()
        # --- Coeficientes CONSISTENTES CON LA MALLA -------------------------
        # El caudal que cruza una cara es proporcional al agua por unidad de
        # longitud, luego al refinar la malla (celdas M veces mas cortas) el
        # coeficiente por celda debe crecer M veces para que el transporte
        # macroscopico no dependa del numero de sub-zonas. La difusion es un
        # termino de segundo orden y escala con M^2.
        # El tope 'seguro' evita que una malla muy fina desestabilice el Euler.
        seguro = 0.35 / max(1e-9, dt)
        k_lat = min(p.lateral_coeff * M, seguro)
        k_run = min(p.runoff_coeff * M, seguro)
        k_dif = min(p.diffusion_coeff * M * M, seguro)
        k_drain = min(p.drainage_coeff, seguro)      # perdida vertical: local
        expo = p.lateral_exponent
        G = p.thermal_conductance * self.cell_len
        # La conduccion entre celdas tambien escala con 1/dx.
        k_cond = min(p.lateral_conductance * M, 0.35 * (self.masa_celda() *
                     p.soil_specific_heat) / max(1e-9, dt))
        Lv = p.latent_heat
        amb = self._ambient(self.t)
        masa = self.masa_celda()
        Q = p.inflow_rate

        # --- Riego repartido geometricamente entre celdas ---
        spray = [0.0] * nc
        activos = [(k, self.levels[k]) for k in range(self.n_zones) if self.levels[k] > 0.0]
        if activos:
            for c in range(nc):
                fila = self.spray[c]
                spray[c] = sum(fila[k] * Q * u for k, u in activos)

        theta = [w / W_fc if W_fc > 0 else 0.0 for w in self.W]
        evap = [0.0] * nc
        drain = [0.0] * nc
        lat = [0.0] * nc
        dif = [0.0] * nc
        for c in range(nc):
            temp_factor = math.exp(p.evap_temp_sensitivity * (self.T[c] - p.evap_temp_ref))
            avail = min(1.0, max(0.0, theta[c]))
            evap[c] = p.evap_base_rate * self.cell_len * temp_factor * avail
            exceso = max(0.0, self.W[c] - W_fc)
            drain[c] = k_drain * exceso
            lat[c] = pend * (k_lat * W_fc * max(0.0, theta[c]) ** expo + k_run * exceso)
            vec = 0.0
            if c > 0:
                vec += theta[c - 1] - theta[c]
            if c < nc - 1:
                vec += theta[c + 1] - theta[c]
            dif[c] = k_dif * vec * W_fc

        # --- Actualizacion (Euler explicito) ---
        newW, newT = [0.0] * nc, [0.0] * nc
        for c in range(nc):
            entra = lat[c - 1] if c > 0 else 0.0
            dWdt = spray[c] + entra + dif[c] - evap[c] - lat[c] - drain[c]
            C = masa * p.soil_specific_heat + self.W[c] * c_w
            q_amb = G * (amb - self.T[c])
            q_water = spray[c] * c_w * (p.water_temperature - self.T[c])
            q_lat = entra * c_w * (self.T[c - 1] - self.T[c]) if c > 0 else 0.0
            q_evap = -evap[c] * Lv
            q_cond = 0.0
            if c > 0:
                q_cond += k_cond * (self.T[c - 1] - self.T[c])
            if c < nc - 1:
                q_cond += k_cond * (self.T[c + 1] - self.T[c])
            dTdt = (q_amb + q_water + q_lat + q_evap + q_cond) / max(1e-9, C)
            newW[c] = min(W_sat, max(0.0, self.W[c] + dWdt * dt))
            newT[c] = self.T[c] + dTdt * dt

        self.leachate += (lat[nc - 1] + sum(drain)) * dt
        self.W, self.T = newW, newT
        self.t += dt
        self._steps += 1
        self.flux_spray, self.flux_evap = spray, evap
        self.flux_drain, self.flux_diff, self.flux_lat = drain, dif, lat

        # --- Trayectoria densa (a dt) y muestreo (a Ts) ---
        moist = self.humedad_por_zona()
        rec = {"t": self.t, "time_hours": self.t / 3600.0,
               "moisture": moist, "temperature": self.temperatura_por_zona(),
               "levels": list(self.levels), "is_sample": False}
        self._pasos_desde_muestra += 1
        if self._pasos_desde_muestra >= self.pasos_por_muestra:
            self._pasos_desde_muestra = 0
            # La tendencia se divide entre el intervalo REAL entre muestras, no
            # entre el Ts pedido: si Ts no es multiplo de dt no coinciden, y una
            # pendiente mal escalada es una entrada mal escalada para el control.
            ts = max(1e-9, self.sample_time_eff)
            self._trend = [(moist[i] - self._prev_moisture[i]) / ts
                           for i in range(self.n_zones)]
            self._prev_moisture = list(moist)
            self._record_sample()
            rec["is_sample"] = True
        self.dense.append(rec)
        self._dense_pending.append(rec)
        if len(self.dense) > self.DENSE_CAP:
            del self.dense[:len(self.dense) - self.DENSE_CAP]
        return self.get_state()

    # ---- agregacion de celdas a zonas ----------------------------------------
    def humedad_por_celda(self):
        W_fc = self._W_fc()
        return [w / W_fc if W_fc > 0 else 0.0 for w in self.W]

    def humedad_por_zona(self):
        """Lo que 've' el control: el PROMEDIO de las celdas de cada zona."""
        W_fc = self._W_fc()
        return [sum(self.W[c] for c in celdas) / (self.sub * W_fc) if W_fc > 0 else 0.0
                for celdas in self.celdas_de]

    def temperatura_por_zona(self):
        return [sum(self.T[c] for c in celdas) / self.sub for celdas in self.celdas_de]

    def set_levels(self, levels):
        """Fija la apertura de todos los aspersores (lista de bool o float)."""
        n = self.n_zones
        vals = list(levels)[:n] + [0.0] * max(0, n - len(list(levels)))
        self.levels = [min(1.0, max(0.0, float(v))) for v in vals]

    def set_level(self, zone: int, value):
        """Fija la apertura de UN aspersor (control independiente por zona)."""
        if 0 <= zone < self.n_zones:
            self.levels[zone] = min(1.0, max(0.0, float(value)))
            return True
        return False

    # ---- muestreo e historial -------------------------------------------------
    def _record_sample(self):
        self._sample_count += 1
        snap = self.get_state(ligero=True)
        self.history.append(snap)
        self._pending.append(snap)
        if len(self.history) > self.HISTORY_CAP:
            del self.history[:len(self.history) - self.HISTORY_CAP]

    def pop_new_samples(self):
        out, self._pending = self._pending, []
        return out

    def pop_new_dense(self):
        out, self._dense_pending = self._dense_pending, []
        return out

    def get_history(self, limit=None):
        if limit is None or limit >= len(self.history):
            return list(self.history)
        return list(self.history[-int(limit):])

    def get_dense_history(self, limit=None):
        if limit is None or limit >= len(self.dense):
            return list(self.dense)
        return list(self.dense[-int(limit):])

    # ---- lectura de estado ----------------------------------------------------
    def zone_state(self, i: int, ligero: bool = False):
        """Estado COMPLETO de una zona: es la observacion que recibe su
        controlador. La humedad y la temperatura son el PROMEDIO de sus
        sub-zonas; ademas se publican el reparto interno (cabecera, pie,
        gradiente) y el contexto de las zonas vecinas, que es lo que permite al
        control anticipar el efecto de la cascada."""
        p = self.params
        n = self.n_zones
        W_fc, W_sat = self._W_fc(), self._W_sat()
        celdas = self.celdas_de[i]
        hum = [self.W[c] / W_fc if W_fc > 0 else 0.0 for c in celdas]
        moist = sum(hum) / len(hum)
        temps = [self.T[c] for c in celdas]
        primera, ultima = celdas[0], celdas[-1]
        up, dn = i - 1, i + 1
        agua_zona = sum(self.W[c] for c in celdas)

        def media_zona(j):
            if not (0 <= j < n):
                return None
            cs = self.celdas_de[j]
            return sum(self.W[c] for c in cs) / (len(cs) * W_fc) if W_fc > 0 else 0.0

        m_up, m_dn = media_zona(up), media_zona(dn)
        return {
            "zone": i,
            "n_zones": n,
            "subzonas": self.sub,
            "x_center": self.x_centers[i],
            "zone_length": self.zone_len,
            "cell_length": self.cell_len,
            "pos_norm": (i / (n - 1)) if n > 1 else 0.0,
            "t": self.t,
            "time_hours": self.t / 3600.0,
            # --- variables medidas de la zona (promedio de sus sub-zonas) ---
            "moisture": moist,
            "moisture_pct_sat": (100.0 * agua_zona / (W_sat * self.sub)) if W_sat > 0 else 0.0,
            "water_kg": agua_zona,
            "temperature": sum(temps) / len(temps),
            "trend": self._trend[i],                     # d(humedad media)/dt [1/s]
            "valve_open": bool(self.levels[i] > 0.5),
            "valve_level": self.levels[i],
            # --- reparto INTERNO de la zona (regimen transitorio) ---
            "moisture_head": hum[0],                     # celda de cabecera (mas alta)
            "moisture_tail": hum[-1],                    # celda de pie (mas baja)
            "moisture_min": min(hum),
            "moisture_max": max(hum),
            "moisture_grad": hum[0] - hum[-1],           # >0: mojado por arriba
            "homogeneidad": 1.0 - min(1.0, (max(hum) - min(hum)) / max(1e-9, moist)),
            # --- contexto de la cascada ---
            "moisture_up": m_up if m_up is not None else 0.0,
            "moisture_down": m_dn if m_dn is not None else 0.0,
            "temperature_up": self.T[self.celdas_de[up][-1]] if up >= 0 else temps[0],
            "valve_level_up": self.levels[up] if up >= 0 else 0.0,
            "has_up": up >= 0,
            "has_down": dn < n,
            # --- flujos del ultimo paso (sumados sobre las celdas) ---
            "flux_spray": sum(self.flux_spray[c] for c in celdas),
            "flux_lateral_in": self.flux_lat[primera - 1] if primera > 0 else 0.0,
            "flux_lateral_out": self.flux_lat[ultima],
            "flux_evaporation": sum(self.flux_evap[c] for c in celdas),
            "flux_drainage": sum(self.flux_drain[c] for c in celdas),
            "flux_diffusion": sum(self.flux_diff[c] for c in celdas),
            "inflow_rate": p.inflow_rate,
            "flux_ref": max(1e-12, p.evap_base_rate * self.zone_len),
            "spray_self": sum(self.spray[c][i] for c in celdas),
            # --- ambiente y reloj ---
            "ambient_temperature": self._ambient(self.t),
            "dt": p.dt,
            "sample_time": self.sample_time_eff,
            "pasos_por_muestra": self.pasos_por_muestra,
            "sample_index": self._sample_count,
            "steps": self._steps,
            # El perfil celda a celda solo se adjunta cuando hace falta (dibujo):
            # en el historial y en la telemetria seria mucho volumen inutil.
            **({} if ligero else {"cells": hum, "cells_temperature": temps}),
        }

    def get_state(self, ligero: bool = False):
        """Estado global + lista de estados por zona. Los campos de primer nivel
        replican los de la version de una zona (promedios) para que cualquier
        cliente antiguo siga funcionando."""
        n = self.n_zones
        zonas = [self.zone_state(i, ligero) for i in range(n)]
        moist = [z["moisture"] for z in zonas]
        return {
            "t": self.t,
            "time_hours": self.t / 3600.0,
            "n_zones": n,
            "subzonas": self.sub,
            "n_cells": self.n_cells,
            "bed_length": self.params.bed_length,
            "zone_length": self.zone_len,
            "cell_length": self.cell_len,
            "moisture": sum(moist) / n,
            "moisture_min": min(moist),
            "moisture_max": max(moist),
            "temperature": sum(self.T) / self.n_cells,
            "valve_open": any(l > 0.5 for l in self.levels),
            "levels": list(self.levels),
            "ambient_temperature": self._ambient(self.t),
            "water_kg": sum(self.W),
            "leachate_kg": self.leachate,
            "sample_time": self.sample_time_eff,
            "sample_time_nominal": self.params.sample_time,
            "pasos_por_muestra": self.pasos_por_muestra,
            "dt": self.params.dt,
            "samples": len(self.history),
            "sample_index": self._sample_count,
            "steps": self._steps,
            "zones": zonas,
        }

    # ---- parametros -----------------------------------------------------------
    def get_params(self):
        d = asdict(self.params)
        d["n_zones"] = self.n_zones
        d["zone_len_eff"] = self.zone_len
        d["n_cells"] = self.n_cells
        d["cell_len"] = self.cell_len
        d["sample_time_eff"] = self.sample_time_eff
        d["pasos_por_muestra"] = self.pasos_por_muestra
        return d

    def set_param(self, key, value):
        valid = {f.name for f in fields(self.params)}
        if key not in valid:
            return False
        setattr(self.params, key, float(value))
        # Coherencias.
        if self.params.saturation_capacity <= self.params.field_capacity:
            self.params.saturation_capacity = self.params.field_capacity * 1.05
        if key == "subzonas":
            self.params.subzonas = float(max(1, int(round(self.params.subzonas))))
        if key in ("dt", "sample_time"):
            self._actualizar_cadencia()
        if key in ("bed_length", "zone_length", "spray_radius", "subzonas"):
            nc_old, W_fc_old = self.n_cells, self._W_fc()
            self._construir_geometria()
            if self.n_cells != nc_old:
                self._remuestrear_celdas(nc_old, W_fc_old)
        W_sat = self._W_sat()
        self.W = [min(max(0.0, w), W_sat) for w in self.W]
        return True

    def _remuestrear_celdas(self, nc_old, W_fc_old):
        """Cambiar L, la separacion de aspersores o el numero de sub-zonas
        cambia la malla: se remuestrea el perfil actual sobre las celdas nuevas
        en vez de reiniciar la simulacion. Se conserva la HUMEDAD RELATIVA
        (W/W_fc), que es la variable fisica que ve el control; la masa de agua
        se recalcula con la masa de la celda nueva."""
        nc = self.n_cells
        theta_old = [self.W[c] / max(1e-9, W_fc_old) for c in range(nc_old)]
        T_old = list(self.T)

        def muestrear(src, c):
            # posicion relativa en la cama -> celda equivalente de la malla vieja
            j = min(nc_old - 1, max(0, int((c + 0.5) * nc_old / nc)))
            return src[j]

        W_fc = self._W_fc()
        self.W = [muestrear(theta_old, c) * W_fc for c in range(nc)]
        self.T = [muestrear(T_old, c) for c in range(nc)]
        self.levels = (list(self.levels) + [0.0] * self.n_zones)[:self.n_zones]
        for name in ("flux_spray", "flux_lat", "flux_evap", "flux_drain", "flux_diff"):
            setattr(self, name, [0.0] * nc)
        self._trend = [0.0] * self.n_zones
        self._prev_moisture = self.humedad_por_zona()

    @staticmethod
    def get_schema():
        return [dict(kind="param", **PARAM_INFO[k]) for k, *_ in
                [(e[0],) for e in PARAM_SCHEMA]]


# =============================================================================
# CONTROLADOR DE HISTERESIS (referencia y "vecinos" durante el entrenamiento)
# =============================================================================

class HisteresisZona:
    """Control por histeresis de una zona. Se usa en la GUI (modo Auto) y como
    controlador de las zonas vecinas mientras se entrena una zona concreta."""

    def __init__(self, low=0.60, high=0.70):
        self.low, self.high = low, high
        self.abierto = False

    def decidir(self, moisture):
        if moisture < self.low:
            self.abierto = True
        elif moisture > self.high:
            self.abierto = False
        return 1.0 if self.abierto else 0.0


# =============================================================================
# SERVICIO LOCAL (TCP loopback, JSON por linea)
# =============================================================================

class MultiZoneService:
    """
    Protocolo (un objeto JSON por linea). Todos los comandos aceptan 'zone'
    para actuar sobre una zona concreta; sin 'zone' actuan sobre el conjunto.

      {"cmd":"get"}                        -> estado global + "zones":[...]
      {"cmd":"get","zone":1}               -> estado de la zona 1 en primer nivel
      {"cmd":"zones"}                      -> geometria: N, longitudes, matriz de aspersion
      {"cmd":"set_valve","zone":1,"open":true}
      {"cmd":"set_valve","zone":1,"level":0.5}
      {"cmd":"set_valve","levels":[1,0,0]} -> todas de una vez
      {"cmd":"step","n":10,"levels":[...]} -> avanza el reloj (modo externo)
      {"cmd":"sample","since":123,"zone":1}
      {"cmd":"params"} / {"cmd":"set_param","key":..,"value":..}
      {"cmd":"history","limit":n} / {"cmd":"dense","limit":n} / {"cmd":"reset"}
      {"cmd":"band","zone":1}              -> banda objetivo publicada por la GUI

    'flags' es el diccionario compartido con la GUI (velocidad, marcha, modo de
    cada zona y bandas de histeresis por zona).

    NOTA: el servicio CONTIENE un hilo en vez de heredar de threading.Thread.
    Heredar es una trampa: Thread define atributos internos propios (_target,
    _args, _handle, _started...) y cualquier metodo de la subclase que coincida
    con uno de ellos queda tapado por el atributo de instancia. Con Python 3.13
    aparecio 'Thread._handle', que tapaba el metodo que atendia a los clientes y
    dejaba el servicio mudo (todas las ordenes daban 'timed out'). Componiendo,
    el problema no puede repetirse con futuras versiones.
    """

    def __init__(self, sim, lock, flags, host=HOST, port=PORT, log=None):
        self.sim, self.lock, self.flags = sim, lock, flags
        self.host, self.port = host, port
        self.log = log if log is not None else (lambda m: None)
        self._server = None
        self._running = threading.Event()
        self._hilo = None

    # ---- ciclo de vida --------------------------------------------------------
    def start(self):
        """Arranca el hilo del servicio (equivalente al de threading.Thread)."""
        if self._hilo is not None and self._hilo.is_alive():
            return self
        self._hilo = threading.Thread(target=self._servir, name="servicio-mz",
                                      daemon=True)
        self._hilo.start()
        return self

    def is_alive(self):
        return self._hilo is not None and self._hilo.is_alive()

    def join(self, timeout=None):
        if self._hilo is not None:
            self._hilo.join(timeout)

    def _servir(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server.bind((self.host, self.port))
        except OSError as e:
            self.log(f"[servicio] No se pudo abrir {self.host}:{self.port} -> {e}")
            return
        self._server.listen(8)
        self._server.settimeout(0.5)
        self._running.set()
        self.log(f"[servicio] Escuchando en {self.host}:{self.port}")
        while self._running.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._atender_cliente, args=(conn, addr),
                             name=f"cliente-{addr[1]}", daemon=True).start()
        try:
            self._server.close()
        except OSError:
            pass
        self.log("[servicio] Detenido.")

    def stop(self):
        self._running.clear()

    def _atender_cliente(self, conn, addr):
        peer = f"{addr[0]}:{addr[1]}"
        self.log(f"[servicio] Cliente conectado: {peer}")
        buf = b""
        try:
            with conn:
                conn.settimeout(1.0)
                while self._running.is_set():
                    try:
                        data = conn.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            respuesta = self._process(line)
                        except Exception as e:   # nunca se deja morir al cliente
                            respuesta = {"ok": False, "error": f"Error interno: {e}"}
                        try:
                            conn.sendall((json.dumps(respuesta) + "\n").encode("utf-8"))
                        except OSError:
                            return
        except Exception as e:
            self.log(f"[servicio] Fallo atendiendo a {peer}: {e}")
        finally:
            self.log(f"[servicio] Cliente desconectado: {peer}")

    # ---- comandos -------------------------------------------------------------
    def _meta(self):
        """Campos que la GUI publica en cada respuesta (bandas, marcha, etc.)."""
        return {
            "speed": self.flags.get("speed"),
            "running": self.flags.get("running"),
            "external_step_mode": self.flags.get("external_step_mode", False),
            "band_low": self.flags.get("band_low"),
            "band_high": self.flags.get("band_high"),
            "bands": self.flags.get("bands"),
            "zone_modes": self.flags.get("zone_modes"),
        }

    def _latido(self, z):
        """Anota que un controlador externo sigue vivo en esa zona. La interfaz
        lo usa para devolver la zona a modo automatico si el proceso muere sin
        avisar (por ejemplo si se cierra desde el editor): sin esto la valvula
        se quedaria congelada en su ultimo valor, quiza abierta."""
        latidos = self.flags.setdefault("latido_externo", {})
        ahora = time.time()
        if z is None:
            for i in range(self.sim.n_zones):
                latidos[i] = ahora
        else:
            latidos[z] = ahora

    @staticmethod
    def _zone_arg(msg):
        z = msg.get("zone")
        if z is None:
            return None
        try:
            return int(z)
        except (TypeError, ValueError):
            return None

    def _process(self, line: bytes):
        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"JSON invalido: {e}"}
        if not isinstance(msg, dict):
            return {"ok": False, "error": "Se esperaba un objeto JSON"}
        cmd = msg.get("cmd")
        z = self._zone_arg(msg)
        try:
            with self.lock:
                sim = self.sim
                if z is not None and not (0 <= z < sim.n_zones):
                    return {"ok": False, "error": f"zona {z} fuera de rango (0..{sim.n_zones-1})"}

                if cmd == "get":
                    base = sim.zone_state(z) if z is not None else sim.get_state()
                    return {"ok": True, **base, **self._meta(),
                            "n_zones": sim.n_zones}

                if cmd == "zones":
                    return {"ok": True, "n_zones": sim.n_zones,
                            "zone_length": sim.zone_len,
                            "bed_length": sim.params.bed_length,
                            "bed_slope": sim.params.bed_slope,
                            "spray_radius": sim.params.spray_radius,
                            "x_centers": list(sim.x_centers),
                            "spray_matrix": [list(r) for r in sim.spray],
                            **self._meta()}

                if cmd == "set_valve":
                    self._latido(z)
                    if "levels" in msg:
                        sim.set_levels([float(v) for v in msg["levels"]])
                    else:
                        lvl = msg.get("level")
                        if lvl is None:
                            lvl = 1.0 if bool(msg.get("open", False)) else 0.0
                        if z is None:
                            sim.set_levels([float(lvl)] * sim.n_zones)
                        else:
                            sim.set_level(z, float(lvl))
                    self.flags["external_step_mode"] = False
                    base = sim.zone_state(z) if z is not None else sim.get_state()
                    return {"ok": True, **base, **self._meta()}

                if cmd == "step":
                    try:
                        n = max(1, min(int(msg.get("n", 1)), 1_000_000))
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "n debe ser entero"}
                    if "levels" in msg:
                        sim.set_levels([float(v) for v in msg["levels"]])
                    elif z is not None and ("open" in msg or "level" in msg):
                        lvl = msg.get("level")
                        if lvl is None:
                            lvl = 1.0 if bool(msg.get("open")) else 0.0
                        sim.set_level(z, float(lvl))
                    self.flags["external_step_mode"] = True
                    for _ in range(n):
                        sim.step()
                    base = sim.zone_state(z) if z is not None else sim.get_state()
                    return {"ok": True, **base, **self._meta()}

                if cmd == "reset":
                    sim.reset()
                    return {"ok": True, **sim.get_state(), **self._meta()}

                if cmd == "cadencia":
                    return {"ok": True, **sim.diagnostico_cadencia()}

                if cmd == "params":
                    return {"ok": True, "params": sim.get_params()}

                if cmd == "set_param":
                    try:
                        ok = sim.set_param(msg.get("key"), msg.get("value"))
                    except (TypeError, ValueError):
                        return {"ok": False, "error": f"Valor invalido para {msg.get('key')}"}
                    if ok:
                        return {"ok": True, "params": sim.get_params()}
                    return {"ok": False, "error": f"Parametro desconocido: {msg.get('key')}"}

                if cmd == "schema":
                    return {"ok": True, "schema": sim.get_schema()}

                if cmd == "sample":
                    hist = sim.history
                    resp = {"ok": True, "sample_index": sim._sample_count,
                            "samples": len(hist), **self._meta()}
                    ultimo = hist[-1] if hist else None
                    resp["sample"] = (ultimo["zones"][z] if (ultimo and z is not None)
                                      else ultimo)
                    since = msg.get("since")
                    if since is not None:
                        try:
                            since = int(since)
                        except (TypeError, ValueError):
                            return {"ok": False, "error": "since debe ser entero"}
                        faltan = sim._sample_count - since
                        nuevas = sim.get_history(faltan) if faltan > 0 else []
                        if z is not None:
                            nuevas = [s["zones"][z] for s in nuevas]
                        resp["nuevas"] = nuevas
                    return resp

                if cmd == "history":
                    try:
                        limit = msg.get("limit")
                        limit = int(limit) if limit is not None else None
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "limit debe ser entero"}
                    h = sim.get_history(limit)
                    if z is not None:
                        h = [s["zones"][z] for s in h]
                    return {"ok": True, "history": h}

                if cmd == "dense":
                    try:
                        limit = msg.get("limit")
                        limit = int(limit) if limit is not None else None
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "limit debe ser entero"}
                    return {"ok": True, "dense": sim.get_dense_history(limit)}

                if cmd == "claim":
                    # Un controlador externo reclama una zona: la GUI deja de
                    # decidir por ella (modo 'externo') y le cede el actuador.
                    modos = self.flags.setdefault("zone_modes", ["auto"] * sim.n_zones)
                    while len(modos) < sim.n_zones:
                        modos.append("auto")
                    self._latido(z)
                    modo = str(msg.get("mode", "externo"))
                    if z is None:
                        for i in range(sim.n_zones):
                            modos[i] = modo
                    else:
                        modos[z] = modo
                    self.flags["zone_modes"] = modos
                    return {"ok": True, "zone_modes": modos}

                if cmd == "release":
                    latidos = self.flags.setdefault("latido_externo", {})
                    if z is None:
                        latidos.clear()
                    else:
                        latidos.pop(z, None)
                    modos = self.flags.setdefault("zone_modes", ["auto"] * sim.n_zones)
                    destino = str(msg.get("mode", "manual"))
                    if z is None:
                        modos = [destino] * sim.n_zones
                    elif z < len(modos):
                        modos[z] = destino
                    self.flags["zone_modes"] = modos
                    return {"ok": True, "zone_modes": modos}

                if cmd == "band":
                    bands = self.flags.get("bands") or []
                    if z is not None and z < len(bands):
                        return {"ok": True, "band_low": bands[z][0], "band_high": bands[z][1]}
                    return {"ok": True, **self._meta()}

                return {"ok": False, "error": f"Comando desconocido: {cmd!r}"}
        except Exception as e:  # el hilo del servicio nunca debe caerse
            return {"ok": False, "error": f"Error interno: {e}"}


def flags_por_defecto(n_zones, low=0.60, high=0.70):
    """Diccionario de banderas compartido GUI <-> servicio."""
    return {
        "speed": 1,
        "running": True,
        "external_step_mode": False,
        "band_low": low,
        "band_high": high,
        "bands": [[low, high] for _ in range(n_zones)],
        "zone_modes": ["auto"] * n_zones,
        "latido_externo": {},      # zona -> instante del ultimo mando externo
    }


# =============================================================================
# CLI (headless / servicio / autopruebas)
# =============================================================================

def run_headless(steps=2000, log=print):
    sim = MultiZoneBed()
    ctrl = [HisteresisZona() for _ in range(sim.n_zones)]
    for _ in range(int(steps)):
        st = sim.get_state()
        niveles = [c.decidir(z["moisture"]) for c, z in zip(ctrl, st["zones"])]
        sim.step(niveles)
    st = sim.get_state()
    log(f"t={st['time_hours']:.2f} h  zonas={st['n_zones']}  lixiviado={st['leachate_kg']:.2f} kg")
    for z in st["zones"]:
        log(f"  Z{z['zone']}  x={z['x_center']:.2f} m  hum={z['moisture']:.3f}  "
            f"T={z['temperature']:.2f} C  riego={z['valve_level']:.2f}  "
            f"lat_out={z['flux_lateral_out']*1000:.3f} g/s")
    return sim


def serve_only(host=HOST, port=PORT):
    import time
    sim = MultiZoneBed()
    lock = threading.Lock()
    flags = flags_por_defecto(sim.n_zones)
    svc = MultiZoneService(sim, lock, flags, host, port, log=print)
    svc.start()
    ctrl = [HisteresisZona() for _ in range(sim.n_zones)]
    print("Servicio multizona activo (Ctrl+C para salir). Reloj propio a tiempo real.")
    try:
        while True:
            time.sleep(0.05)
            with lock:
                if flags.get("running") and not flags.get("external_step_mode"):
                    for _ in range(int(flags.get("speed", 1))):
                        st = sim.get_state()
                        modos = flags.get("zone_modes") or []
                        for i, zs in enumerate(st["zones"]):
                            modo = modos[i] if i < len(modos) else "auto"
                            if modo == "auto":
                                lo, hi = flags["bands"][i]
                                ctrl[i].low, ctrl[i].high = lo, hi
                                sim.set_level(i, ctrl[i].decidir(zs["moisture"]))
                        sim.step()
    except KeyboardInterrupt:
        svc.stop()
        print("\nServicio detenido.")


def selftest():
    fallos = []

    def check(cond, msg):
        if not cond:
            fallos.append(msg)

    sim = MultiZoneBed()
    check(sim.n_zones == 3, f"L=6, zona=2 debe dar 3 zonas (dio {sim.n_zones})")
    col = [sum(sim.spray[c][1] for c in sim.celdas_de[i]) for i in range(3)]
    check(abs(sum(col) - 1.0) < 1e-9,
          "las columnas de la matriz de aspersion deben sumar 1")
    check(col[1] > col[0] > 0.0,
          "el aspersor central debe mojar mas su zona que la vecina")

    # Cascada: regar solo el aspersor 0 debe humedecer tambien a las de abajo,
    # que no reciben riego directo (la zona 2 no recibe ni una gota del A0).
    sim.reset()
    inicial = sim.get_state()["zones"][2]["moisture"]
    for _ in range(4000):
        sim.step([1.0, 0.0, 0.0])
    st = sim.get_state()
    m = [z["moisture"] for z in st["zones"]]
    check(sum(sim.spray[c][0] for c in sim.celdas_de[2]) < 1e-9,
          "el aspersor 0 no debe alcanzar la zona 2")
    check(m[2] > inicial + 0.2,
          f"la zona 2 solo puede mojarse por la cascada (paso de {inicial:.2f} a {m[2]:.2f})")
    check(m[2] < m[0] and m[2] < m[1],
          f"el pie de la cama debe quedar mas seco que la cabecera (dio {m})")
    check(st["zones"][1]["flux_lateral_in"] > 0, "la zona 1 debe recibir escorrentia")

    # Sin pendiente, difusion, conduccion lateral ni solape de aspersion, las
    # zonas quedan TOTALMENTE desacopladas: el modelo se reduce a N copias del
    # simulador de una sola zona.
    p = BedParams(bed_slope=0.0, diffusion_coeff=0.0, spray_radius=0.5,
                  lateral_conductance=0.0)
    s2 = MultiZoneBed(p)
    for _ in range(2000):
        s2.step([1.0, 0.0, 0.0])
    z = s2.get_state()["zones"]
    check(abs(z[1]["moisture"] - z[2]["moisture"]) < 1e-6,
          "sin pendiente ni difusion las zonas 1 y 2 deben evolucionar igual")

    # Cascada EN REGIMEN NORMAL (por debajo de capacidad de campo): con las tres
    # zonas en histeresis, las de abajo deben necesitar menos riego.
    s5 = MultiZoneBed()
    c5 = [HisteresisZona() for _ in range(s5.n_zones)]
    riego = [0.0] * s5.n_zones
    for _ in range(20000):
        st = s5.get_state()
        niveles = [c.decidir(zz["moisture"]) for c, zz in zip(c5, st["zones"])]
        for i, v in enumerate(niveles):
            riego[i] += v
        s5.step(niveles)
    check(riego[0] > riego[1] and riego[0] > riego[2],
          f"la cabecera debe regar mas que las zonas de abajo (dio {riego})")
    check(s5.get_state()["zones"][2]["flux_lateral_in"] > 0.0,
          "debe haber flujo lateral tambien por debajo de capacidad de campo")

    # Sin pendiente no hay transporte longitudinal.
    s6 = MultiZoneBed(BedParams(bed_slope=0.0))
    for _ in range(500):
        s6.step([1.0, 0.0, 0.0])
    check(s6.get_state()["zones"][1]["flux_lateral_in"] == 0.0,
          "con pendiente 0 no debe haber flujo lateral")

    # Cambiar L reconstruye zonas conservando el perfil.
    s3 = MultiZoneBed()
    for _ in range(600):
        s3.step([1.0, 0.0, 0.0])
    hum_antes = s3.get_state()["zones"][0]["moisture"]
    s3.set_param("bed_length", 10.0)
    check(s3.n_zones == 5, f"L=10 debe dar 5 zonas (dio {s3.n_zones})")
    check(abs(s3.get_state()["zones"][0]["moisture"] - hum_antes) < 0.05,
          "al cambiar L el perfil de humedad debe conservarse aproximadamente")
    s3.step()  # debe seguir integrando sin errores tras la reparticion

    # Muestreo.
    s4 = MultiZoneBed()
    for _ in range(60):
        s4.step([0.0] * s4.n_zones)
    check(s4.get_state()["sample_index"] == 10,
          f"dt=10 s y Ts=60 s -> 10 muestras en 600 s (dio {s4.get_state()['sample_index']})")

    # SUB-ZONAS: la malla no debe alterar lo que entra ni lo que se conserva,
    # pero si debe hacer transitoria la respuesta dentro de cada zona.
    for M in (1, 2, 4, 8):
        b = MultiZoneBed(BedParams(subzonas=M))
        check(b.n_cells == 3 * M, f"con {M} sub-zonas deben salir {3*M} celdas")
        reparto = [sum(b.spray[c][0] for c in b.celdas_de[i]) for i in range(3)]
        check(abs(reparto[0] - 0.8579) < 1e-3 and abs(reparto[1] - 0.1421) < 1e-3,
              f"el riego que recibe cada ZONA no debe depender de la malla (M={M}: {reparto})")

    b = MultiZoneBed(BedParams(subzonas=6))
    agua0, entra, sale = sum(b.W), 0.0, 0.0
    for k in range(1500):
        b.step([1.0, 0.0, 1.0] if k < 500 else [0.0, 0.0, 0.0])
        entra += sum(b.flux_spray) * b.params.dt
        sale += (sum(b.flux_evap) + sum(b.flux_drain) + b.flux_lat[-1]) * b.params.dt
    check(abs(sum(b.W) - (agua0 + entra - sale)) < 1e-6,
          "el balance de masa debe cerrar con la malla fina")

    b = MultiZoneBed(BedParams(subzonas=8, init_moisture=0.35))
    for _ in range(150):
        b.step([1.0, 0.0, 0.0])
    z0 = b.zone_state(0)
    check(max(z0["cells"]) - min(z0["cells"]) > 0.01,
          "regando solo el aspersor 0 la zona 0 debe quedar NO uniforme (transitorio)")
    check(abs(z0["moisture"] - sum(z0["cells"]) / len(z0["cells"])) < 1e-9,
          "la humedad de la zona debe ser el promedio de sus sub-zonas")
    b2 = MultiZoneBed(BedParams(subzonas=1, init_moisture=0.35))
    for _ in range(150):
        b2.step([1.0, 0.0, 0.0])
    check(len(b2.zone_state(0)["cells"]) == 1 and
          b2.zone_state(0)["moisture_grad"] == 0.0,
          "con 1 sub-zona la zona vuelve a ser un tanque de mezcla perfecta")

    # Cambiar la malla en marcha conserva el perfil.
    b3 = MultiZoneBed(BedParams(subzonas=2))
    for _ in range(400):
        b3.step([1.0, 0.0, 0.0])
    antes = b3.zone_state(0)["moisture"]
    b3.set_param("subzonas", 8)
    check(b3.n_cells == 24, f"tras cambiar a 8 sub-zonas deben existir 24 celdas")
    check(abs(b3.zone_state(0)["moisture"] - antes) < 0.02,
          "al refinar la malla en marcha la humedad de la zona debe conservarse")
    b3.step()

    # El SERVICIO debe responder de verdad por el socket. Esta prueba es la que
    # detecta fallos como el de Python 3.13 (un metodo tapado por un atributo
    # interno de Thread dejaba el servicio mudo y todo daba 'timed out').
    import json as _json
    import time as _time
    sim_svc = MultiZoneBed()
    lock = threading.Lock()
    flags = flags_por_defecto(sim_svc.n_zones)
    puerto = 8791
    svc = MultiZoneService(sim_svc, lock, flags, port=puerto)
    svc.start()
    _time.sleep(0.4)
    try:
        cli = socket.create_connection(("127.0.0.1", puerto), timeout=3.0)
        with cli:
            def pedir(msg):
                cli.sendall((_json.dumps(msg) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    trozo = cli.recv(8192)
                    if not trozo:
                        raise ConnectionError("el servicio cerro la conexion")
                    buf += trozo
                return _json.loads(buf.split(b"\n", 1)[0].decode())

            r = pedir({"cmd": "zones"})
            check(r.get("ok") and r.get("n_zones") == 3,
                  f"el servicio debe responder 'zones' (dio {r})")
            r = pedir({"cmd": "claim", "zone": 1, "mode": "externo"})
            check(r.get("zone_modes", [None, None])[1] == "externo",
                  "el comando 'claim' debe marcar la zona como externa")
            r = pedir({"cmd": "set_valve", "zone": 1, "level": 1.0})
            check(abs(r.get("valve_level", 0) - 1.0) < 1e-9,
                  "el comando 'set_valve' debe abrir el actuador de la zona")
            r = pedir({"cmd": "step", "n": 5})
            check(r.get("steps") == 5, f"'step' debe avanzar 5 pasos (dio {r.get('steps')})")
            r = pedir({"cmd": "nada"})
            check(r.get("ok") is False, "un comando desconocido debe devolver error")
    except Exception as e:
        fallos.append(f"el servicio local no respondio: {e!r}")
    finally:
        svc.stop()

    if fallos:
        print("AUTOPRUEBAS: FALLOS")
        for f in fallos:
            print("  -", f)
        return 1
    print("AUTOPRUEBAS: todo correcto.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nucleo multizona de la cama de vermicompostaje.")
    ap.add_argument("--headless", type=int, metavar="PASOS",
                    help="Simula N pasos con histeresis en todas las zonas y resume.")
    ap.add_argument("--serve", action="store_true", help="Arranca solo el servicio local.")
    ap.add_argument("--test", action="store_true", help="Ejecuta las autopruebas.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    if args.test:
        return selftest()
    if args.headless:
        run_headless(args.headless)
        return 0
    if args.serve:
        serve_only(args.host, args.port)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
