# Despliegue en la cama del huerto (paso 5)

Esta guía empieza cuando la [simulación en la Pi](GUIA_PI5.md) ya funciona completa. El orden importa: **cada etapa se prueba sin agua antes de la siguiente.**

## 1. Firmware del ESP32 (en el banco, sin válvulas)

1. Arduino IDE → placa **ESP32 Dev Module** → abre `firmware/actuador_valvulas/actuador_valvulas.ino` → *Subir*.
2. Monitor Serie a **115200**, con fin de línea **Nueva línea**. Escribe y comprueba:
   - `CONFIG` → `OK CONFIG N=3 MAX_PULSO=900 ...`
   - `ABRIR 1 5` → el relé 1 hace clic; a los 5 s, `EVT CERRADA 1 TIEMPO 5`
   - Sin escribir nada durante 60 s con un relé abierto → `EVT CERRADA ... WATCHDOG`
3. Si el relé se activa al revés, la placa es activa a nivel bajo: pon `RELE_ACTIVO_ALTO = false` y vuelve a subir.
4. Pulsa **EN** (reset) con un relé abierto: al arrancar todos deben quedar en reposo.

Cableado: GPIO 25, 26 y 27 → relés de las zonas 1, 2 y 3. Válvulas **normalmente cerradas** y con fuente propia. Deja un corte físico rotulado junto a la fuente de las válvulas.

## 2. Identificar los USB en la Pi

```bash
ls -l /dev/serial/by-id/
```

Debe haber dos entradas: el FTDI del RS‑485 y el CP2102 (o CH340) del ESP32. Copia las rutas completas a `.env` (`SENSORES_USB`, `ESP32_USB`).

Si no aparece nada, ejecuta `sudo dmesg -w`, enchufa el adaptador y mira si aparece `attached to ttyUSB...`. Si no aparece, el problema es físico: el cable puede ser solo de carga, prueba otro puerto o usa la fuente oficial de 27 W.

## 3. Sensores: direcciones 3, 4 y 6

Los tres SN‑3002 deben tener las direcciones **3, 4 y 6**, como en `control.json`. Se pueden comprobar con las herramientas que siguen en `~/vermicompostaje/despliegue` (con el stack apagado, para no pelear por el puerto):

```bash
cd ~/vermicompostaje/despliegue
python3 hardware_mz.py --escanear /dev/serial/by-id/usb-FTDI_...
```

Si algún canal sale en 0 al leer en bloque pero bien uno a uno, pon `MODO_LECTURA=individual` en `.env`.

## 4. Pasar a modo huerto

En `.env`:

```bash
COMPOSE_FILE=docker-compose.yml
```

Luego:

```bash
docker compose up -d
docker compose logs -f sensores actuadores
```

**Todas las zonas arrancan en `manual`**, así que nada riega solo.

## 5. Calibrar antes de automatizar

`vwc_cc` es el % VWC que mide el sensor en capacidad de campo. Por cada zona:

1. Satura una muestra del sustrato y déjala drenar entre 24 y 48 h, tapada.
2. Mide con el sensor: ese valor es su `vwc_cc`.
3. Ajusta en `servicios/control/control.json` el `vwc_cc` y la `banda` (% VWC) de la zona, y reconstruye: `docker compose up -d --build control`.

Hasta hacer esto, el RL trabaja con una conversión supuesta (`vwc_cc = 35`). Las lecturas de hoy pueden estar mal porque los sensores aún no están calibrados.

## 6. Encender el control zona por zona

1. **Manual:** prueba cada válvula con agua real, por ejemplo con el agente: `abre la válvula de la zona 1 por 1 minuto`.
2. **PI en una sola zona** durante un día: `pon la zona 2 en PI`. Revisa `db.decisiones` y el dashboard.
3. **RL** solo después: es una política entrenada en simulación y decide cada 300 s, no cada 60 s como en el entrenamiento. Úsala como experimento comparado con el PI.

## 7. Operación

- Respaldo nocturno: `crontab -e` → `15 2 * * * bash /home/gitcode1/vermicompostaje-unificado/scripts/respaldo_mongo.sh`.
- Sin internet (HAT sin señal): MongoDB, el control, el dashboard local y el agente siguen funcionando. Solo se pierde el envío MQTT al laboratorio, y los lotes se pueden pedir después.
- Parada de emergencia: `curl -s -X POST localhost:8100/cerrar_todo -H 'Content-Type: application/json' -d '{"origen":"operador"}'`, o decirle al agente «cierra todo».
