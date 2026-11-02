#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardware_mz.py  -  CONEXION REAL: SENSORES DE SUELO + ELECTROVALVULAS
=====================================================================

Une el lecho real con el modelo: lee los sensores SN-3002-TR por RS-485 (Modbus
RTU a traves de un adaptador USB), manda las electrovalvulas a traves de un
ESP32 en el mismo bus, y CORRIGE la simulacion con lo que miden los sensores,
de modo que el modelo deje de ser una fantasia y sea un estimador del lecho
real.

                    ┌── sensor zona 0 (dir. 1) ──┐
   PC ── USB/RS485 ──┼── sensor zona 1 (dir. 2) ──┼── par trenzado + 120 Ω
                    ├── sensor zona 2 (dir. 3) ──┤
                    └── ESP32 valvulas (dir. 10) ┘

QUE HACE CADA PIEZA
-------------------
  MapaCampo      : que direccion Modbus corresponde a cada zona, y la
                   calibracion de cada sensor. Se guarda en 'campo.json' y se
                   edita desde la pestana Hardware de la interfaz.
  SensorSuelo    : driver del SN-3002-TR (escalado, signo, validacion).
  ActuadorESP32  : las N electrovalvulas como bobinas Modbus del ESP32.
  BusCampo       : dueno del puerto. Ciclo de sondeo, escaneo de direcciones,
                   cambio de direccion/velocidad de un sensor, estadisticas.
  Asimilador     : mete las medidas en el modelo multizona.
  LazoCampo      : hilo propio que sondea a Ts, asimila y aplica las valvulas.

CONVERSION DE UNIDADES (el punto delicado)
------------------------------------------
El sensor da HUMEDAD VOLUMETRICA en % (0-100). El modelo trabaja con humedad
RELATIVA a la capacidad de campo (W/W_fc), donde 1,0 = capacidad de campo. La
conversion necesita saber a que % volumetrico esta la capacidad de campo de TU
sustrato:

    humedad_modelo = (VWC_medido + offset) / VWC_en_capacidad_de_campo

Para un vermicompost suele estar entre el 30 % y el 45 % VWC. Es el parametro
que hay que calibrar de verdad: si esta mal, la banda objetivo del control no
corresponde a la humedad real del lecho. Procedimiento en el README.

AVISOS DEL FABRICANTE QUE EL CODIGO RESPETA
-------------------------------------------
  * La conductividad solo es fiable con humedad volumetrica por encima del 20 %.
  * El tiempo de estabilizacion del sensor es de hasta 5 min tras enterrarlo.
  * A/B no se pueden invertir; 120 Ω de terminacion en los extremos del bus.
  * Todos los dispositivos del bus deben compartir velocidad y direccion unica.
"""

import json
import os
import struct
import threading
import time
from dataclasses import dataclass, field, asdict

from modbus_rtu import (MaestroModbus, TransporteSerie, TransporteSimulado,
                        EsclavoSimulado, ErrorModbus, SinRespuesta,
                        ExcepcionModbus, hexa)

RUTA_CONFIG = "campo.json"

# --- Registros del SN-3002-TR (direcciones base 0, funcion 0x03) -------------
REG_HUMEDAD = 0x0000        # % volumetrico x10
REG_TEMPERATURA = 0x0001    # C x10, con signo (complemento a dos)
REG_CONDUCTIVIDAD = 0x0002  # uS/cm
REG_PH = 0x0003             # pH x10
REG_NITROGENO = 0x0004      # mg/kg
REG_FOSFORO = 0x0005
REG_POTASIO = 0x0006
REG_SALINIDAD = 0x0007
REG_TDS = 0x0008
REG_FERTILIDAD = 0x000C
REG_CAL_TEMPERATURA = 0x0050
REG_CAL_HUMEDAD = 0x0051
REG_CAL_CONDUCTIVIDAD = 0x0052
REG_CAL_PH = 0x0053
# --- Registros de estado del ESP32 de valvulas (ver esp32_valvulas.ino) ------
REG_ESP_MAPA = 0x0000       # estado de las valvulas como mapa de bits
REG_ESP_UPTIME = 0x0001     # s desde el arranque
REG_ESP_TRAMAS = 0x0002     # tramas validas recibidas
REG_ESP_DESCARTADAS = 0x0003  # tramas descartadas (CRC malo o direccion ajena)
REG_ESP_SIN_ORDENES = 0x0004  # s desde la ultima orden valida
REG_ESP_GUARDA = 0x0005     # tiempo de guarda en s
REG_ESP_VERSION = 0x0006    # version del firmware
REG_ESP_N_VALVULAS = 0x0007  # cuantas bobinas expone

REG_DIRECCION = 0x07D0      # 1..254
REG_BAUDIOS = 0x07D1        # 0=2400, 1=4800, 2=9600

BAUDIOS_CODIGO = {2400: 0, 4800: 1, 9600: 2}
CODIGO_BAUDIOS = {v: k for k, v in BAUDIOS_CODIGO.items()}

VWC_MINIMO_EC = 20.0        # por debajo, la conductividad no es fiable
# Una sonda al aire (o mal enterrada) da humedad ~0, conductividad 0 y el pH
# pegado al minimo del rango. NO es "suelo muy seco": es que no hay contacto.
# Hay que detectarlo porque si esa lectura entra en el modelo, el control cree
# que el lecho esta reseco y abre el riego indefinidamente.
VWC_SIN_CONTACTO = 1.0      # % VWC por debajo del cual se sospecha del contacto
LIMITES = {"vwc": (0.0, 100.0), "temperatura": (-40.0, 80.0),
           "conductividad": (0.0, 20000.0), "ph": (3.0, 9.0)}


# =============================================================================
# CONFIGURACION (el "papel" donde se indican las direcciones)
# =============================================================================

@dataclass
class ConfigSensor:
    """Un sensor de suelo asignado a una zona."""
    zona: int = 0
    direccion: int = 1               # direccion Modbus ya grabada en el sensor
    etiqueta: str = ""
    activo: bool = True
    # --- calibracion ---
    vwc_capacidad_campo: float = 35.0   # % VWC que equivale a W/W_fc = 1,0
    offset_humedad: float = 0.0          # % VWC a sumar (correccion por software)
    offset_temperatura: float = 0.0      # C a sumar
    leer_npk: bool = False               # segunda trama con N, P, K, salinidad, TDS
    # El SN-3002 se vende en variantes (ver 1.5 del datasheet): NPKPH, THNPKPH,
    # ECNPKPH, ECTHNPKPH... No todas traen conductividad. Si la tuya no la trae,
    # el registro 0x0002 vale 0 SIEMPRE, y usar ese 0 como prueba de que la sonda
    # esta al aire seria un error que bloquearia las medidas justo cuando el
    # lecho esta seco de verdad.
    tiene_ec: bool = True
    # 'bloque'     : los 4 parametros en UNA trama (lo que hace el ejemplo del
    #                datasheet). Menos trafico.
    # 'individual' : un registro por trama, como el script de referencia que si
    #                funciona en este montaje. Hay unidades de SN-3002 que
    #                devuelven 0 en algunos canales al leerlos en bloque pero
    #                responden bien de uno en uno.
    # 'auto'       : en la primera lectura hace las dos y compara; si difieren,
    #                se queda con la individual para siempre y lo avisa.
    modo_lectura: str = "auto"


@dataclass
class ConfigActuador:
    """El ESP32 que conmuta las electrovalvulas.

    Puede compartir el bus RS-485 con los sensores o tener SU PROPIO PUERTO
    (otro adaptador USB-RS485, o el USB del propio ESP32). Tenerlo aparte es
    mas robusto: el trafico de los sensores no interfiere con el mando de las
    valvulas, cada bus puede ir a su velocidad, y un fallo en el bus de sensores
    no impide cerrar el riego. Deja 'puerto' vacio para compartir el bus.
    """
    puerto: str = ""                 # vacio = mismo bus que los sensores
    baudios: int = 4800
    direccion: int = 10
    bobina_base: int = 0             # bobina de la zona 0; la zona i usa base+i
    invertido: bool = False          # True si el rele es activo a nivel bajo
    activo: bool = True
    verificar: bool = True           # releer las bobinas para confirmar el mando


@dataclass
class ConfigCampo:
    puerto: str = ""                 # "COM5", "/dev/ttyUSB0" o "simulado"
    baudios: int = 4800
    timeout: float = 1.0             # como el script de referencia
    reintentos: int = 2
    pausa_entre_sensores: float = 0.05   # respiro para el bus entre esclavos
    tiempo_muestreo: float = 60.0    # s reales entre ciclos de sondeo (Ts)
    modo: str = "nudging"            # 'nudging' | 'medida' | 'solo_lectura'
    ganancia: float = 0.35           # peso de la medida en modo 'nudging'
    ganancia_temperatura: float = 0.5
    autocalibrar_evaporacion: bool = True
    bloquear_sin_contacto: bool = True   # no asimilar lecturas sin contacto
    sensores: list = field(default_factory=list)
    actuador: ConfigActuador = field(default_factory=ConfigActuador)

    # ---- persistencia --------------------------------------------------------
    def guardar(self, ruta=RUTA_CONFIG):
        d = asdict(self)
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ruta)
        return ruta

    @classmethod
    def cargar(cls, ruta=RUTA_CONFIG):
        if not os.path.exists(ruta):
            return None
        with open(ruta, "r", encoding="utf-8") as f:
            d = json.load(f)
        cfg = cls(**{k: v for k, v in d.items()
                     if k not in ("sensores", "actuador")})
        cfg.sensores = [ConfigSensor(**s) for s in d.get("sensores", [])]
        cfg.actuador = ConfigActuador(**d.get("actuador", {}))
        return cfg

    @classmethod
    def por_defecto(cls, n_zonas=3, primera_direccion=1, direccion_esp32=10):
        """Mapa inicial razonable: sensores en 1..N y el ESP32 en 10."""
        cfg = cls()
        cfg.sensores = [ConfigSensor(zona=i, direccion=primera_direccion + i,
                                     etiqueta=f"Zona {i}")
                        for i in range(n_zonas)]
        cfg.actuador = ConfigActuador(direccion=direccion_esp32)
        return cfg

    def sensor_de(self, zona):
        for s in self.sensores:
            if s.zona == zona:
                return s
        return None

    @property
    def actuador_aparte(self):
        """True si el ESP32 tiene su propio puerto, distinto del de los sensores."""
        pa = (self.actuador.puerto or "").strip()
        return bool(pa) and pa != self.puerto

    def comprobar(self, n_zonas=None):
        """Errores de configuracion que rompen el bus si no se detectan antes."""
        problemas = []
        vistas = {}
        for s in self.sensores:
            if not 1 <= s.direccion <= 254:
                problemas.append(f"zona {s.zona}: la direccion {s.direccion} esta "
                                 f"fuera de 1..254")
            if s.direccion in vistas:
                problemas.append(f"direccion {s.direccion} repetida en las zonas "
                                 f"{vistas[s.direccion]} y {s.zona}: en Modbus cada "
                                 f"esclavo debe ser unico")
            vistas[s.direccion] = s.zona
            if s.vwc_capacidad_campo <= 1.0:
                problemas.append(f"zona {s.zona}: la humedad volumetrica en "
                                 f"capacidad de campo ({s.vwc_capacidad_campo}) no "
                                 f"es creible; deberia rondar 30-45 %")
        if self.actuador.bobina_base != 0:
            problemas.append(
                f"la bobina de la zona 0 esta en {self.actuador.bobina_base}: el "
                f"firmware de ejemplo numera las valvulas desde 0, asi que con "
                f"{self.actuador.bobina_base} pedira bobinas que no existen y el "
                f"ESP32 rechazara el mando. Ponla a 0 salvo que hayas cambiado el "
                f"firmware.")
        if (self.actuador.activo and not self.actuador_aparte
                and self.actuador.direccion in vistas):
            problemas.append(f"el ESP32 tiene la direccion {self.actuador.direccion}, "
                             f"la misma que el sensor de la zona "
                             f"{vistas[self.actuador.direccion]} y comparten bus")
        if n_zonas is not None:
            faltan = [i for i in range(n_zonas) if self.sensor_de(i) is None]
            if faltan:
                problemas.append(f"no hay sensor asignado a las zonas {faltan}: esas "
                                 f"zonas seguiran con el valor simulado")
        return problemas


# =============================================================================
# DRIVER DEL SENSOR
# =============================================================================

def _con_signo(valor):
    return struct.unpack(">h", struct.pack(">H", valor & 0xFFFF))[0]


class SensorSuelo:
    """SN-3002-TR-*-N01 (humedad, temperatura, conductividad, pH [, NPK])."""

    def __init__(self, cfg: ConfigSensor):
        self.cfg = cfg
        self.modo_efectivo = None if cfg.modo_lectura == "auto" else cfg.modo_lectura
        self.aviso_modo = ""
        self.ultima = None
        self.fallos_seguidos = 0
        self.lecturas_ok = 0
        self.lecturas_error = 0
        self.ultimo_error = ""

    # ---- lectura -------------------------------------------------------------
    def _leer_bloque(self, maestro):
        """Los cuatro parametros en una sola trama (ejemplo del datasheet)."""
        return maestro.leer_registros(self.cfg.direccion, REG_HUMEDAD, 4)

    def _leer_individual(self, maestro, pausa=0.0):
        """Un registro por trama, como el script de referencia. Mas lento pero
        es lo que algunas unidades necesitan para devolver todos los canales."""
        vals = []
        for reg in (REG_HUMEDAD, REG_TEMPERATURA, REG_CONDUCTIVIDAD, REG_PH):
            vals.append(maestro.leer_registros(self.cfg.direccion, reg, 1)[0])
            if pausa:
                time.sleep(pausa)
        return vals

    def _elegir_modo(self, maestro):
        """Compara las dos formas de leer y se queda con la que da mas canales.

        No es una preferencia estetica: hay unidades que en bloque devuelven 0
        en humedad o conductividad y de una en una responden bien. En vez de
        adivinar cual es la tuya, se miden las dos y se decide con datos.
        """
        try:
            bloque = self._leer_bloque(maestro)
            individual = self._leer_individual(maestro)
        except ErrorModbus:
            self.modo_efectivo = "individual"     # lo mas conservador
            return
        # El criterio NO puede ser "¿dan el mismo numero?": las dos lecturas se
        # toman en instantes distintos y cualquier ruido de medida las separa,
        # con lo que siempre elegiria individual. Lo que importa es CUANTOS
        # CANALES entrega cada forma: se cambia a individual solo si asi salen
        # canales que en bloque venian a cero.
        vivos_b = [i for i, v in enumerate(bloque) if v]
        vivos_i = [i for i, v in enumerate(individual) if v]
        if len(vivos_i) > len(vivos_b):
            self.modo_efectivo = "individual"
            nombres = ["humedad", "temperatura", "conductividad", "pH"]
            ganados = [nombres[i] for i in vivos_i if i not in vivos_b]
            self.aviso_modo = (
                f"leyendo de uno en uno aparecen canales que en bloque venian a "
                f"cero ({', '.join(ganados)}): bloque={bloque}, "
                f"individual={individual}. Se pasa a lectura INDIVIDUAL, como el "
                f"script de referencia.")
        else:
            self.modo_efectivo = "bloque"
            self.aviso_modo = ("las dos formas de leer entregan los mismos canales; "
                               "se usa lectura en bloque (una trama por sensor)")

    def leer(self, maestro: MaestroModbus, pausa=0.0):
        """Lee los cuatro parametros basicos y, si se pide, NPK/salinidad/TDS."""
        c = self.cfg
        try:
            if self.modo_efectivo is None:
                self._elegir_modo(maestro)
            if self.modo_efectivo == "individual":
                crudos = self._leer_individual(maestro, pausa)
            else:
                crudos = self._leer_bloque(maestro)
        except ErrorModbus as e:
            self.fallos_seguidos += 1
            self.lecturas_error += 1
            self.ultimo_error = str(e)
            return None

        vwc = crudos[0] / 10.0 + c.offset_humedad
        temp = _con_signo(crudos[1]) / 10.0 + c.offset_temperatura
        ec = float(crudos[2])
        ph = crudos[3] / 10.0
        lectura = {
            "zona": c.zona, "direccion": c.direccion, "t": time.time(),
            "vwc": vwc, "temperatura": temp, "conductividad": ec, "ph": ph,
            "humedad_rel": vwc / max(1e-6, c.vwc_capacidad_campo),
            "crudos": crudos,
            # La conductividad (y con ella salinidad, TDS y la estimacion de
            # NPK) solo es representativa con el suelo suficientemente humedo.
            "ec_fiable": vwc >= VWC_MINIMO_EC,
            "avisos": [],
        }
        for clave, campo in (("vwc", "vwc"), ("temperatura", "temperatura"),
                             ("conductividad", "conductividad"), ("ph", "ph")):
            lo, hi = LIMITES[clave]
            if not lo <= lectura[campo] <= hi:
                lectura["avisos"].append(
                    f"{campo}={lectura[campo]:.2f} fuera del rango del sensor "
                    f"({lo}..{hi})")
        if not lectura["ec_fiable"]:
            lectura["avisos"].append(
                f"humedad {vwc:.1f} % < {VWC_MINIMO_EC:.0f} %: la conductividad "
                f"no es fiable (lo advierte el fabricante)")
        # Sonda al aire / sin contacto con el sustrato. El indicio fuerte es que
        # el registro de humedad valga EXACTAMENTE 0 cuentas: un suelo real, por
        # seco que este, devuelve algo. La conductividad solo se usa como prueba
        # adicional si esta variante de sensor la trae.
        crudo_hum_cero = crudos[0] == 0
        ec_delata = (ec <= 0.0) if c.tiene_ec else True
        lectura["sin_contacto"] = crudo_hum_cero and ec_delata
        if lectura["sin_contacto"]:
            lectura["avisos"].insert(0, (
                f"el registro de humedad vale 0 cuentas exactas"
                + (", y la conductividad tambien" if c.tiene_ec else "")
                + f" (pH {ph:.1f}). Lo mas probable es que las agujas no esten "
                f"COMPLETAMENTE metidas en el sustrato, o que haya huecos de aire "
                f"alrededor. La temperatura si es valida."))
        lectura["valido"] = (not any("fuera del rango" in a for a in lectura["avisos"])
                             and not lectura["sin_contacto"])

        lectura["modo_lectura"] = self.modo_efectivo
        if c.leer_npk:
            try:
                # NPK en bloque de 3, igual que el script de referencia.
                extra = maestro.leer_registros(c.direccion, REG_NITROGENO, 5)
                lectura.update({"nitrogeno": extra[0], "fosforo": extra[1],
                                "potasio": extra[2], "salinidad": extra[3],
                                "tds": extra[4]})
            except ErrorModbus as e:
                lectura["avisos"].append(f"NPK no leido: {e}")

        self.fallos_seguidos = 0
        self.lecturas_ok += 1
        self.ultima = lectura
        return lectura

    def diagnosticar(self, maestro):
        """Recorre TODOS los registros del sensor para ver que trae de verdad.

        Sirve para las dos causas tipicas de 'la humedad marca 0': que la sonda
        no haga contacto, o que la variante comprada no tenga ese canal (el
        SN-3002 se vende en versiones con y sin conductividad, y con y sin
        temperatura/humedad; apartado 1.5 del datasheet). Tambien revisa los
        registros de calibracion, porque un valor raro escrito ahi desplaza
        todas las lecturas y no hay forma de verlo desde la lectura normal.
        """
        d = {"direccion": self.cfg.direccion, "bloques": {}, "avisos": [],
             "responde": False}
        bloques = [
            ("basicos", REG_HUMEDAD, 4,
             ["humedad_x10", "temperatura_x10", "conductividad", "ph_x10"]),
            ("nutrientes", REG_NITROGENO, 5,
             ["nitrogeno", "fosforo", "potasio", "salinidad", "tds"]),
            ("fertilidad", REG_FERTILIDAD, 1, ["fertilidad"]),
            ("coeficientes", 0x0022, 3,
             ["coef_temp_ec", "coef_salinidad", "coef_tds"]),
            ("calibracion", REG_CAL_TEMPERATURA, 4,
             ["cal_temperatura_x10", "cal_humedad_x10", "cal_conductividad",
              "cal_ph"]),
            ("identidad", REG_DIRECCION, 2, ["direccion", "codigo_baudios"]),
        ]
        for nombre, base, n, etiquetas in bloques:
            try:
                v = maestro.leer_registros(self.cfg.direccion, base, n)
                d["bloques"][nombre] = dict(zip(etiquetas, v))
                d["responde"] = True
            except ExcepcionModbus:
                d["bloques"][nombre] = None      # esta variante no lo implementa
            except ErrorModbus as e:
                d["avisos"].append(f"no responde al leer '{nombre}': {e}")
                return d

        bas = d["bloques"].get("basicos") or {}
        cal = d["bloques"].get("calibracion") or {}
        if bas:
            nut = d["bloques"].get("nutrientes") or {}
            nut_vivos = any(v for v in nut.values())
            if bas["humedad_x10"] == 0:
                d["avisos"].append(
                    "la humedad marca 0 cuentas EXACTAS. Un suelo real, por seco "
                    "que este, devuelve algo. Causas, en orden de probabilidad: "
                    "(1) las agujas no estan completamente enterradas o hay aire "
                    "alrededor -metela a fondo, compacta el sustrato y espera "
                    "hasta 5 min-; (2) el canal de humedad no existe o esta "
                    "averiado. PARA DISTINGUIRLAS: mete las agujas en un vaso de "
                    "agua (el sensor es IP68 y el fabricante lo permite). En agua "
                    "la humedad debe irse a valores altos en segundos; si sigue "
                    "en 0, el canal no funciona. Usa 'Probar canales' mientras la "
                    "mojas.")
            if bas["conductividad"] == 0 and self.cfg.tiene_ec:
                d["avisos"].append(
                    "la conductividad marca 0"
                    + (" pero los nutrientes NO son cero, y en este sensor N, P y "
                       "K se derivan de la conductividad: el canal existe y lo que "
                       "falla es el contacto." if nut_vivos else
                       " y los nutrientes tambien (N, P y K se derivan de la "
                       "conductividad, asi que es coherente): o no hay contacto, o "
                       "tu variante no trae ese canal."))
            if bas["ph_x10"] and bas["ph_x10"] <= 30 and bas["humedad_x10"] == 0:
                d["avisos"].append(
                    "OJO: humedad, conductividad y pH estan los tres en el suelo "
                    "de su rango a la vez. Tres canales distintos fallando igual "
                    "apunta a una sola causa comun: las agujas no tocan sustrato "
                    "humedo. La temperatura no lo sufre porque es un sensor "
                    "interno independiente.")
            if bas["ph_x10"] and bas["ph_x10"] <= 30:
                d["avisos"].append(
                    f"el pH esta pegado al minimo del rango ({bas['ph_x10']/10:.1f}, "
                    f"el sensor mide 3..9): tipico cuando los electrodos no tocan "
                    f"suelo humedo.")
            d["nutrientes_vivos"] = nut_vivos
        for k, v in cal.items():
            if v not in (0, None):
                d["avisos"].append(
                    f"hay una calibracion escrita en el sensor: {k} = {v}. Si no la "
                    f"pusiste tu, desplaza todas las lecturas; se pone a 0 "
                    f"escribiendo ese registro.")
        ident = d["bloques"].get("identidad") or {}
        if ident:
            b = CODIGO_BAUDIOS.get(ident.get("codigo_baudios"), "?")
            d["baudios_declarados"] = b
            if b != "?" and b != maestro.tr.baudios:
                d["avisos"].append(
                    f"el sensor dice estar configurado a {b} baudios y el bus va a "
                    f"{maestro.tr.baudios}: funciona por casualidad, igualalos.")
        return d

    CANALES = [("humedad", REG_HUMEDAD, 10.0, "%"),
               ("temperatura", REG_TEMPERATURA, 10.0, "C"),
               ("conductividad", REG_CONDUCTIVIDAD, 1.0, "uS/cm"),
               ("ph", REG_PH, 10.0, ""),
               ("nitrogeno", REG_NITROGENO, 1.0, "mg/kg"),
               ("fosforo", REG_FOSFORO, 1.0, "mg/kg"),
               ("potasio", REG_POTASIO, 1.0, "mg/kg")]

    def probar_canales(self, maestro, muestras=20, pausa=0.5, parar=None):
        """Lee repetidamente y dice QUE CANALES ESTAN VIVOS.

        La idea: un canal que existe y funciona CAMBIA cuando manipulas la sonda
        (la mueves, la entierras, la mojas). Un canal inexistente o averiado se
        queda clavado en el mismo numero. Es la unica prueba que distingue
        'no hay contacto' de 'este sensor no mide eso', porque el mapa de
        registros es identico en todas las variantes del SN-3002 y los canales
        ausentes simplemente devuelven 0.

        Uso: lanzar la prueba y, mientras corre, mojar las agujas en un vaso de
        agua o clavarlas en sustrato humedo.
        """
        historial = {n: [] for n, _, _, _ in self.CANALES}
        errores = 0
        for _ in range(int(muestras)):
            if parar is not None and parar.is_set():
                break
            try:
                v = maestro.leer_registros(self.cfg.direccion, REG_HUMEDAD, 7)
                for (nombre, reg, escala, _u), crudo in zip(self.CANALES, v):
                    valor = (_con_signo(crudo) if nombre == "temperatura"
                             else crudo) / escala
                    historial[nombre].append(valor)
            except ErrorModbus:
                errores += 1
            time.sleep(pausa)
        informe = {"direccion": self.cfg.direccion, "muestras": 0,
                   "errores": errores, "canales": {}, "avisos": []}
        for nombre, _reg, _e, unidad in self.CANALES:
            serie = historial[nombre]
            if not serie:
                continue
            informe["muestras"] = len(serie)
            lo, hi = min(serie), max(serie)
            distintos = len(set(serie))
            if hi == 0.0 and lo == 0.0:
                estado = "SIEMPRE CERO"
            elif distintos == 1:
                estado = "constante"
            else:
                estado = "vivo"
            informe["canales"][nombre] = {
                "min": lo, "max": hi, "ultimo": serie[-1], "distintos": distintos,
                "estado": estado, "unidad": unidad}
        vivos = [n for n, c in informe["canales"].items() if c["estado"] == "vivo"]
        ceros = [n for n, c in informe["canales"].items()
                 if c["estado"] == "SIEMPRE CERO"]
        if "humedad" in ceros:
            informe["avisos"].append(
                "la humedad no se movio del cero durante toda la prueba. Si has "
                "mojado o movido la sonda mientras corria, el canal de humedad NO "
                "funciona: o tu variante no lo trae (comprueba el codigo de la "
                "etiqueta) o la sonda esta averiada. Si NO la manipulaste, repite "
                "la prueba moviendola.")
        elif "humedad" in vivos:
            c = informe["canales"]["humedad"]
            informe["avisos"].append(
                f"el canal de humedad SI funciona: vario entre {c['min']:.1f} % y "
                f"{c['max']:.1f} %. El problema es de contacto o de instalacion, "
                f"no del sensor.")
        if ceros:
            informe["avisos"].append(
                f"canales que nunca salieron de cero: {', '.join(ceros)}.")
        if vivos:
            informe["avisos"].append(f"canales que variaron: {', '.join(vivos)}.")
        return informe

    # ---- configuracion del propio sensor -------------------------------------
    def asignar_direccion(self, maestro, nueva):
        """Graba una direccion nueva (registro 0x07D0).

        SOLO con ese sensor conectado al bus: la escritura va dirigida a la
        direccion actual, y si hay dos sensores con la misma responderian los
        dos a la vez."""
        if not 1 <= nueva <= 254:
            raise ValueError("la direccion debe estar entre 1 y 254")
        maestro.escribir_registro(self.cfg.direccion, REG_DIRECCION, int(nueva))
        self.cfg.direccion = int(nueva)
        return True

    def cambiar_baudios(self, maestro, baudios):
        """Cambia la velocidad del sensor (0x07D1). Ojo: despues hay que
        reabrir el puerto a la nueva velocidad, y TODOS los dispositivos del
        bus tienen que ir a la misma."""
        if baudios not in BAUDIOS_CODIGO:
            raise ValueError(f"velocidad no admitida: {baudios}")
        maestro.escribir_registro(self.cfg.direccion, REG_BAUDIOS,
                                  BAUDIOS_CODIGO[baudios])
        return True

    def calibrar_por_referencia(self, maestro, vwc_referencia):
        """Ajusta el offset por software para que la lectura coincida con una
        medida de referencia (secado en estufa, sonda patron...). No toca los
        registros de calibracion del sensor: es reversible."""
        lec = self.leer(maestro)
        if lec is None:
            raise ErrorModbus("no se pudo leer el sensor para calibrar")
        bruto = lec["vwc"] - self.cfg.offset_humedad
        self.cfg.offset_humedad = float(vwc_referencia) - bruto
        return self.cfg.offset_humedad


# =============================================================================
# ACTUADOR (ESP32)
# =============================================================================

class ActuadorESP32:
    """Las electrovalvulas vistas como bobinas Modbus de un ESP32 esclavo.

    Una sola trama 0x0F fija todas las valvulas a la vez: evita estados
    intermedios y reduce el trafico del bus."""

    def __init__(self, cfg: ConfigActuador):
        self.cfg = cfg
        self.ultimo_mando = None
        self.ultimo_leido = None
        self.fallos_seguidos = 0
        self.ultimo_error = ""
        self.discrepancias = 0

    def _fisico(self, abierto):
        return (not abierto) if self.cfg.invertido else bool(abierto)

    def aplicar(self, maestro, niveles):
        """niveles: lista de aperturas por zona (0 = cerrada, >0 = abierta)."""
        if not self.cfg.activo:
            return False
        deseado = [bool(n > 0.5) for n in niveles]
        try:
            maestro.escribir_bobinas(self.cfg.direccion, self.cfg.bobina_base,
                                     [self._fisico(v) for v in deseado])
            self.ultimo_mando = deseado
            self.fallos_seguidos = 0
        except ErrorModbus as e:
            self.fallos_seguidos += 1
            self.ultimo_error = str(e)
            return False
        if self.cfg.verificar:
            leido = self.leer(maestro, len(deseado))
            if leido is not None and leido != deseado:
                self.discrepancias += 1
                self.ultimo_error = (f"el ESP32 confirma {leido} pero se mando "
                                     f"{deseado}")
                return False
        return True

    def leer(self, maestro, n):
        if not self.cfg.activo:
            return None
        try:
            crudo = maestro.leer_bobinas(self.cfg.direccion, self.cfg.bobina_base, n)
        except ErrorModbus as e:
            self.ultimo_error = str(e)
            return None
        self.ultimo_leido = [self._fisico(v) for v in crudo]
        return self.ultimo_leido

    def diagnosticar(self, maestro, n_zonas=None):
        """Interroga los registros de estado del ESP32.

        Es la prueba que separa los tres fallos posibles cuando una valvula no
        obedece: (a) el ESP32 no esta en el bus -> no responde a nada;
        (b) esta pero con otra direccion o velocidad -> tampoco responde, aunque
        su contador de tramas DESCARTADAS sube porque si oye el trafico;
        (c) responde pero rechaza el mando -> problema de rango de bobinas.
        """
        d = {"responde": False, "avisos": [], "registros": None}
        try:
            r = maestro.leer_registros(self.cfg.direccion, REG_ESP_MAPA, 8)
        except ExcepcionModbus as e:
            d["avisos"].append(
                f"responde pero rechaza leer sus registros de estado ({e}). "
                f"¿Es realmente el ESP32 con este firmware, o hay otro "
                f"dispositivo en la direccion {self.cfg.direccion}?")
            d["responde"] = True
            return d
        except ErrorModbus as e:
            d["avisos"].append(
                f"no responde ({e}). Comprueba, por este orden: "
                f"1) que el ESP32 esta alimentado y con el programa cargado; "
                f"2) que su DIRECCION_MODBUS es {self.cfg.direccion} y su "
                f"velocidad la misma que los sensores; "
                f"3) A con A y B con B (sin invertir) y GND comun con el bus; "
                f"4) DE y RE unidos al mismo pin; "
                f"5) 120 Ohm solo en los DOS extremos del bus.")
            return d
        d["responde"] = True
        d["registros"] = {
            "mapa_valvulas": r[0], "uptime_s": r[1], "tramas_validas": r[2],
            "tramas_descartadas": r[3], "sin_ordenes_s": r[4],
            "guarda_s": r[5], "version": r[6], "n_valvulas": r[7],
        }
        n_val = r[7]
        if not 1 <= n_val <= 64:
            d["avisos"].append(
                f"el numero de valvulas que declara ({n_val}) no es creible: "
                f"seguramente el firmware es anterior y no publica el registro 7.")
            n_val = None
        elif n_zonas is not None:
            fin = self.cfg.bobina_base + n_zonas
            if fin > n_val:
                d["avisos"].append(
                    f"MANDO IMPOSIBLE: se piden {n_zonas} bobinas desde la "
                    f"{self.cfg.bobina_base} (es decir hasta la {fin - 1}) pero el "
                    f"ESP32 solo tiene {n_val} (0..{n_val - 1}). Pon 'Bobina de la "
                    f"zona 0' a 0, o sube N_VALVULAS en el firmware.")
        if r[3] > 0 and r[2] == 0:
            d["avisos"].append(
                f"ha descartado {r[3]} tramas y no ha aceptado ninguna: oye el bus "
                f"pero ninguna trama va dirigida a el. Casi seguro su direccion no "
                f"es {self.cfg.direccion}.")
        if r[5] and r[4] > r[5]:
            d["avisos"].append(
                f"lleva {r[4]} s sin ordenes validas y su tiempo de guarda es "
                f"{r[5]} s: ha cerrado las valvulas por seguridad.")
        return d

    def cerrar_todo(self, maestro, n):
        """Parada segura: se llama al soltar el bus o al perder la comunicacion."""
        try:
            maestro.escribir_bobinas(self.cfg.direccion, self.cfg.bobina_base,
                                     [self._fisico(False)] * n)
            return True
        except ErrorModbus:
            return False


# =============================================================================
# BUS DE CAMPO
# =============================================================================

class BusCampo:
    """Dueno del puerto: sondea los sensores y manda el actuador."""

    def __init__(self, cfg: ConfigCampo, log=None, esclavos_simulados=None):
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.maestro = None          # bus de los sensores
        self.maestro_act = None      # bus del actuador (puede ser el mismo)
        self.sensores = [SensorSuelo(s) for s in cfg.sensores]
        self.actuador = ActuadorESP32(cfg.actuador)
        self._simulados = esclavos_simulados
        self.ultimo_ciclo_s = 0.0
        self.ciclos = 0
        self.trazas = []          # ultimas tramas, para depurar desde la interfaz

    # ---- conexion ------------------------------------------------------------
    def _traza(self, m):
        self.trazas.append(m)
        if len(self.trazas) > 200:
            del self.trazas[:len(self.trazas) - 200]

    def _abrir(self, puerto, baudios, etiqueta):
        if puerto == "simulado" or self._simulados is not None:
            tr = TransporteSimulado(self._simulados or {})
            tr.baudios = baudios
        else:
            tr = TransporteSerie(puerto, baudios, self.cfg.timeout)
        m = MaestroModbus(tr, self.cfg.timeout, self.cfg.reintentos,
                          log=lambda x, e=etiqueta: self._traza(f"[{e}] {x}"))
        self.log(f"[campo] Bus {etiqueta} abierto en {puerto} a {baudios} baudios, "
                 f"8N1. Silencio entre tramas {m.silencio*1000:.1f} ms.")
        return m

    def conectar(self):
        self.maestro = self._abrir(self.cfg.puerto, self.cfg.baudios, "sensores")
        if self.cfg.actuador_aparte:
            self.maestro_act = self._abrir(self.cfg.actuador.puerto,
                                           self.cfg.actuador.baudios, "valvulas")
        else:
            self.maestro_act = self.maestro
            if self.cfg.actuador.activo:
                self.log("[campo] El ESP32 comparte el bus con los sensores.")
        return True

    def cerrar(self, n_zonas=None):
        if self.maestro is None:
            return
        if n_zonas and self.maestro_act is not None:
            self.actuador.cerrar_todo(self.maestro_act, n_zonas)
            self.log("[campo] Electrovalvulas cerradas antes de soltar el bus.")
        if self.maestro_act is not None and self.maestro_act is not self.maestro:
            self.maestro_act.cerrar()
        self.maestro.cerrar()
        self.maestro = self.maestro_act = None

    @property
    def conectado(self):
        return self.maestro is not None

    # ---- temporizacion -------------------------------------------------------
    def tiempo_ciclo_estimado(self):
        """Cuanto tarda un ciclo completo de sondeo, en el mejor caso.

        Sirve para comprobar que el tiempo de muestreo es realizable: a 4800
        baudios cada lectura de 4 registros son 8 bytes de ida y 13 de vuelta."""
        if self.maestro is None:
            baudios = self.cfg.baudios
            silencio = max(0.002, 3.5 * 11.0 / baudios)
        else:
            baudios, silencio = self.maestro.tr.baudios, self.maestro.silencio
        t = 0.0
        for s in self.sensores:
            if not s.cfg.activo:
                continue
            # En lectura individual son 4 tramas de 1 registro en vez de 1 de 4.
            individual = (s.modo_efectivo or s.cfg.modo_lectura) == "individual"
            tramas = (4 if individual else 1) + (1 if s.cfg.leer_npk else 0)
            t += tramas * ((8 + 13) * 11.0 / baudios + 2 * silencio)
            t += self.cfg.pausa_entre_sensores
        if self.cfg.actuador.activo and not self.cfg.actuador_aparte:
            n = 1 + (1 if self.cfg.actuador.verificar else 0)
            t += n * ((9 + 8) * 11.0 / baudios + 2 * silencio)
        return t

    def avisos_muestreo(self):
        """Avisos sobre el tiempo de muestreo elegido."""
        avisos = []
        minimo = self.tiempo_ciclo_estimado()
        peor = minimo + len([s for s in self.sensores if s.cfg.activo]) * \
            self.cfg.timeout * (self.cfg.reintentos + 1)
        if self.cfg.tiempo_muestreo < peor:
            avisos.append(
                f"Ts = {self.cfg.tiempo_muestreo:g} s es menor que el peor caso del "
                f"bus ({peor:.2f} s con reintentos y timeouts): si un sensor falla, "
                f"el ciclo se solapara. Sube Ts o baja los reintentos.")
        if self.cfg.tiempo_muestreo < 1.0:
            avisos.append("Ts por debajo de 1 s no aporta nada: el suelo no cambia "
                          "tan rapido y solo desgasta el bus.")
        if self.cfg.tiempo_muestreo > 600.0:
            avisos.append(
                f"Ts = {self.cfg.tiempo_muestreo:g} s (mas de 10 min) deja al control "
                f"casi ciego: con el riego abierto la humedad puede cruzar la banda "
                f"entera entre dos muestras.")
        return avisos

    # ---- ciclo de sondeo -----------------------------------------------------
    def ciclo(self):
        """Lee todos los sensores activos. Devuelve {zona: lectura}."""
        if self.maestro is None:
            return {}
        t0 = time.monotonic()
        lecturas = {}
        pausa = self.cfg.pausa_entre_sensores
        for s in self.sensores:
            if not s.cfg.activo:
                continue
            lec = s.leer(self.maestro, pausa)
            if lec is not None:
                lecturas[s.cfg.zona] = lec
                if s.aviso_modo:
                    self.log(f"[campo] Zona {s.cfg.zona}: {s.aviso_modo}")
                    s.aviso_modo = ""
            if pausa:
                time.sleep(pausa)
            elif s.fallos_seguidos in (1, 5, 20):
                self.log(f"[campo] Zona {s.cfg.zona} (dir. {s.cfg.direccion}) sin "
                         f"respuesta x{s.fallos_seguidos}: {s.ultimo_error}")
        self.ultimo_ciclo_s = time.monotonic() - t0
        self.ciclos += 1
        return lecturas

    def diagnosticar_actuador(self, n_zonas):
        if self.maestro_act is None:
            return {"responde": False,
                    "avisos": ["el bus no esta abierto"], "registros": None}
        return self.actuador.diagnosticar(self.maestro_act, n_zonas)

    def aplicar_valvulas(self, niveles):
        if self.maestro_act is None:
            return False
        ok = self.actuador.aplicar(self.maestro_act, niveles)
        if not ok and self.actuador.fallos_seguidos in (1, 5, 20):
            self.log(f"[campo] El ESP32 (dir. {self.cfg.actuador.direccion}) no "
                     f"confirma el mando: {self.actuador.ultimo_error}")
        return ok

    # ---- herramientas de puesta en marcha ------------------------------------
    def escanear(self, desde=1, hasta=32, progreso=None):
        """Busca que hay en el bus. Los sensores responden al registro 0x0000;
        el ESP32 puede no tenerlo, asi que tambien se le pregunta por bobinas."""
        if self.maestro is None:
            return []
        encontrados = []
        for u in range(desde, hasta + 1):
            if progreso:
                progreso(u, desde, hasta)
            tipo = None
            # Se pregunta PRIMERO por bobinas: es lo que distingue de verdad al
            # actuador del sensor. El SN-3002 no implementa la funcion 0x01 y
            # responde 'funcion ilegal'; el ESP32 si. Al reves no funciona,
            # porque los registros 0..3 del ESP32 tambien se leen y se
            # confundirian con una lectura de suelo.
            try:
                b = self.maestro.leer_bobinas(u, self.cfg.actuador.bobina_base, 1)
                n_val = max(1, len(self.sensores))
                try:
                    b = self.maestro.leer_bobinas(u, self.cfg.actuador.bobina_base,
                                                  n_val)
                except ErrorModbus:
                    pass
                tipo = ("actuador de valvulas (ESP32)", {"bobinas": b})
            except ErrorModbus:
                pass
            if tipo is None:
                try:
                    v = self.maestro.leer_registros(u, REG_HUMEDAD, 4)
                    datos = {"vwc": v[0] / 10.0,
                             "temperatura": _con_signo(v[1]) / 10.0,
                             "conductividad": v[2], "ph": v[3] / 10.0}
                    creible = (0 <= datos["vwc"] <= 100
                               and -40 <= datos["temperatura"] <= 80
                               and 0 <= datos["ph"] <= 14)
                    tipo = ("sensor de suelo" if creible
                            else "dispositivo (valores no creibles para un sensor)",
                            datos)
                except ExcepcionModbus:
                    tipo = ("dispositivo (rechazo el registro 0x0000)", {})
                except ErrorModbus:
                    tipo = None
            if tipo:
                encontrados.append({"direccion": u, "tipo": tipo[0], "datos": tipo[1]})
                self.log(f"[campo] Direccion {u}: {tipo[0]} {tipo[1]}")
        return encontrados

    def diagnosticar_sensores(self):
        """Diagnostico completo de cada sensor activo."""
        if self.maestro is None:
            return []
        return [dict(s.diagnosticar(self.maestro), zona=s.cfg.zona)
                for s in self.sensores if s.cfg.activo]

    def probar_canales(self, zona, muestras=20, pausa=0.5, parar=None):
        for s in self.sensores:
            if s.cfg.zona == zona:
                return s.probar_canales(self.maestro, muestras, pausa, parar)
        return None

    def asignar_direccion(self, actual, nueva):
        """Cambia la direccion de un sensor. Con UN SOLO sensor en el bus."""
        if self.maestro is None:
            raise ErrorModbus("el bus no esta abierto")
        self.maestro.escribir_registro(actual, REG_DIRECCION, int(nueva))
        self.log(f"[campo] Direccion {actual} -> {nueva}. Desconecta y vuelve a "
                 f"conectar el sensor para que la tome.")
        return True

    def resumen(self):
        est = self.maestro.estadisticas if self.maestro else {}
        return {
            "puerto": self.cfg.puerto, "baudios": self.cfg.baudios,
            "conectado": self.conectado, "ciclos": self.ciclos,
            "ultimo_ciclo_s": self.ultimo_ciclo_s,
            "ciclo_estimado_s": self.tiempo_ciclo_estimado(),
            "tiempo_muestreo": self.cfg.tiempo_muestreo,
            "estadisticas": dict(est),
            "sensores": [{"zona": s.cfg.zona, "direccion": s.cfg.direccion,
                          "ok": s.lecturas_ok, "error": s.lecturas_error,
                          "fallos_seguidos": s.fallos_seguidos,
                          "ultima": s.ultima, "ultimo_error": s.ultimo_error}
                         for s in self.sensores],
            "actuador": {"direccion": self.cfg.actuador.direccion,
                         "mando": self.actuador.ultimo_mando,
                         "leido": self.actuador.ultimo_leido,
                         "discrepancias": self.actuador.discrepancias,
                         "fallos_seguidos": self.actuador.fallos_seguidos,
                         "ultimo_error": self.actuador.ultimo_error},
        }


# =============================================================================
# ASIMILACION: METER LAS MEDIDAS EN EL MODELO
# =============================================================================

class Asimilador:
    """
    Corrige el estado del modelo con lo que miden los sensores.

    Modos:
      'solo_lectura' : el modelo sigue por su cuenta; las medidas solo se
                       registran. Util para comparar modelo y realidad.
      'medida'       : el estado de la zona se sustituye por la medida. El
                       modelo queda como interpolador entre muestras y como
                       estimador del perfil interno.
      'nudging'      : (por defecto) se corrige una fraccion 'ganancia' de la
                       diferencia en cada muestra. Conserva la fisica del modelo
                       -tendencia, cascada, perfil por sub-zonas, que es lo que
                       usa el control- pero anclada a la realidad. Con ganancia
                       0,35 y Ts = 60 s, una diferencia se corrige en ~3 min.

    Ademas, si 'autocalibrar_evaporacion' esta activo, estima un factor por zona
    que corrige el sesgo sistematico del modelo: si el lecho real se seca mas
    rapido de lo que predice, el factor sube. Es una calibracion lenta e
    integral, acotada entre 0,3 y 3.
    """

    def __init__(self, sim, cfg: ConfigCampo, log=None):
        self.sim = sim
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.factor_evap = {}
        self.innovacion = {}       # zona -> media movil de (medido - modelo)
        self.medidas = {}
        self.asimilaciones = 0

    def asimilar(self, lecturas):
        """Aplica las medidas al modelo. Debe llamarse con el lock del modelo."""
        if not lecturas:
            return {}
        sim = self.sim
        W_fc = sim._W_fc()
        aplicado = {}
        for zona, lec in lecturas.items():
            if zona >= sim.n_zones:
                continue
            if lec.get("sin_contacto") and not self.cfg.bloquear_sin_contacto:
                pass          # el usuario asume el riesgo: se asimila igual
            elif not lec.get("valido", True):
                # Se guarda la medida para que la interfaz la muestre, pero no
                # se mete en el modelo: una sonda al aire arrastraria el modelo
                # a cero y el control regaria sin parar.
                self.medidas[zona] = lec
                continue
            celdas = sim.celdas_de[zona]
            medido = lec["humedad_rel"]
            modelo = sum(sim.W[c] for c in celdas) / (len(celdas) * W_fc)
            dif = medido - modelo
            k = (1.0 if self.cfg.modo == "medida"
                 else (0.0 if self.cfg.modo == "solo_lectura"
                       else max(0.0, min(1.0, self.cfg.ganancia))))
            if k > 0.0:
                # La correccion se reparte entre las celdas CONSERVANDO el perfil
                # interno relativo: el sensor mide un punto, no el perfil, y el
                # perfil es lo que aporta el modelo.
                W_objetivo = (modelo + k * dif) * W_fc
                escala = (W_objetivo / max(1e-9, modelo * W_fc)) if modelo > 1e-9 else None
                W_sat = sim._W_sat()
                for c in celdas:
                    nuevo = (sim.W[c] * escala if escala is not None
                             else W_objetivo)
                    sim.W[c] = min(W_sat, max(0.0, nuevo))
                kt = max(0.0, min(1.0, self.cfg.ganancia_temperatura
                                  if self.cfg.modo != "medida" else 1.0))
                for c in celdas:
                    sim.T[c] += kt * (lec["temperatura"] - sim.T[c])
            # Sesgo acumulado: sirve para autocalibrar y para avisar al usuario.
            previo = self.innovacion.get(zona, 0.0)
            self.innovacion[zona] = previo + 0.1 * (dif - previo)
            if self.cfg.autocalibrar_evaporacion:
                f = self.factor_evap.get(zona, 1.0)
                # Si el lecho real esta mas SECO que el modelo (dif<0), el modelo
                # evapora de menos: hay que subir el factor.
                f *= (1.0 - 0.02 * self.innovacion[zona] / max(0.05, abs(medido)))
                self.factor_evap[zona] = max(0.3, min(3.0, f))
            aplicado[zona] = {"medido": medido, "modelo": modelo, "dif": dif,
                              "innovacion": self.innovacion[zona],
                              "factor_evap": self.factor_evap.get(zona, 1.0)}
            self.medidas[zona] = lec
        # Se publica en el modelo para que la interfaz y el servicio lo vean.
        sim.medidas = {z: dict(self.medidas[z], **aplicado.get(z, {}))
                       for z in self.medidas}
        self.asimilaciones += 1
        return aplicado

    def resumen(self):
        return {"asimilaciones": self.asimilaciones, "modo": self.cfg.modo,
                "ganancia": self.cfg.ganancia,
                "innovacion": dict(self.innovacion),
                "factor_evap": dict(self.factor_evap)}


# =============================================================================
# LAZO DE CAMPO (hilo propio)
# =============================================================================

class LazoCampo:
    """
    Hilo que se ocupa del bus: sondea a Ts, asimila y aplica las valvulas.

    Va en un hilo aparte porque el puerto serie BLOQUEA: un sensor que no
    responde puede costar timeout x reintentos, y eso no puede congelar la
    interfaz ni el reloj de la simulacion. Se compone en vez de heredar de
    Thread (ver la nota del servicio: heredar tapa metodos con los atributos
    internos de Thread).
    """

    def __init__(self, sim, lock, flags, bus: BusCampo, asimilador: Asimilador,
                 log=None):
        self.sim, self.lock, self.flags = sim, lock, flags
        self.bus, self.asimilador = bus, asimilador
        self.log = log or (lambda m: None)
        self._corriendo = threading.Event()
        self._forzar = threading.Event()
        self._hilo = None
        self.sondeos = 0
        self.proximo_sondeo = 0.0     # time.time() del proximo ciclo
        self.ultimo_sondeo = 0.0
        self.ultimo_aplicado = None
        self.errores = 0

    def start(self):
        if self._hilo is not None and self._hilo.is_alive():
            return self
        self._corriendo.set()
        self._hilo = threading.Thread(target=self._bucle, name="lazo-campo",
                                      daemon=True)
        self._hilo.start()
        return self

    def stop(self, cerrar_valvulas=True):
        self._corriendo.clear()
        if self._hilo is not None:
            self._hilo.join(timeout=3.0)
        with self.lock:
            n = self.sim.n_zones
        if cerrar_valvulas:
            self.bus.cerrar(n_zonas=n)
        else:
            self.bus.cerrar()

    def sondear_ahora(self):
        """Pide un ciclo de sondeo inmediato SIN tocar el puerto desde el hilo
        que llama. Es lo que usa el boton 'Probar una lectura' cuando el lazo ya
        esta en marcha: si la interfaz hablara por su cuenta con el bus, dos
        hilos se pisarian en un medio que es medio duplex."""
        self._forzar.set()

    @property
    def activo(self):
        return self._hilo is not None and self._hilo.is_alive()

    def edad_lectura(self):
        """Segundos desde la ultima lectura buena (para que la interfaz pueda
        decir si lo que muestra es de ahora o de hace un minuto)."""
        return (time.time() - self.ultimo_sondeo) if self.ultimo_sondeo else None

    def falta_para_sondeo(self):
        return max(0.0, self.proximo_sondeo - time.time()) if self.proximo_sondeo else None

    def _bucle(self):
        Ts = max(0.2, float(self.bus.cfg.tiempo_muestreo))
        for aviso in self.bus.avisos_muestreo():
            self.log(f"[campo] AVISO: {aviso}")
        self.log(f"[campo] Lazo de campo en marcha: sondeo cada {Ts:g} s "
                 f"(un ciclo de bus tarda ~{self.bus.tiempo_ciclo_estimado()*1000:.0f} ms), "
                 f"modo '{self.bus.cfg.modo}'.")
        proximo = time.monotonic()
        self.proximo_sondeo = time.time()
        while self._corriendo.is_set():
            ahora = time.monotonic()
            forzado = self._forzar.is_set()
            if ahora >= proximo or forzado:
                self._forzar.clear()
                proximo = ahora + Ts
                self.proximo_sondeo = time.time() + Ts
                try:
                    lecturas = self.bus.ciclo()
                    with self.lock:
                        self.asimilador.asimilar(lecturas)
                    self.sondeos += 1
                    if lecturas:
                        self.ultimo_sondeo = time.time()
                except Exception as e:
                    self.errores += 1
                    self.log(f"[campo] Error en el ciclo de sondeo: {e}")
            # Las valvulas se refrescan siempre que cambie el mando, no solo a Ts:
            # el control puede decidir entre muestras y la valvula debe seguirlo.
            try:
                with self.lock:
                    niveles = list(self.sim.levels)
                if niveles != self.ultimo_aplicado:
                    if self.bus.aplicar_valvulas(niveles):
                        self.ultimo_aplicado = niveles
            except Exception as e:
                self.errores += 1
                self.log(f"[campo] Error aplicando valvulas: {e}")
            time.sleep(0.05)
        self.log("[campo] Lazo de campo detenido.")


# =============================================================================
# SENSOR SIMULADO (banco de pruebas sin hardware)
# =============================================================================

class SensorSimulado(EsclavoSimulado):
    """Esclavo Modbus que finge ser un SN-3002 leyendo de una celda del modelo.

    Permite probar toda la cadena -driver, ciclo, asimilacion, valvulas- sin
    tener el hardware delante, y ademas comprobar que la asimilacion converge.
    """

    def __init__(self, unidad, sim=None, zona=0, vwc_fc=35.0, ruido=0.3,
                 sesgo=0.0):
        super().__init__(unidad, {REG_DIRECCION: unidad, REG_BAUDIOS: 1})
        self.sim, self.zona = sim, zona
        self.vwc_fc, self.ruido, self.sesgo = vwc_fc, ruido, sesgo

    def antes_de_leer(self, direccion, cantidad):
        import random
        if self.sim is not None and self.zona < self.sim.n_zones:
            celdas = self.sim.celdas_de[self.zona]
            W_fc = self.sim._W_fc()
            hum = sum(self.sim.W[c] for c in celdas) / (len(celdas) * W_fc)
            temp = sum(self.sim.T[c] for c in celdas) / len(celdas)
        else:
            hum, temp = 0.65, 22.0
        vwc = hum * self.vwc_fc + self.sesgo + random.uniform(-self.ruido, self.ruido)
        self.registros[REG_HUMEDAD] = max(0, min(1000, int(round(vwc * 10))))
        self.registros[REG_TEMPERATURA] = int(round(temp * 10)) & 0xFFFF
        self.registros[REG_CONDUCTIVIDAD] = int(300 + 900 * hum)
        self.registros[REG_PH] = int(round(7.0 * 10))
        for r, v in ((REG_NITROGENO, 120), (REG_FOSFORO, 45), (REG_POTASIO, 210),
                     (REG_SALINIDAD, 150), (REG_TDS, 400), (REG_FERTILIDAD, 900)):
            self.registros.setdefault(r, v)


def banco_simulado(sim, cfg: ConfigCampo, ruido=0.3, sesgo=0.0):
    """Diccionario de esclavos simulados coherente con la configuracion."""
    esclavos = {}
    for s in cfg.sensores:
        esclavos[s.direccion] = SensorSimulado(s.direccion, sim, s.zona,
                                               s.vwc_capacidad_campo, ruido, sesgo)
    if cfg.actuador.activo:
        esclavos[cfg.actuador.direccion] = EsclavoSimulado(
            cfg.actuador.direccion, {}, bobinas=max(8, sim.n_zones))
    return esclavos


# =============================================================================
# AUTOPRUEBAS Y DEMOSTRACION
# =============================================================================

def selftest():
    from vermicompost_mz import MultiZoneBed, BedParams
    fallos = []

    def check(c, m):
        if not c:
            fallos.append(m)

    sim = MultiZoneBed(BedParams(init_moisture=0.50))
    cfg = ConfigCampo.por_defecto(sim.n_zones)
    cfg.puerto = "simulado"
    cfg.tiempo_muestreo = 5.0
    check(cfg.comprobar(sim.n_zones) == [],
          f"la configuracion por defecto no deberia dar problemas: "
          f"{cfg.comprobar(sim.n_zones)}")

    # Direcciones repetidas y choque con el ESP32.
    mala = ConfigCampo.por_defecto(3)
    mala.sensores[1].direccion = 1
    check(any("repetida" in p for p in mala.comprobar()),
          "debe detectar direcciones Modbus repetidas")
    mala2 = ConfigCampo.por_defecto(3)
    mala2.actuador.direccion = 2
    check(any("ESP32" in p for p in mala2.comprobar()),
          "debe detectar que el ESP32 choca con un sensor")

    # Lectura real de tramas contra sensores simulados ligados al modelo.
    bus = BusCampo(cfg, esclavos_simulados=banco_simulado(sim, cfg, ruido=0.0))
    bus.conectar()
    lec = bus.ciclo()
    check(len(lec) == sim.n_zones, f"deben leerse {sim.n_zones} sensores: {len(lec)}")
    z0 = lec[0]
    check(abs(z0["vwc"] - 0.50 * 35.0) < 0.2,
          f"con humedad 0,50 y VWC_fc=35 el sensor debe dar ~17,5 %: {z0['vwc']:.2f}")
    check(abs(z0["humedad_rel"] - 0.50) < 0.01,
          f"la conversion a humedad relativa debe devolver 0,50: {z0['humedad_rel']:.3f}")
    check(not z0["ec_fiable"] and any("conductividad" in a for a in z0["avisos"]),
          "con VWC 17,5 % debe avisar de que la conductividad no es fiable")

    # Valvulas: ida y vuelta con verificacion.
    ok = bus.aplicar_valvulas([1.0, 0.0, 1.0])
    check(ok, f"el mando de valvulas debe confirmarse: {bus.actuador.ultimo_error}")
    check(bus.actuador.ultimo_leido == [True, False, True],
          f"el ESP32 debe confirmar el estado: {bus.actuador.ultimo_leido}")
    # Rele invertido (activo a nivel bajo).
    cfg_inv = ConfigCampo.por_defecto(3)
    cfg_inv.puerto = "simulado"
    cfg_inv.actuador.invertido = True
    bus_inv = BusCampo(cfg_inv, esclavos_simulados=banco_simulado(sim, cfg_inv))
    bus_inv.conectar()
    bus_inv.aplicar_valvulas([1.0, 0.0, 0.0])
    esp = bus_inv.maestro.tr.esclavos[10]
    check(esp.bobinas[:3] == [False, True, True],
          f"con rele invertido las bobinas deben ir al reves: {esp.bobinas[:3]}")

    # Escaneo del bus.
    hallados = bus.escanear(1, 12)
    dirs = sorted(h["direccion"] for h in hallados)
    check(dirs == [1, 2, 3, 10], f"el escaneo debe hallar 1,2,3 y 10: {dirs}")
    check(any(h["tipo"].startswith("sensor") for h in hallados),
          "debe reconocer los sensores de suelo")

    # Tiempo de muestreo: estimacion y avisos.
    t = bus.tiempo_ciclo_estimado()
    check(0.05 < t < 1.0, f"un ciclo a 4800 baudios deberia rondar 0,2-0,5 s: {t:.3f}")
    cfg.tiempo_muestreo = 0.5
    check(any("menor que el peor caso" in a for a in bus.avisos_muestreo()),
          "debe avisar si Ts no cabe en el peor caso del bus")
    cfg.tiempo_muestreo = 1200.0
    check(any("casi ciego" in a for a in bus.avisos_muestreo()),
          "debe avisar si Ts es enorme")
    cfg.tiempo_muestreo = 60.0

    # ASIMILACION: el MODELO debe converger al LECHO REAL.
    # Se montan DOS lechos: uno hace de realidad (es lo que leen los sensores) y
    # otro es el modelo que corrige el asimilador. Si los sensores leyeran el
    # propio modelo habria realimentacion y la prueba no valdria de nada.
    real = MultiZoneBed(BedParams(init_moisture=0.75, evap_base_rate=5.0e-5))
    modelo = MultiZoneBed(BedParams(init_moisture=0.45))
    cfg2 = ConfigCampo.por_defecto(modelo.n_zones)
    cfg2.puerto = "simulado"
    cfg2.ganancia = 0.4
    bus2 = BusCampo(cfg2, esclavos_simulados=banco_simulado(real, cfg2, ruido=0.0))
    bus2.conectar()
    asi = Asimilador(modelo, cfg2)
    antes = abs(modelo.zone_state(0)["moisture"] - real.zone_state(0)["moisture"])
    for _ in range(15):
        asi.asimilar(bus2.ciclo())
    despues = abs(modelo.zone_state(0)["moisture"] - real.zone_state(0)["moisture"])
    check(despues < antes * 0.05,
          f"el modelo debe converger al lecho real: la diferencia paso de "
          f"{antes:.3f} a {despues:.3f}")
    check(0 in modelo.medidas and "conductividad" in modelo.medidas[0],
          "las medidas deben quedar publicadas en el modelo")
    check(modelo.zone_state(0)["medida"] is not None,
          "zone_state debe exponer la medida del sensor de la zona")

    # Modo 'medida': sustitucion directa en una sola muestra.
    modelo2 = MultiZoneBed(BedParams(init_moisture=0.45))
    cfg2b = ConfigCampo.por_defecto(modelo2.n_zones)
    cfg2b.puerto, cfg2b.modo = "simulado", "medida"
    bus2b = BusCampo(cfg2b, esclavos_simulados=banco_simulado(real, cfg2b, ruido=0.0))
    bus2b.conectar()
    Asimilador(modelo2, cfg2b).asimilar(bus2b.ciclo())
    # La tolerancia no puede ser cero: el sensor entrega la humedad volumetrica
    # en decimas de punto (resolucion 0,1 % VWC), lo que con VWC_fc = 35 % son
    # 0,0029 de humedad relativa. Ese es el suelo de precision del sistema.
    resolucion = 0.1 / cfg2b.sensores[0].vwc_capacidad_campo
    check(abs(modelo2.zone_state(0)["moisture"] -
              real.zone_state(0)["moisture"]) <= 2 * resolucion,
          f"en modo 'medida' una muestra debe igualar el modelo a la medida "
          f"(dentro de la resolucion del sensor, {resolucion:.4f})")

    # Modo 'solo_lectura' no debe tocar el estado.
    sim3 = MultiZoneBed(BedParams(init_moisture=0.50))
    cfg3 = ConfigCampo.por_defecto(sim3.n_zones)
    cfg3.puerto, cfg3.modo = "simulado", "solo_lectura"
    bus3 = BusCampo(cfg3, esclavos_simulados=banco_simulado(sim3, cfg3))
    bus3.conectar()
    asi3 = Asimilador(sim3, cfg3)
    w_antes = list(sim3.W)
    asi3.asimilar(bus3.ciclo())
    check(sim3.W == w_antes, "en 'solo_lectura' el modelo no debe modificarse")

    # MODO DE LECTURA: hay unidades que en bloque devuelven 0 en algunos
    # canales y de una en una responden bien (es lo que hace el script de
    # referencia del usuario). Debe detectarse solo.
    class _Quisquilloso(EsclavoSimulado):
        def antes_de_leer(self, d, n):
            if n > 1:
                self.registros[0x0000] = 0
                self.registros[0x0002] = 0
            else:
                self.registros[0x0000] = 312
                self.registros[0x0002] = 850

    from modbus_rtu import MaestroModbus as _MM
    esc = _Quisquilloso(6, {0x0000: 312, 0x0001: 264, 0x0002: 850, 0x0003: 65})
    mq = _MM(TransporteSimulado({6: esc}), timeout=0.05, reintentos=0)
    sq = SensorSuelo(ConfigSensor(zona=0, direccion=6))
    lq = sq.leer(mq)
    check(sq.modo_efectivo == "individual",
          f"debe detectar que este sensor necesita lectura individual "
          f"(eligio {sq.modo_efectivo})")
    check(lq and abs(lq["vwc"] - 31.2) < 0.01 and lq["conductividad"] == 850,
          f"con lectura individual deben salir todos los canales: {lq}")
    check(not lq["sin_contacto"], "no debe marcarse sin contacto si hay medida")
    esc2 = EsclavoSimulado(7, {0x0000: 312, 0x0001: 264, 0x0002: 850, 0x0003: 65})
    s2 = SensorSuelo(ConfigSensor(zona=1, direccion=7))
    s2.leer(_MM(TransporteSimulado({7: esc2}), timeout=0.05, reintentos=0))
    check(s2.modo_efectivo == "bloque",
          "con un sensor coherente debe quedarse en lectura en bloque")
    s3 = SensorSuelo(ConfigSensor(zona=2, direccion=7, modo_lectura="individual"))
    check(s3.modo_efectivo == "individual",
          "el modo forzado por configuracion debe respetarse sin sondear")

    # Guardar y cargar la configuracion.
    ruta = "campo_prueba.json"
    cfg.guardar(ruta)
    otra = ConfigCampo.cargar(ruta)
    check(otra is not None and len(otra.sensores) == len(cfg.sensores)
          and otra.sensores[1].direccion == cfg.sensores[1].direccion
          and otra.actuador.direccion == cfg.actuador.direccion,
          "la configuracion debe sobrevivir al guardado y la carga")
    os.remove(ruta)

    # Cambio de direccion de un sensor.
    bus.asignar_direccion(1, 5)
    check(bus.maestro.tr.esclavos[1].registros[REG_DIRECCION] == 5,
          "asignar_direccion debe escribir el registro 0x07D0")

    if fallos:
        print("HARDWARE - FALLOS:")
        for f in fallos:
            print("  -", f)
        return 1
    print("HARDWARE - autopruebas correctas.")
    return 0


def demo(segundos=20.0):
    """Lazo completo contra el banco simulado: sondeo, asimilacion y valvulas."""
    from vermicompost_mz import MultiZoneBed, BedParams, HisteresisZona, \
        flags_por_defecto
    # 'real' hace de lecho fisico (con parametros distintos a proposito: es lo
    # que pasa siempre) y 'sim' es el modelo que se corrige con los sensores.
    real = MultiZoneBed(BedParams(init_moisture=0.72, evap_base_rate=4.5e-5,
                                  lateral_coeff=1.2e-5))
    sim = MultiZoneBed(BedParams(init_moisture=0.45))
    lock = threading.Lock()
    flags = flags_por_defecto(sim.n_zones)
    cfg = ConfigCampo.por_defecto(sim.n_zones)
    cfg.puerto, cfg.tiempo_muestreo, cfg.ganancia = "simulado", 2.0, 0.4
    bus = BusCampo(cfg, log=print,
                   esclavos_simulados=banco_simulado(real, cfg, ruido=0.4))
    bus.conectar()
    asi = Asimilador(sim, cfg, log=print)
    lazo = LazoCampo(sim, lock, flags, bus, asi, log=print).start()
    ctrl = [HisteresisZona() for _ in range(sim.n_zones)]
    t0 = time.time()
    try:
        while time.time() - t0 < segundos:
            time.sleep(0.05)
            with lock:
                st = sim.get_state(ligero=True)
                for i, z in enumerate(st["zones"]):
                    sim.set_level(i, ctrl[i].decidir(z["moisture"]))
                for _ in range(5):
                    sim.step()
            # el lecho "real" avanza con las mismas valvulas
            real.set_levels(sim.levels)
            for _ in range(5):
                real.step()
            if int(time.time() - t0) % 4 == 0:
                with lock:
                    st = sim.get_state(ligero=True)
                    med = dict(sim.medidas)
                print(f"t={time.time()-t0:5.1f}s  " + "  ".join(
                    f"Z{i}: real {real.zone_state(i)['moisture']:.3f} / sensor "
                    f"{med.get(i, {}).get('humedad_rel', float('nan')):.3f} / "
                    f"modelo {z['moisture']:.3f}"
                    for i, z in enumerate(st["zones"])))
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    lazo.stop()
    print("\n--- resumen del bus ---")
    print(json.dumps({k: v for k, v in bus.resumen().items()
                      if k != "sensores"}, indent=2, default=str))
    print("--- asimilacion ---")
    print(json.dumps(asi.resumen(), indent=2))
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Modulo de campo: sensores y valvulas.")
    ap.add_argument("--test", action="store_true", help="autopruebas (sin hardware)")
    ap.add_argument("--demo", type=float, nargs="?", const=20.0, metavar="SEG",
                    help="lazo completo contra el banco simulado")
    ap.add_argument("--puertos", action="store_true",
                    help="lista los puertos serie disponibles")
    ap.add_argument("--leer", metavar="PUERTO",
                    help="lectura continua por consola (equivale al script de "
                         "referencia); usar con --dirs")
    ap.add_argument("--dirs", default="1,2,3",
                    help="direcciones Modbus separadas por comas, p. ej. 4,5,6")
    ap.add_argument("--intervalo", type=float, default=2.0,
                    help="segundos entre rondas de lectura")
    ap.add_argument("--escanear", metavar="PUERTO",
                    help="escanea el bus real buscando dispositivos")
    ap.add_argument("--baudios", type=int, default=4800)
    ap.add_argument("--hasta", type=int, default=32)
    ap.add_argument("--crear-config", action="store_true",
                    help=f"escribe un {RUTA_CONFIG} de ejemplo")
    ap.add_argument("--zonas", type=int, default=3)
    ap.add_argument("--npk", action="store_true", help="leer tambien N, P y K")
    args = ap.parse_args(argv)

    if args.test:
        return selftest()
    if args.demo:
        return demo(args.demo)
    if args.puertos:
        puertos = TransporteSerie.puertos_disponibles()
        if not puertos:
            print("No se ve ningun puerto serie. ¿Esta enchufado el adaptador "
                  "USB-RS485 y su driver instalado?")
            return 1
        for p, d in puertos:
            print(f"  {p}   {d}")
        return 0
    if args.crear_config:
        cfg = ConfigCampo.por_defecto(args.zonas)
        ruta = cfg.guardar()
        print(f"Escrito {ruta}. Edita las direcciones y la humedad volumetrica "
              f"en capacidad de campo de cada zona.")
        return 0
    if args.leer:
        dirs = [int(x) for x in args.dirs.split(",") if x.strip()]
        cfg = ConfigCampo(puerto=args.leer, baudios=args.baudios)
        cfg.sensores = [ConfigSensor(zona=i, direccion=d, etiqueta=f"dir {d}",
                                     leer_npk=args.npk)
                        for i, d in enumerate(dirs)]
        cfg.actuador.activo = False
        simulados = None
        if args.leer == "simulado":
            from vermicompost_mz import MultiZoneBed, BedParams
            lecho = MultiZoneBed(BedParams(bed_length=2.0 * len(dirs),
                                           init_moisture=0.62))
            simulados = banco_simulado(lecho, cfg, ruido=0.4)
        bus = BusCampo(cfg, log=print, esclavos_simulados=simulados)
        bus.conectar()
        print(f"Leyendo {dirs} en {args.leer} a {args.baudios} bps. Ctrl+C para "
              f"parar.\n")
        try:
            while True:
                print(f"--- Ronda {time.strftime('%H:%M:%S')} ---")
                for s_ in bus.sensores:
                    lec = s_.leer(bus.maestro, cfg.pausa_entre_sensores)
                    if lec is None:
                        print(f"  [dir {s_.cfg.direccion}] sin respuesta: "
                              f"{s_.ultimo_error}")
                        continue
                    extra = ""
                    if lec.get("nitrogeno") is not None:
                        extra = (f" | N:{lec['nitrogeno']} P:{lec['fosforo']} "
                                 f"K:{lec['potasio']} mg/kg")
                    print(f"  [dir {s_.cfg.direccion}] Humedad: {lec['vwc']:.1f}% | "
                          f"Temp: {lec['temperatura']:.1f}C | "
                          f"CE: {lec['conductividad']:.0f}uS/cm | "
                          f"pH: {lec['ph']:.1f}{extra}"
                          f"   [{s_.modo_efectivo}]")
                    if s_.aviso_modo:
                        print(f"      {s_.aviso_modo}")
                        s_.aviso_modo = ""
                    for a in lec["avisos"]:
                        print(f"      aviso: {a}")
                print()
                time.sleep(args.intervalo)
        except KeyboardInterrupt:
            print("\nDetenido.")
        finally:
            bus.cerrar()
        return 0
    if args.escanear:
        cfg = ConfigCampo(puerto=args.escanear, baudios=args.baudios)
        bus = BusCampo(cfg, log=print)
        bus.conectar()
        print(f"Escaneando direcciones 1..{args.hasta} a {args.baudios} baudios...")
        hallados = bus.escanear(1, args.hasta,
                                progreso=lambda u, a, b: print(f"  ...{u}", end="\r"))
        print(" " * 20)
        if not hallados:
            print("Nada. Revisa: A/B sin invertir, alimentacion 4,5-30 V, "
                  "velocidad (prueba 2400 y 9600) y la terminacion de 120 Ω.")
        for h in hallados:
            print(f"  direccion {h['direccion']:3d}  {h['tipo']}  {h['datos']}")
        bus.cerrar()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
