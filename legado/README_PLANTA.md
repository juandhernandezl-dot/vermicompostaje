# Aplicación de operación — cama de vermicompostaje

Esto **no es el simulador**. Es el sistema en funcionamiento: lee los sensores
reales, decide y abre o cierra las electroválvulas. Sin modelo físico, sin
aprendizaje, sin datos inventados. Lo que se ve en pantalla es lo que hay en el
lecho.

## Archivos

| Archivo | Qué es |
|---|---|
| `planta.py` | La aplicación: configuración, control, sinóptico. **Es lo único que hay que ejecutar.** |
| `hardware_mz.py` | Drivers del sensor SN-3002-TR y del actuador ESP32. |
| `modbus_rtu.py` | Maestro Modbus RTU sobre RS-485. |
| `esp32_valvulas.ino` | Firmware del ESP32. |

El simulador (`vermicompost_mz.py`, `gui_mz.py`, `rl_*.py`) queda aparte y no se
carga: la aplicación de operación no lo importa.

## Uso

```bash
python3 planta.py                 # interfaz
python3 planta.py --demo          # interfaz con un lecho de mentira, sin hardware
python3 planta.py --test          # autopruebas del control
python3 planta.py --consola COM7 --dirs 4,5,6 --muestreo 30
```

La configuración se guarda en `planta.json`.

## Todo en % de humedad volumétrica

El simulador trabaja en humedad relativa a capacidad de campo. Aquí **no**: se
trabaja en el % VWC que da el sensor, que es lo que se puede medir y comprobar.
La capacidad de campo queda solo como referencia informativa («0,72 de capacidad
de campo» bajo la cifra grande), no como parte del lazo. Una calibración mal
puesta ya no puede desplazar el control.

## Modos de control por zona

| Modo | Qué hace |
|---|---|
| **Apagado** | La válvula nunca se abre. |
| **Manual** | Manda el operador con el botón. Aun así cierra sola si se supera la banda alta. |
| **Automático** | Histéresis sobre la banda, con descanso mínimo y tope de riego. |
| **Externo** | El control local no toca esa válvula; queda para un proceso externo (por ejemplo un agente RL). |

Se cambia desde la propia fila de la zona, en marcha, sin parar la planta.

## Seguridades

Son independientes del modo y no se pueden desactivar desde la pantalla:

1. **Sin lectura fiable no se riega.** Si un sensor deja de responder, o la sonda
   no hace contacto, la zona se cierra tras N lecturas malas y queda en FALLO.
   Regar a ciegas es inundar el lecho con los ojos cerrados.
2. **Tope de riego continuo** por zona (10 min por defecto). Una válvula pegada o
   un sensor que nunca sube regarían para siempre.
3. **Descanso mínimo entre riegos** (5 min por defecto). El agua tarda en
   infiltrarse; sin esa espera el control persigue su propia estela y la válvula
   no para de conmutar.
4. **Muestreo mínimo de 1 s.** Sondear más rápido no aporta nada y castiga el bus.
5. Al parar la planta o cerrar la ventana, **todas las válvulas se cierran**.
6. El ESP32 tiene además su **tiempo de guarda por firmware**, que no depende de
   este programa: si el PC calla más de 120 s, cierra por su cuenta.

Cada zona muestra siempre **por qué** está como está («dentro de la banda»,
«seca, pero en descanso 3,2 min», «FALLO: tope de riego continuo»). En operación,
saber el motivo importa tanto como el estado.

## La pantalla

- **Planta** — sinóptico con una tarjeta por zona: columna de humedad con la
  banda objetivo marcada, cifra grande en % VWC, temperatura, aspersor que gotea
  cuando riega, contador de riegos y minutos acumulados, y el borde de la tarjeta
  en color según el estado (verde en banda, ámbar seca, azul húmeda, rojo fallo).
  Debajo, una fila por zona para cambiar modo, banda y mando manual, y una
  gráfica de la humedad reciente.
- **Configuración** — puertos, direcciones Modbus de cada sensor, asignación de
  válvulas, calibración, tope de riego y descanso. Botones de escaneo del bus y
  prueba de lectura.
- **Registro** — eventos con hora: cada apertura y cierre con su motivo, fallos,
  cambios de modo.

Arriba, siempre visible: estado, número de rondas, edad de la última lectura,
cuenta atrás de la siguiente, y el botón rojo **CERRAR TODO**.
