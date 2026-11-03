# Contratos entre piezas

Si una pieza cambia su lado de un contrato, la prueba `pruebas/prueba_integrada.py` debe fallar. Esa prueba corre el código real de puente, emisor, control y agente contra la planta simulada.

## 1. Serie Pi ↔ ESP32 (`actuador_valvulas.ino`)

115200 8N1, una línea por orden. La Pi envía `#<id> <ORDEN>` y el ESP32 responde `#<id> OK …` o `#<id> ERR …`.

| Orden | Respuesta |
|---|---|
| `PING` | `OK PING` |
| `CONFIG` | `OK CONFIG N=3 MAX_PULSO=900 MAX_CONTINUO=1800 DESCANSO=300 WATCHDOG=60 FW=1` |
| `ABRIR v s` | `OK ABRIR v aplicado [RECORTADO]` · `ERR VALVULA` · `ERR SEGUNDOS` · `ERR BLOQUEADA v resto` |
| `CERRAR v` | `OK CERRAR v` |
| `CERRAR_TODO` | `OK CERRAR_TODO` |
| `ESTADO` | `OK r1 r2 r3` (segundos restantes; **sin** la palabra ESTADO) |

Eventos que el ESP32 emite por su cuenta:
- `EVT INICIO`
- `EVT CERRADA v MOTIVO seg`, con MOTIVO `TIEMPO` | `ORDEN` | `WATCHDOG` | `MAXIMO`
- `EVT WATCHDOG`

## 2. HTTP del puente `actuadores:8000` (solo lo usa `control`)

- `GET /estado`
- `POST /valvulas/<v>/abrir` con `{"segundos", "origen"}`
- `POST /valvulas/<v>/cerrar` con `{"origen"}`
- `POST /valvulas/cerrar_todo`

`/niveles` existe, pero `control` no lo usa: cierra también las válvulas de las zonas en manual.

## 3. HTTP del control `control:8100` (agente, Node‑RED, operador)

| Ruta | Cuerpo | Notas |
|---|---|---|
| `GET /estado` | — | Por zona: modo, lectura con su edad, problema de lectura y última decisión. Incluye el estado del puente. |
| `POST /modo` | `{"zona", "modo": "manual\|pi\|rl", "origen"}` | Pasar a manual, o salir de manual, **cierra** la válvula. Si no se puede activar RL (falta la tabla), la zona sigue como estaba (409). |
| `POST /manual/abrir` | `{"zona", "segundos" (1–900), "origen"}` | **409 `requiere_manual`** si la zona está en PI o RL. |
| `POST /manual/cerrar` | `{"zona", "origen"}` | Siempre permitido, en cualquier modo. |
| `POST /cerrar_todo` | `{"origen"}` | Todo a manual y cerrado. |

### 3.1 HTTP del agente `agente:8200` (solo red interna de Docker; lo usa Node‑RED)

Sin `ports:` en el compose: desde fuera de Docker no se alcanza. Es el mismo pipeline que `chat.py`.

| Ruta | Cuerpo | Notas |
|---|---|---|
| `GET /salud` | — | `{"ok": true}`; lo usa el healthcheck. |
| `POST /chat` | `{"chat_id": 8–64 de [A-Za-z0-9_-], "texto": 1–500 caracteres}` | `{"respuesta", "confirmar_s"}`; `confirmar_s` son los segundos que quedan para responder SÍ/NO, o `null`. Las órdenes llegan a `control` con `origen` = `agente:dashboard:<8 primeros del chat_id>`. 400 si la entrada no es válida, 413 si el cuerpo pasa de 4 KB. Cada `chat_id` tiene su propia confirmación SÍ/NO pendiente (vence a los 60 s); se guarda como `web:<chat_id>`, aparte de la terminal. |

## 4. MongoDB `vermicompost`

| Colección | Escribe | Documento |
|---|---|---|
| `lecturas` | sensores | `timestamp` (UTC), `fecha`, `sitio`, `sensor_id`, `modo`, `corrida`, `humedad_pct` (% VWC), `temperatura_C`, `conductividad_uScm`, `ph`, `nitrogeno_mgkg`, `fosforo_mgkg`, `potasio_mgkg` |
| `riegos` | actuadores | `valvula`, `nombre`, `inicio`, `fin`, `duracion_pedida_s`, `duracion_aplicada_s`, `duracion_real_s`, `recortado`, `origen` (`agente`, `pi-z2`, `rl-z3`…), `estado` (`abierta`/`cerrada`), `motivo_cierre` |
| `decisiones` | control | `timestamp`, `zona`, `modo`, `vwc_pct`, `nivel` (0–1), `segundos`, `motivo` |
| `control_modos` | control | `zona`, `modo`, `origen`, `cambiado` |

## 5. MQTT (broker.hivemq.com:1883; prefijo `uao/vermicompost/huertolab`, o `uao/vermicompost/sim` en simulación)

| Tópico | Quién publica | Contenido |
|---|---|---|
| `<prefijo>/datos` | sensores | `timestamp,sensor_id,humedad,temp,ec,ph,n,p,k` (una lectura) |
| `<prefijo>/lote` | Node‑RED (06:00, 18:00, botón) | CSV del día, con retain |
| `<prefijo>/solicitud` | receptor | `{"fecha": "AAAA-MM-DD", "id": "…"}` |
| `<prefijo>/lote/solicitado` | Node‑RED | CSV o `SIN_DATOS,fecha`, sin retain |

**Ningún tópico MQTT mueve válvulas**, a propósito: el broker es público.
