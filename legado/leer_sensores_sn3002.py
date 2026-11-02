import time
import serial
import minimalmodbus

# ---------------------------------------------------------------
# CONFIGURACION - AJUSTA ESTO ANTES DE CORRER
# ---------------------------------------------------------------
PUERTO = 'COM7'                    # Windows: 'COM3', 'COM5'...  |  Linux/Mac: '/dev/ttyUSB0'
DIRECCIONES_ESCLAVOS = [4, 5, 6]   # Direcciones Modbus ya asignadas a cada sensor
BAUDRATE = 4800                    
INTERVALO_S = 2                    # segundos entre rondas de lectura completas
PAUSA_ENTRE_ESCLAVOS_S = 0.05      # pequena pausa entre sensores para dar tiempo al bus
# ---------------------------------------------------------------


def conectar_sensor(direccion):
    sensor = minimalmodbus.Instrument(PUERTO, direccion, mode=minimalmodbus.MODE_RTU)
    sensor.serial.baudrate = BAUDRATE
    sensor.serial.bytesize = 8
    sensor.serial.parity = serial.PARITY_NONE
    sensor.serial.stopbits = 1
    sensor.serial.timeout = 1
    sensor.clear_buffers_before_each_transaction = True
    return sensor


def leer_sensor(sensor):
    """Lee los 7 parametros. Cada uno se intenta por separado para que un
    fallo puntual (ruido, timeout) no tumbe la lectura completa."""
    datos = {}

    try:
        datos['humedad_%'] = sensor.read_register(0x0000, number_of_decimals=1, functioncode=3)
    except Exception as e:
        datos['humedad_%'] = None
        print(f"    [!] Error leyendo humedad: {e}")

    try:
        datos['temperatura_C'] = sensor.read_register(0x0001, number_of_decimals=1, functioncode=3, signed=True)
    except Exception as e:
        datos['temperatura_C'] = None
        print(f"    [!] Error leyendo temperatura: {e}")

    try:
        datos['conductividad_uScm'] = sensor.read_register(0x0002, number_of_decimals=0, functioncode=3)
    except Exception as e:
        datos['conductividad_uScm'] = None
        print(f"    [!] Error leyendo conductividad: {e}")

    try:
        datos['ph'] = sensor.read_register(0x0003, number_of_decimals=1, functioncode=3)
    except Exception as e:
        datos['ph'] = None
        print(f"    [!] Error leyendo pH: {e}")

    try:
        n, p, k = sensor.read_registers(0x0004, 3, functioncode=3)
        datos['N_mgkg'], datos['P_mgkg'], datos['K_mgkg'] = n, p, k
    except Exception as e:
        datos['N_mgkg'] = datos['P_mgkg'] = datos['K_mgkg'] = None
        print(f"    [!] Error leyendo NPK: {e}")

    return datos


def main():
    print(f"Conectando en {PUERTO} @ {BAUDRATE} bps.")
    print(f"Esclavos a leer: {DIRECCIONES_ESCLAVOS}")
    print("Listo. Ctrl+C para detener.\n")

    # Un objeto Instrument por esclavo (mismo puerto serie subyacente,
    # minimalmodbus lo maneja bien reabriendo/reutilizando segun direccion)
    sensores = {direccion: conectar_sensor(direccion) for direccion in DIRECCIONES_ESCLAVOS}

    try:
        while True:
            t = time.strftime('%H:%M:%S')
            print(f"--- Ronda {t} ---")
            for direccion, sensor in sensores.items():
                d = leer_sensor(sensor)
                print(f"  [Esclavo {direccion}] Humedad: {d['humedad_%']}%RH | "
                    f"Temp: {d['temperatura_C']}°C | "
                    f"CE: {d['conductividad_uScm']}uS/cm | pH: {d['ph']} | "
                    f"N:{d['N_mgkg']} P:{d['P_mgkg']} K:{d['K_mgkg']} mg/kg")
                time.sleep(PAUSA_ENTRE_ESCLAVOS_S)
            print()
            time.sleep(INTERVALO_S)
    except KeyboardInterrupt:
        print("\nLectura detenida por el usuario.")
    finally:
        for sensor in sensores.values():
            sensor.serial.close()


if __name__ == '__main__':
    main()
