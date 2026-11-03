# Guía Raspberry Pi 5 — stack completo con la planta simulada

Cubre los pasos 1, 2 y 4: verificar vermi‑stack, integrarlo con el control RL/PI/MANUAL y probar todo junto sin hardware. Todo se hace **desde el PC Ubuntu por SSH**. No hace falta ningún adaptador USB, ni el HAT, ni el ESP32.

**Criterio de éxito:** `docker compose ps` muestra 6 contenedores en marcha (más 2 con el agente). La API de la planta muestra la humedad cambiando. Una orden al agente abre una válvula del modelo, y esa apertura queda en MongoDB y en el dashboard.

---

## 0. Antes de empezar (en la Pi, por SSH)

```bash
ssh gitcode1@raspberrypi.local          # o la IP: hostname -I en la Pi
docker compose version                   # debe ser >= 2.24 (usa "!reset")
docker ps --format '{{.Names}}\t{{.Ports}}'
free -h; df -h /
```

- Si `docker ps` muestra **`huerto-node-red`**, el stack del huerto está corriendo con `network_mode: host` y ocupa el puerto 1880. No lo toques: más abajo pondrás `NODERED_PUERTO=1881`. Si además corre **`huerto-ollama`**, apágalo antes de usar el agente de vermicompost (`docker stop huerto-ollama`): dos modelos de 4B no caben juntos en 8 GB.
- Los contenedores del vermi‑stack viejo (`mongodb`, `nodered`, `sensores`, `actuadores`) usan **los mismos nombres y puertos** que el nuevo, así que hay que apagarlos (paso 2).

## 1. Copiar el repo a la Pi (desde el PC)

Coloca los archivos en el PC en `~/Documents/vermicompostaje/`, con la estructura del README. Luego, **desde el PC**:

```bash
rsync -av --exclude '.venv' --exclude 'dist' --exclude 'build' --exclude '__pycache__' \
      ~/Documents/vermicompostaje/ gitcode1@raspberrypi.local:~/vermicompostaje-unificado/
```

- `~/vermicompostaje` (v3 y despliegue) y `~/vermi-stack` **no se tocan**: quedan como respaldo para volver atrás.
- Si `rsync` no está instalado: `sudo apt install rsync` en los dos equipos.
- **Sin rsync, con VS Code:** extensión *Remote - SSH* → *Connect to Host* `gitcode1@raspberrypi.local` → *Open Folder* `~/vermicompostaje-unificado`. Luego arrastra la carpeta del PC al explorador de VS Code, o crea cada archivo y pega su contenido.

## 2. Respaldar y apagar el stack viejo (en la Pi)

```bash
cd ~/vermi-stack
docker exec mongodb mongodump --db vermicompost --archive --gzip > ~/respaldo_antes_de_unificar.gz
ls -lh ~/respaldo_antes_de_unificar.gz
docker compose down                      # apaga; NO borra mongo-data

# Los datos reales pasan a vivir fuera del repo (se COPIAN: el original queda para volver atrás).
mkdir -p ~/vermi-datos
sudo cp -a ~/vermi-stack/mongo-data ~/vermi-datos/mongo
du -sh ~/vermi-datos/mongo
```

## 3. Configurar en modo simulación

```bash
cd ~/vermicompostaje-unificado
cp .env.example .env
nano .env
```

Cambia estas líneas:

```bash
#COMPOSE_FILE=docker-compose.yml
COMPOSE_FILE=docker-compose.yml:docker-compose.sim.yml
DATOS_DIR=/home/gitcode1/vermi-datos
NODERED_PUERTO=1880                      # 1881 si huerto-node-red usa el 1880
```

En simulación, MongoDB usa `~/vermi-datos/mongo-sim`, así que los datos reales del paso 2 no se tocan.

### Contraseñas de Node-RED

El editor y el dashboard piden usuario y clave. Hay dos cuentas: `admin` para el editor y `huerto` para el dashboard (quien riega no debe poder cambiar los flujos). En `.env` se guarda el **hash** bcrypt, nunca la clave:

```bash
bash scripts/hash_clave.sh        # una vez para la clave del editor y otra para la del dashboard
nano .env                         # pega cada hash ENTRE COMILLAS SIMPLES
```

```bash
NODERED_ADMIN_USUARIO=admin
NODERED_ADMIN_HASH='$2b$10$...'
NODERED_DASH_USUARIO=huerto
NODERED_DASH_HASH='$2b$10$...'
```

Si falta un par o el hash está mal copiado, ese acceso queda **bloqueado** y `docker compose logs nodered` muestra `[seguridad] ... ese acceso queda bloqueado`. Node-RED sigue publicando lecturas y lotes.

La clave viaja sin cifrar dentro de la red (HTTP). Úsala solo en la red del huerto o del laboratorio, nunca expuesta a internet.

## 4. Construir y levantar

```bash
docker compose build                     # la primera vez tarda (npm de Node-RED, g++ del firmware)
docker compose up -d
docker compose ps                        # mongodb, nodered, sensores, actuadores, control, planta-simulada
docker compose logs -f actuadores control   # Ctrl+C para salir de los logs
```

En los logs de `actuadores` debe aparecer `[esp32] sincronizado: OK CONFIG N=3 ...`. Esa respuesta la da el **firmware real** que corre dentro de `planta-simulada`.

## 5. Verificar pieza por pieza

```bash
# Planta simulada: humedad por zona (VWC) y válvulas
curl -s localhost:7003/estado | python3 -m json.tool

# Puente: el ESP32 conectado y las 3 válvulas
curl -s localhost:8000/estado | python3 -m json.tool

# Control: modo, última lectura y última decisión por zona
curl -s localhost:8100/estado | python3 -m json.tool

# MongoDB: llegan lecturas cada 30 s
docker exec mongodb mongosh vermicompost --quiet --eval \
  'db.lecturas.find({}, {_id:0, sensor_id:1, humedad_pct:1, timestamp:1}).sort({timestamp:-1}).limit(3)'
```

Salud de los contenedores: `docker compose ps` debe mostrar `(healthy)` en `actuadores` y `control` (comprueban que su API responde en `/estado`; si no responde 3 veces seguidas pasan a `unhealthy`). Docker **no** reinicia por sí solo un contenedor `unhealthy`; solo reinicia los que se caen.

Dashboard: abre `http://raspberrypi.local:1880/dashboard` desde el PC (o el puerto que pusiste en `NODERED_PUERTO`). Deben aparecer los sensores 3, 4 y 6.

## 6. Probar el control (RL / PI / MANUAL)

```bash
C=localhost:8100
# Manual: abrir la zona 1 por 60 s (el firmware la cierra solo)
curl -s -X POST $C/manual/abrir -H 'Content-Type: application/json' -d '{"zona":1,"segundos":60,"origen":"prueba"}'
# PI en la zona 2 y RL en la zona 3
curl -s -X POST $C/modo -H 'Content-Type: application/json' -d '{"zona":2,"modo":"pi","origen":"prueba"}'
curl -s -X POST $C/modo -H 'Content-Type: application/json' -d '{"zona":3,"modo":"rl","origen":"prueba"}'
docker compose logs -f control           # una decisión por zona cada 30 s
# El árbitro rechaza abrir a mano una zona que manda el PI (esperado: 409 requiere_manual)
curl -s -X POST $C/manual/abrir -H 'Content-Type: application/json' -d '{"zona":2,"segundos":30,"origen":"prueba"}'
# Falla de sensor: la zona 3 deja de regar a los ~75 s
curl -s -X POST localhost:7003/falla -H 'Content-Type: application/json' -d '{"sensor":6,"activa":true}'
curl -s -X POST localhost:7003/falla -H 'Content-Type: application/json' -d '{"sensor":6,"activa":false}'
# Todo a manual y cerrado
curl -s -X POST $C/cerrar_todo -H 'Content-Type: application/json' -d '{"origen":"prueba"}'
```

Riegos y decisiones registrados:

```bash
docker exec mongodb mongosh vermicompost --quiet --eval 'db.riegos.find({},{_id:0}).sort({inicio:-1}).limit(5)'
docker exec mongodb mongosh vermicompost --quiet --eval 'db.decisiones.find({},{_id:0}).sort({timestamp:-1}).limit(5)'
```

**Prueba de seguridad del firmware.** Abre una válvula y apaga el puente: `docker compose stop actuadores`. A los 60 s el firmware de la planta cierra todo por falta de latido (compruébalo con `curl -s localhost:7003/estado`). Luego `docker compose start actuadores`.

## 7. El agente en una terminal (sin Telegram)

```bash
docker compose --profile agente up -d          # arranca agente y ollama
docker compose exec -it ollama ollama pull qwen3.5:4b   # la 1ª vez: ~3,4 GB, con barra de progreso
docker compose exec ollama ollama list         # debe aparecer qwen3.5:4b
docker compose exec -it agente python chat.py
```

El servicio `ollama-pull` intenta la misma descarga solo, pero en sus logs (`docker compose logs ollama-pull`) no hay barra de progreso: repite `pulling manifest` mientras espera, y eso no indica si avanza. Con `exec -it` ves el avance real o el error. Si la descarga se corta, repite el comando: continúa donde iba.

**Criterio de éxito:** `ollama list` muestra el modelo y `¿qué comen las lombrices?` recibe respuesta. Si el agente registra `404 Not Found ... /api/chat`, el modelo no está descargado.

Frases de prueba:
- `¿cómo está la zona 1?`
- `abre la válvula de la zona 1 por 2 minutos`, luego `sí`
- `¿la válvula 1 está abierta?`
- `pon la zona 2 en PI`, luego `sí`
- `abre la válvula de la zona 2` (debe negarse: la zona la controla el PI)
- `¿cuánto se ha regado hoy?`
- `¿qué comen las lombrices?` (esta va al LLM; en la Pi tarda entre 20 y 60 s la primera vez)
- `cierra todo`

Las órdenes y las consultas de estado no usan el LLM, así que responden al instante aunque Ollama no esté listo.

### El agente desde el dashboard

Con el perfil `agente` arriba, el dashboard tiene una segunda página, **Asistente** (`/dashboard/asistente`, en el menú ☰). Se entra con la cuenta `huerto`. Es el mismo agente que en la terminal:
- Abrir o cambiar de modo muestra los botones grandes **Sí, hazlo** / **No** durante 60 s. Cerrar no pide confirmación.
- Cada navegador tiene su propia conversación, que se conserva al recargar la página.
- Si el agente está apagado, la página responde «El asistente no está disponible…» y no cambia nada.
- Cada orden queda registrada con su canal: `agente:dashboard:<sesión>` o `agente:terminal` (campo `origen` en `riegos` y en los logs de `control`).

**Criterio de éxito:** desde el celular, en la página Asistente, «abre la válvula de la zona 2 por 1 minuto» → **Sí, hazlo** → la respuesta empieza por «Listo, abrí» y `docker compose logs control | grep agente:dashboard` muestra la orden.

La página llama a `POST /chat` del contenedor `agente` en `:8200`, solo dentro de la red de Docker (no publica puerto). Para probar esa API directamente desde la Pi:

```bash
docker compose exec agente python -c "import json,urllib.request as u; r=u.urlopen(u.Request('http://127.0.0.1:8200/chat', json.dumps({'chat_id':'prueba-pi-01','texto':'¿cómo está la zona 1?'}).encode(), {'Content-Type':'application/json'})); print(json.load(r)['respuesta'])"
```

## 8. Receptor del laboratorio contra la simulación

En el PC: `./dist/ReceptorVermicompost --sim` (ver [GUIA_PC.md](GUIA_PC.md)). Las lecturas llegan con el prefijo `uao/vermicompost/sim/...`. Los lotes de simulación se guardan en `~/lotes_respaldo_sim`, separados de los reales. Requiere que la Pi tenga internet (HiveMQ).

Para comprobar a mano la **solicitud de lote** (el receptor la envía con su botón; el flujo de Node-RED escucha `uao/vermicompost/+/solicitud` y solo atiende el prefijo de su entorno), desde la Pi con el stack arriba:

```bash
docker compose exec nodered node -e "
const m=require('mqtt');const c=m.connect('mqtt://broker.hivemq.com:1883');
const P='uao/vermicompost/sim';
c.on('connect',()=>{c.subscribe(P+'/lote/solicitado',{qos:1},()=>c.publish(P+'/solicitud',JSON.stringify({fecha:new Date().toISOString().slice(0,10),id:'prueba'}),{qos:1}))});
c.on('message',(t,p)=>{console.log('RESPUESTA',t,p.toString().slice(0,200));process.exit()});
setTimeout(()=>{console.log('SIN RESPUESTA');process.exit(1)},20000)"
```

Debe imprimir `RESPUESTA ... lote/solicitado` con un CSV (o `SIN_DATOS,<fecha>` si aún no hay lecturas ese día).

## 9. Pruebas sin Docker (opcional, en la Pi o en el PC)

```bash
sudo apt install -y g++ python3-venv
cd ~/vermicompostaje-unificado
python3 -m venv .venv && source .venv/bin/activate
pip install pymongo mongomock pyserial httpx paho-mqtt
bash pruebas/correr_todo.sh              # debe terminar en TODO CORRECTO
```

Las pruebas usan sus propios puertos (17201 a 17299) y una base en memoria, así que pueden correr con el stack encendido sin tocarlo. Si encuentran ocupado alguno de sus puertos, se detienen con `ABORTADA` en vez de arrancar a medias.

## Volver atrás (si algo falla)

```bash
cd ~/vermicompostaje-unificado && docker compose --profile agente down
cd ~/vermi-stack && docker compose up -d      # el stack viejo con sus datos originales
```

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `mongodb` en `Restarting (133)` y el log dice `tcmalloc ... MmapAligned() failed` | MongoDB 8 no arranca en la Pi 5. El compose usa `mongo:7.0`; si ves esto, tienes una copia vieja de `docker-compose.yml`. |
| `docker compose down` dice `Network ... Resource is still in use` | `agente` y `ollama` siguen arriba: pertenecen al perfil `agente` y `down` sin perfil no los toca. Usa `docker compose --profile agente down`. |
| El agente dice "no puedo pensar bien" y el log muestra `404 Not Found ... /api/chat` | El modelo no está descargado: sección 7, `ollama pull` con `exec -it`. |
| `ollama: Your kernel does not support memory limit capabilities` | El kernel de la Pi trae desactivado el control de memoria de los contenedores, así que se ignora el tope `OLLAMA_MEM`. No impide nada. Para activarlo: agrega `cgroup_enable=memory cgroup_memory=1` al final de la única línea de `/boot/firmware/cmdline.txt` y reinicia. |
| `Conflict. The container name "/mongodb" is already in use` | El stack viejo sigue arriba (paso 2). |
| `bind: address already in use` en el 1880 | `huerto-node-red` ocupa el puerto: `NODERED_PUERTO=1881`. |
| `!reset` da error de sintaxis | Docker Compose < 2.24: `sudo apt update && sudo apt install docker-compose-plugin`. |
| El puente dice `sin puerto ... reintentando` | `planta-simulada` no arrancó: `docker compose logs planta`. |
| El agente responde "no puedo pensar bien" | Ollama sigue descargando o cargando el modelo; las órdenes funcionan igual. |
| El dashboard marca la humedad en rojo | Sus rangos (70–85 %) no son % VWC; ver *Riesgos conocidos* del README. |
