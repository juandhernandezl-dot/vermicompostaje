#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modbus_rtu.py  -  MAESTRO MODBUS RTU SOBRE RS-485
=================================================

Implementacion minima y autocontenida del maestro Modbus RTU que necesita el
sistema: leer los sensores de suelo SN-3002-TR (registros de retencion) y
mandar las electrovalvulas a traves del ESP32 (bobinas).

Se escribe a mano en vez de usar una libreria porque asi el codigo documenta
exactamente lo que va por el cable, que es lo que hace falta cuando algo no
responde. Validado contra el ejemplo del propio datasheet:

    consulta   01 03 00 00 00 04 44 09
    respuesta  01 03 08 0292 FF9B 03E8 0038 57 B6
    -> humedad 65,8 %   temp -10,1 C   EC 1000 uS/cm   pH 5,6

PARAMETROS DEL BUS (fijados por el sensor)
------------------------------------------
    8 bits de datos, SIN paridad, 1 bit de parada, CRC-16 (poli 0xA001)
    2400 / 4800 / 9600 baudios; el sensor sale de fabrica a 4800
    direccion 1..254, de fabrica 1
    codigos de funcion usados: 0x03 (leer), 0x06 (escribir uno),
                               0x10 (escribir varios), 0x01/0x05/0x0F (bobinas)
    el byte ALTO de cada dato de 16 bits va primero (big endian)
    el CRC va con el byte BAJO primero

SILENCIO ENTRE TRAMAS
---------------------
Modbus RTU delimita las tramas por tiempo: hacen falta 3,5 tiempos de caracter
de silencio antes y despues. A 4800 baudios un caracter (11 bits con arranque y
parada) son 2,29 ms, luego el silencio es de 8 ms. Se respeta explicitamente
porque en RS-485 de dos hilos el adaptador USB tarda en cambiar de sentido y sin
esa pausa se pierden respuestas.

TRANSPORTES
-----------
  TransporteSerie     : puerto real (necesita pyserial).
  TransporteSimulado  : bus virtual con esclavos de mentira, para desarrollar y
                        probar TODA la cadena sin hardware conectado.
"""

import struct
import threading
import time

try:
    import serial
    from serial.tools import list_ports
    _HAY_SERIAL = True
except ImportError:      # pragma: no cover
    serial = None
    list_ports = None
    _HAY_SERIAL = False


# =============================================================================
# CRC-16 MODBUS
# =============================================================================

def crc16(datos: bytes) -> int:
    """CRC-16 de Modbus (polinomio invertido 0xA001, valor inicial 0xFFFF)."""
    crc = 0xFFFF
    for b in datos:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def con_crc(cuerpo: bytes) -> bytes:
    """Anade el CRC al final, byte BAJO primero (como manda Modbus RTU)."""
    c = crc16(cuerpo)
    return cuerpo + bytes((c & 0xFF, c >> 8))


def crc_valido(trama: bytes) -> bool:
    return len(trama) > 2 and crc16(trama[:-2]) == (trama[-1] << 8 | trama[-2])


def hexa(datos: bytes) -> str:
    return " ".join(f"{b:02X}" for b in datos)


# =============================================================================
# EXCEPCIONES
# =============================================================================

EXCEPCIONES = {
    1: "funcion ilegal",
    2: "direccion de datos ilegal",
    3: "valor de datos ilegal",
    4: "fallo del dispositivo esclavo",
    5: "reconocimiento (esclavo ocupado, reintentar)",
    6: "esclavo ocupado",
    7: "reconocimiento negativo",
    8: "error de paridad de memoria",
}


class ErrorModbus(Exception):
    """Fallo de comunicacion o excepcion devuelta por el esclavo."""


class SinRespuesta(ErrorModbus):
    """El esclavo no contesto dentro del tiempo de espera."""


class ExcepcionModbus(ErrorModbus):
    def __init__(self, unidad, funcion, codigo):
        self.unidad, self.funcion, self.codigo = unidad, funcion, codigo
        super().__init__(f"esclavo {unidad}, funcion 0x{funcion:02X}: "
                         f"excepcion {codigo} ({EXCEPCIONES.get(codigo, 'desconocida')})")


# =============================================================================
# TRANSPORTES
# =============================================================================

class TransporteSerie:
    """Puerto serie real a traves del adaptador USB-RS485."""

    def __init__(self, puerto, baudios=4800, timeout=0.6):
        if not _HAY_SERIAL:
            raise ErrorModbus(
                "pyserial no esta instalado. Instalalo con:  pip install pyserial")
        self.puerto_nombre = puerto
        self.baudios = baudios
        self.ser = serial.Serial(
            port=puerto, baudrate=baudios,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=timeout)
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    @property
    def abierto(self):
        return self.ser is not None and self.ser.is_open

    def limpiar(self):
        if self.abierto:
            self.ser.reset_input_buffer()

    def escribir(self, datos: bytes):
        self.ser.write(datos)
        self.ser.flush()

    def leer(self, n, timeout):
        fin = time.monotonic() + timeout
        buf = b""
        while len(buf) < n and time.monotonic() < fin:
            trozo = self.ser.read(n - len(buf))
            if trozo:
                buf += trozo
            else:
                time.sleep(0.002)
        return buf

    def cerrar(self):
        try:
            if self.abierto:
                self.ser.close()
        except Exception:
            pass

    @staticmethod
    def puertos_disponibles():
        if not _HAY_SERIAL:
            return []
        return [(p.device, p.description or "") for p in list_ports.comports()]


class TransporteSimulado:
    """
    Bus virtual: mismos bytes, mismas tramas, sin cable.

    Permite desarrollar y probar el modulo de campo completo (lectura de
    sensores, escaneo de direcciones, mando de valvulas) antes de tener el
    hardware delante, y sirve de banco de pruebas automatico.
    """

    def __init__(self, esclavos=None, retardo=0.0, perdida=0.0, medio_duplex=True):
        self.esclavos = dict(esclavos or {})   # unidad -> objeto con procesar()
        self.retardo = retardo                 # s de retardo simulado por trama
        self.perdida = perdida                 # probabilidad de perder la trama
        self.medio_duplex = medio_duplex       # imita el atropello de dos hilos
        self.atropellos = 0
        self._pendiente = b""
        self.baudios = 4800
        self.puerto_nombre = "simulado"
        self.abierto = True
        self.tramas = []                       # historial (peticion, respuesta)

    def limpiar(self):
        self._pendiente = b""

    def escribir(self, datos: bytes):
        import random
        if self.medio_duplex and self._pendiente:
            # Alguien escribio sin haber leido la respuesta anterior: en un bus
            # real de dos hilos eso es exactamente lo que pasa cuando dos hilos
            # comparten el puerto, y la respuesta se pierde o se mezcla.
            self.atropellos += 1
        self._pendiente = b""
        # Pausa minima para imitar el tiempo que tarda la trama en salir por el
        # cable. Ademas suelta el GIL, que es lo que hace que este banco de
        # pruebas sea capaz de reproducir las carreras entre hilos (con un
        # puerto real las hay siempre, porque la E/S bloquea).
        time.sleep(self.retardo if self.retardo else 0.0005)
        if self.perdida and random.random() < self.perdida:
            self.tramas.append((datos, b""))
            return
        unidad = datos[0] if datos else 0
        esclavo = self.esclavos.get(unidad)
        respuesta = b"" if esclavo is None else esclavo.procesar(datos)
        self._pendiente = respuesta
        self.tramas.append((datos, respuesta))

    def leer(self, n, timeout):
        trozo, self._pendiente = self._pendiente[:n], self._pendiente[n:]
        return trozo

    def cerrar(self):
        self.abierto = False

    @staticmethod
    def puertos_disponibles():
        return [("simulado", "bus virtual de pruebas")]


# =============================================================================
# MAESTRO
# =============================================================================

class MaestroModbus:
    """Maestro Modbus RTU. Una instancia por bus (por puerto)."""

    def __init__(self, transporte, timeout=0.6, reintentos=2, log=None):
        self.tr = transporte
        self.timeout = timeout
        self.reintentos = reintentos
        self.log = log or (lambda m: None)
        # RS-485 de dos hilos es medio duplex: una transaccion (escribir la
        # peticion y leer la respuesta) NO se puede partir. Si el hilo del lazo
        # de campo y el de la interfaz usan el puerto a la vez, uno escribe
        # mientras el otro espera y las respuestas se cruzan: el sintoma es que
        # "solo funciona la primera lectura". El candado lo impide.
        self._candado = threading.Lock()
        self.estadisticas = {"peticiones": 0, "fallos": 0, "timeouts": 0,
                             "crc_malo": 0, "excepciones": 0, "reintentos": 0}

    # ---- temporizacion -------------------------------------------------------
    @property
    def silencio(self):
        """3,5 tiempos de caracter (11 bits por caracter), minimo 2 ms."""
        b = getattr(self.tr, "baudios", 4800) or 4800
        return max(0.002, 3.5 * 11.0 / b)

    def tiempo_trama(self, n_bytes):
        b = getattr(self.tr, "baudios", 4800) or 4800
        return n_bytes * 11.0 / b

    # ---- nucleo --------------------------------------------------------------
    def _transaccion(self, unidad, cuerpo, n_esperados):
        """Envia una peticion y devuelve la respuesta cruda ya validada.
        Es atomica: el candado cubre peticion y respuesta juntas."""
        with self._candado:
            return self._transaccion_sin_candado(unidad, cuerpo, n_esperados)

    def _transaccion_sin_candado(self, unidad, cuerpo, n_esperados):
        peticion = con_crc(bytes([unidad]) + cuerpo)
        ultimo = None
        for intento in range(self.reintentos + 1):
            if intento:
                self.estadisticas["reintentos"] += 1
                time.sleep(self.silencio * 4)
            self.estadisticas["peticiones"] += 1
            self.tr.limpiar()
            time.sleep(self.silencio)
            self.tr.escribir(peticion)
            self.log(f"TX {hexa(peticion)}")
            espera = self.timeout + self.tiempo_trama(n_esperados + 4)
            resp = self.tr.leer(n_esperados, espera)
            self.log(f"RX {hexa(resp) if resp else '(nada)'}")
            if not resp:
                self.estadisticas["timeouts"] += 1
                ultimo = SinRespuesta(
                    f"el esclavo {unidad} no respondio en {espera*1000:.0f} ms")
                continue
            # Respuesta de excepcion: funcion con el bit alto puesto, 5 bytes.
            if len(resp) >= 5 and resp[1] & 0x80:
                trama = resp[:5]
                if crc_valido(trama):
                    self.estadisticas["excepciones"] += 1
                    raise ExcepcionModbus(resp[0], resp[1] & 0x7F, resp[2])
            if len(resp) < n_esperados:
                # completar por si el adaptador entrego la trama a trozos
                resp += self.tr.leer(n_esperados - len(resp), self.timeout)
            if len(resp) < n_esperados:
                self.estadisticas["timeouts"] += 1
                ultimo = SinRespuesta(
                    f"respuesta incompleta del esclavo {unidad}: "
                    f"{len(resp)} de {n_esperados} bytes ({hexa(resp)})")
                continue
            resp = resp[:n_esperados]
            if not crc_valido(resp):
                self.estadisticas["crc_malo"] += 1
                ultimo = ErrorModbus(f"CRC incorrecto del esclavo {unidad}: "
                                     f"{hexa(resp)}")
                continue
            if resp[0] != unidad:
                ultimo = ErrorModbus(f"respondio la unidad {resp[0]} en vez de "
                                     f"la {unidad}")
                continue
            return resp
        self.estadisticas["fallos"] += 1
        raise ultimo if ultimo else ErrorModbus("fallo desconocido")

    # ---- funciones -----------------------------------------------------------
    def leer_registros(self, unidad, direccion, cantidad):
        """0x03 - leer registros de retencion. Devuelve lista de enteros 0..65535."""
        if not 1 <= cantidad <= 125:
            raise ValueError("cantidad debe estar entre 1 y 125")
        cuerpo = struct.pack(">BHH", 0x03, direccion, cantidad)
        # unidad + funcion + n_bytes + datos + crc
        resp = self._transaccion(unidad, cuerpo, 5 + 2 * cantidad)
        n = resp[2]
        if n != 2 * cantidad:
            raise ErrorModbus(f"el esclavo devolvio {n} bytes, se esperaban "
                              f"{2 * cantidad}")
        return list(struct.unpack(f">{cantidad}H", resp[3:3 + n]))

    def escribir_registro(self, unidad, direccion, valor):
        """0x06 - escribir un registro de retencion."""
        cuerpo = struct.pack(">BHH", 0x06, direccion, valor & 0xFFFF)
        resp = self._transaccion(unidad, cuerpo, 8)
        if resp[2:6] != cuerpo[1:5]:
            raise ErrorModbus("el eco de la escritura no coincide")
        return True

    def escribir_registros(self, unidad, direccion, valores):
        """0x10 - escribir varios registros de retencion."""
        vals = list(valores)
        cuerpo = (struct.pack(">BHHB", 0x10, direccion, len(vals), 2 * len(vals))
                  + struct.pack(f">{len(vals)}H", *(v & 0xFFFF for v in vals)))
        self._transaccion(unidad, cuerpo, 8)
        return True

    def leer_bobinas(self, unidad, direccion, cantidad):
        """0x01 - leer estado de bobinas. Devuelve lista de bool."""
        n_bytes = (cantidad + 7) // 8
        cuerpo = struct.pack(">BHH", 0x01, direccion, cantidad)
        resp = self._transaccion(unidad, cuerpo, 5 + n_bytes)
        crudos = resp[3:3 + resp[2]]
        return [bool(crudos[i // 8] >> (i % 8) & 1) for i in range(cantidad)]

    def escribir_bobina(self, unidad, direccion, valor):
        """0x05 - forzar una bobina."""
        cuerpo = struct.pack(">BHH", 0x05, direccion, 0xFF00 if valor else 0x0000)
        self._transaccion(unidad, cuerpo, 8)
        return True

    def escribir_bobinas(self, unidad, direccion, valores):
        """0x0F - forzar varias bobinas de una vez (una sola trama para todas
        las valvulas: mas rapido y sin estados intermedios raros)."""
        vals = list(valores)
        n_bytes = (len(vals) + 7) // 8
        crudos = bytearray(n_bytes)
        for i, v in enumerate(vals):
            if v:
                crudos[i // 8] |= 1 << (i % 8)
        cuerpo = (struct.pack(">BHHB", 0x0F, direccion, len(vals), n_bytes)
                  + bytes(crudos))
        self._transaccion(unidad, cuerpo, 8)
        return True

    # ---- utilidades ----------------------------------------------------------
    def presente(self, unidad, direccion=0x0000, cantidad=1):
        """¿Contesta algo en esa direccion? Una excepcion tambien cuenta como
        presente: el dispositivo esta ahi, solo que ese registro no le gusta."""
        try:
            self.leer_registros(unidad, direccion, cantidad)
            return True
        except ExcepcionModbus:
            return True
        except ErrorModbus:
            return False

    def escanear(self, desde=1, hasta=32, direccion=0x0000, progreso=None):
        """Recorre direcciones buscando dispositivos. Imprescindible cuando no
        se sabe (o no se recuerda) que direccion tiene cada sensor."""
        encontrados = []
        for u in range(desde, hasta + 1):
            if progreso:
                progreso(u, desde, hasta)
            if self.presente(u, direccion):
                encontrados.append(u)
        return encontrados

    def cerrar(self):
        self.tr.cerrar()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cerrar()


# =============================================================================
# ESCLAVOS SIMULADOS (para el TransporteSimulado)
# =============================================================================

class EsclavoSimulado:
    """Esclavo Modbus de mentira: responde tramas reales sobre un diccionario
    de registros y una lista de bobinas."""

    def __init__(self, unidad, registros=None, bobinas=0):
        self.unidad = unidad
        self.registros = dict(registros or {})
        self.bobinas = [False] * bobinas

    # --- ganchos que puede sobrescribir una subclase ---
    def antes_de_leer(self, direccion, cantidad):
        pass

    def al_escribir(self, direccion, valor):
        pass

    def procesar(self, trama: bytes) -> bytes:
        if not crc_valido(trama) or trama[0] != self.unidad:
            return b""
        f = trama[1]
        try:
            if f == 0x03:
                d, n = struct.unpack(">HH", trama[2:6])
                self.antes_de_leer(d, n)
                vals = [self.registros.get(d + i) for i in range(n)]
                if any(v is None for v in vals):
                    return self._excepcion(f, 2)
                cuerpo = (bytes([self.unidad, 0x03, 2 * n])
                          + struct.pack(f">{n}H", *(v & 0xFFFF for v in vals)))
                return con_crc(cuerpo)
            if f == 0x06:
                d, v = struct.unpack(">HH", trama[2:6])
                self.registros[d] = v
                self.al_escribir(d, v)
                return con_crc(trama[:6])
            if f == 0x10:
                d, n = struct.unpack(">HH", trama[2:6])
                for i in range(n):
                    v = struct.unpack(">H", trama[7 + 2 * i:9 + 2 * i])[0]
                    self.registros[d + i] = v
                    self.al_escribir(d + i, v)
                return con_crc(trama[:6])
            if f == 0x01:
                d, n = struct.unpack(">HH", trama[2:6])
                if d + n > len(self.bobinas):
                    return self._excepcion(f, 2)
                nb = (n + 7) // 8
                crudos = bytearray(nb)
                for i in range(n):
                    if self.bobinas[d + i]:
                        crudos[i // 8] |= 1 << (i % 8)
                return con_crc(bytes([self.unidad, 0x01, nb]) + bytes(crudos))
            if f == 0x05:
                d, v = struct.unpack(">HH", trama[2:6])
                if d >= len(self.bobinas):
                    return self._excepcion(f, 2)
                self.bobinas[d] = (v == 0xFF00)
                return con_crc(trama[:6])
            if f == 0x0F:
                d, n = struct.unpack(">HH", trama[2:6])
                if d + n > len(self.bobinas):
                    return self._excepcion(f, 2)
                crudos = trama[7:7 + trama[6]]
                for i in range(n):
                    self.bobinas[d + i] = bool(crudos[i // 8] >> (i % 8) & 1)
                return con_crc(trama[:6])
        except (struct.error, IndexError):
            return self._excepcion(f, 3)
        return self._excepcion(f, 1)

    def _excepcion(self, funcion, codigo):
        return con_crc(bytes([self.unidad, funcion | 0x80, codigo]))


# =============================================================================
# AUTOPRUEBAS
# =============================================================================

def selftest():
    fallos = []

    def check(c, m):
        if not c:
            fallos.append(m)

    # 1) CRC y escalado exactamente como en el datasheet del SN-3002.
    c = crc16(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x04]))
    check((c & 0xFF, c >> 8) == (0x44, 0x09),
          f"la consulta del datasheet debe dar CRC 44 09 (dio {c&0xFF:02X} {c>>8:02X})")
    resp = bytes([0x01, 0x03, 0x08, 0x02, 0x92, 0xFF, 0x9B, 0x03, 0xE8, 0x00, 0x38,
                  0x57, 0xB6])
    check(crc_valido(resp), "la respuesta del datasheet debe validar el CRC")

    # 2) Ida y vuelta completa contra un esclavo simulado.
    esclavo = EsclavoSimulado(1, {0x0000: 0x0292, 0x0001: 0xFF9B,
                                  0x0002: 0x03E8, 0x0003: 0x0038})
    tr = TransporteSimulado({1: esclavo})
    m = MaestroModbus(tr, timeout=0.05, reintentos=0)
    vals = m.leer_registros(1, 0x0000, 4)
    check(vals == [0x0292, 0xFF9B, 0x03E8, 0x0038],
          f"lectura de 4 registros incorrecta: {vals}")
    check(struct.unpack(">h", struct.pack(">H", vals[1]))[0] / 10.0 == -10.1,
          "la temperatura negativa debe interpretarse en complemento a dos")
    peticion = tr.tramas[0][0]
    check(peticion == bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x04, 0x44, 0x09]),
          f"la trama enviada no es la del datasheet: {hexa(peticion)}")

    # 3) Escritura, excepciones y ausencia.
    m.escribir_registro(1, 0x07D0, 7)
    check(esclavo.registros[0x07D0] == 7, "0x06 debe escribir el registro")
    try:
        m.leer_registros(1, 0x9999, 1)
        fallos.append("leer un registro inexistente debe dar excepcion")
    except ExcepcionModbus as e:
        check(e.codigo == 2, f"deberia ser excepcion 2, fue {e.codigo}")
    try:
        m.leer_registros(9, 0x0000, 1)
        fallos.append("una unidad ausente debe dar SinRespuesta")
    except SinRespuesta:
        pass

    # 4) Bobinas (las electrovalvulas del ESP32).
    esp = EsclavoSimulado(10, {}, bobinas=4)
    m2 = MaestroModbus(TransporteSimulado({10: esp}), timeout=0.05, reintentos=0)
    m2.escribir_bobinas(10, 0, [True, False, True, False])
    check(esp.bobinas == [True, False, True, False],
          f"0x0F debe fijar todas las bobinas: {esp.bobinas}")
    check(m2.leer_bobinas(10, 0, 4) == [True, False, True, False],
          "0x01 debe releer el estado de las bobinas")
    m2.escribir_bobina(10, 1, True)
    check(esp.bobinas[1] is True, "0x05 debe forzar una bobina")

    # 5) Escaneo del bus.
    m3 = MaestroModbus(TransporteSimulado({3: EsclavoSimulado(3, {0: 1}),
                                           7: EsclavoSimulado(7, {0: 1})}),
                       timeout=0.02, reintentos=0)
    check(m3.escanear(1, 10) == [3, 7], "el escaneo debe encontrar las unidades 3 y 7")

    # 6) Reintentos ante perdida de tramas.
    tr4 = TransporteSimulado({1: EsclavoSimulado(1, {0: 5})}, perdida=0.5)
    m4 = MaestroModbus(tr4, timeout=0.02, reintentos=8)
    try:
        check(m4.leer_registros(1, 0, 1) == [5], "con reintentos debe acabar leyendo")
    except ErrorModbus as e:
        fallos.append(f"los reintentos no bastaron: {e}")

    # 7) Dos hilos compartiendo el bus: el candado debe serializar y ninguna
    #    respuesta debe cruzarse (era la causa de "solo va la primera lectura").
    tr5 = TransporteSimulado({1: EsclavoSimulado(1, {0: 111}),
                              2: EsclavoSimulado(2, {0: 222})})
    m5 = MaestroModbus(tr5, timeout=0.05, reintentos=0)
    errores = []

    def machacar(unidad, esperado, vueltas=80):
        for _ in range(vueltas):
            try:
                v = m5.leer_registros(unidad, 0, 1)
                if v != [esperado]:
                    errores.append(f"unidad {unidad} devolvio {v}")
            except ErrorModbus as e:
                errores.append(f"unidad {unidad}: {type(e).__name__}")

    hilos = [threading.Thread(target=machacar, args=(1, 111)),
             threading.Thread(target=machacar, args=(2, 222))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    check(not errores, f"dos hilos sobre el mismo bus deben coexistir: {errores[:3]}")
    check(tr5.atropellos == 0,
          f"no debe haber respuestas atropelladas ({tr5.atropellos})")

    if fallos:
        print("MODBUS - FALLOS:")
        for f in fallos:
            print("  -", f)
        return 1
    print("MODBUS - autopruebas correctas (incluido el ejemplo del datasheet).")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
