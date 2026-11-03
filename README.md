# Vermicompostaje automatizado — HuertoLab UAO

Control de humedad por zona de una cama de vermicompost: sensores de suelo SN‑3002 (Modbus RS‑485), 3 electroválvulas manejadas por un ESP32, control **RL / PI / MANUAL** por zona, dashboard en Node‑RED, agente conversacional local (Ollama, terminal) y un receptor de laboratorio por MQTT.

> **Estado (27 oct 2026):** todo el software está probado contra la **planta simulada**, que ejecuta el firmware real del ESP32 compilado para el PC. Faltan tres cosas: la prueba con hardware real, la ejecución de los contenedores en la Pi y la **calibración de los sensores**. Ver [Qué está verificado](#qué-está-verificado-y-qué-no).

## Qué corre dónde

```
            Raspberry Pi 5 (docker compose)                              PC del laboratorio
 ┌──────────────────────────────────────────────────────────────┐       ┌────────────────────┐
 │ sensores ──lecturas──► mongodb ◄──lee── control ──HTTP──► actuadores ──USB──► ESP32 ──► 3 válvulas
 │   │ USB-RS485 (SN-3002 dir. 3, 4, 6)        ▲  API :8100     (puente)          (actuador_valvulas.ino)
 │   │                                         │
 │   └──MQTT──► broker.hivemq.com ◄──MQTT── nodered (dashboard :1880, lotes) ──MQTT──► receptor_lab
 │                                           agente (terminal) + ollama                 │ (Ubuntu/Windows)
 └──────────────────────────────────────────────────────────────┘       └────────────────────┘
```

**Quién manda qué.** Cada recurso tiene un único dueño; nadie más lo toca:

| Recurso | Dueño único | Los demás… |
|---|---|---|
| Adaptador USB‑RS485 (sensores) | `sensores` | leen `lecturas` en MongoDB |
| ESP32 de las válvulas (USB) | `actuadores` (puente) | — |
| **Modo de cada zona** (manual / pi / rl) | **`control`** | piden por su API :8100 |
| Órdenes al puente | `control` | el agente y Node‑RED nunca le hablan al puente |
| Seguridad física | **firmware del ESP32** | tope por pulso, tope continuo y cierre por falta de latido, sin depender de la Pi |

## Un solo interruptor: huerto o simulación

En `.env`:

```bash
COMPOSE_FILE=docker-compose.yml                            # huerto (USB reales)
COMPOSE_FILE=docker-compose.yml:docker-compose.sim.yml     # simulación (sin hardware)
```

En modo simulación, `planta-simulada` sustituye a los USB por TCP: corre el **firmware real** del ESP32, imita 3 sensores Modbus y usa la física del simulador v3. Los servicios ejecutan el **mismo código** que en el huerto. Para que nunca se mezclen:

- la simulación usa otra base (`mongo-sim`);
- usa otro prefijo MQTT (`uao/vermicompost/sim`), y el receptor se abre con `--sim`;
- aparece un contenedor `planta-simulada` en `docker compose ps`.

## Estructura

```
docker-compose.yml        stack del huerto (Pi 5)
docker-compose.sim.yml    override: planta simulada en lugar de los USB
.env.example              modo, rutas de datos, USB, modelo del agente
servicios/
  sensores/               emisor Modbus → MongoDB + MQTT
  actuadores/             puente HTTP ↔ ESP32 (único dueño del USB) y registro de riegos
  control/                RL / PI / MANUAL por zona, árbitro del modo (control.json)
  agente/                 agente conversacional: terminal (chat.py) y página Asistente del dashboard (API interna /chat)
nodered/                  flujos del dashboard y lotes (imagen con los nodos ya instalados)
firmware/
  actuador_valvulas/      firmware del ESP32, compatible con el puente
  pruebas/                arnés para compilarlo y probarlo en el PC
simulador/                simulador multizona v3 (única copia), tablas RL, planta_simulada.py
receptor_lab/             receptor de laboratorio (PC): Linux y Windows
pruebas/                  prueba integrada del stack contra la planta simulada
scripts/                  respaldo de MongoDB
docs/                     guías y contratos
```

Los datos **no** están en el repo: MongoDB vive en `DATOS_DIR` (por defecto `../vermi-datos`). La arquitectura descartada (`planta.py`, `hardware_mz.py`, `modbus_rtu.py`, `esp32_valvulas.ino` Modbus y el `.ino` del PI con DAC) se guarda en la rama `legado`, no en `main`.

## Empezar

| Qué | Guía |
|---|---|
| Probar todo en la Pi 5 sin hardware (pasos 1, 2 y 4) | [docs/GUIA_PI5.md](docs/GUIA_PI5.md) |
| Receptor de laboratorio en Ubuntu 22.04 (paso 3) | [docs/GUIA_PC.md](docs/GUIA_PC.md) |
| Llevarlo al huerto (paso 5) | [docs/DESPLIEGUE_HUERTO.md](docs/DESPLIEGUE_HUERTO.md) |
| APIs, protocolo serie, colecciones y tópicos | [docs/CONTRATOS.md](docs/CONTRATOS.md) |
| Subir a GitHub (y la rama `legado`) | [docs/REPOSITORIO.md](docs/REPOSITORIO.md) |

Pruebas sin Docker ni hardware (en cualquier PC con Python ≥ 3.10 y g++):

```bash
pip install pymongo mongomock pyserial httpx paho-mqtt
bash pruebas/correr_todo.sh
```

## Qué está verificado y qué no

**Verificado (27 oct 2026, contra la planta simulada):**
- El firmware compila sin advertencias (`-Wall -Wextra`) y cumple el protocolo del puente: rechazo de órdenes inválidas, recorte a 900 s, cierre al terminar el pulso, cierre por falta de latido, tope continuo de 30 min con bloqueo y arranque con relés en reposo.
- Puente ↔ firmware: sincronización, apertura y cierre, registro de riegos en MongoDB, cierre de riegos pendientes tras un reinicio del ESP32.
- Sensores → MongoDB por Modbus, en lectura en bloque y una a una.
- Control: arbitraje (no se abre a mano una zona en PI/RL), PI y RL deciden y riegan, "sin lectura fiable no se riega", `cerrar_todo`, modos persistentes.
- Agente: confirmación SÍ/NO, rechazo de ambigüedad, consultas sin LLM, ruta al LLM con el estado fresco y `think: false`.
- Receptor: ejecutable de Linux, prefijo de simulación y compatibilidad con paho‑mqtt 1.x y 2.x.

**No verificado aún:**
- Contenedores construidos y corriendo en la Pi (aquí no hay Docker activo).
- Flujos de Node‑RED ejecutados.
- MQTT con HiveMQ.
- Ollama con qwen3.5:4b en la Pi.
- Hardware real: ESP32, relés, válvulas, sensores, HAT de comunicaciones.
- **Calibración**: `vwc_cc` (% VWC en capacidad de campo) y las bandas de cada zona.

## Riesgos conocidos

- **Unidades de humedad sin calibrar.** El RL se entrenó en W/W_fc (banda 0,60–0,70 ≈ 21–24,5 % VWC con `vwc_cc=35`). El PI usa la banda de `control.json` (25–32 % VWC). El dashboard marca como óptimo 70–85 %, un rango de humedad gravimétrica de la literatura que **no es comparable** con la VWC del sensor. Hay que unificarlo tras calibrar.
- **Cadencia del RL.** Las tablas se entrenaron decidiendo cada 60 s simulados. En el huerto el control decide cada 300 s, así que el RL en campo es una política fuera de su condición de entrenamiento: tómese como experimento, no como producción.
- **Broker MQTT público.** Cualquiera puede leer las lecturas. Ningún tópico MQTT mueve válvulas, a propósito.
- **microSD.** Programar `scripts/respaldo_mongo.sh` cada noche.
