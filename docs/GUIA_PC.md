# Guía del PC (Ubuntu 22.04) — Receptor de laboratorio

Cubre el paso 3. El receptor se suscribe por MQTT (broker público HiveMQ) a las lecturas en tiempo real y a los lotes diarios que publica la Pi, los guarda en `~/lotes_respaldo` y los grafica.

**Criterio de éxito:** el receptor abre su ventana, muestra **CONECTADO AL BROKER** y, con la Pi en simulación, aparecen las lecturas de los sensores 3, 4 y 6 cada 30 s.

## Qué cambió respecto al Receptor_Lab de Windows

| Problema en Ubuntu | Causa | Arreglo |
|---|---|---|
| El `.exe` no corre | Es un binario de Windows | `construir_linux.sh` genera un ejecutable de Linux |
| `AttributeError: CallbackAPIVersion` | El `python3-paho-mqtt` de Ubuntu 22.04 es la 1.6 | El script usa un entorno propio con paho 2.x, y el código acepta ambas |
| La ventana abre otra cosa | El puerto 8765 era también el del simulador v3 (`gui_mz.py`) | Ahora usa el 8766 (`RECEPTOR_PUERTO`) y comprueba que el puerto ocupado sea de verdad un receptor |
| Solo buscaba Edge/Chrome de Windows | — | También busca `google-chrome` y `chromium`; si no hay ninguno, abre una pestaña normal (Firefox) |
| Los datos simulados se mezclarían con los reales | — | `--sim` escucha `uao/vermicompost/sim/...` y guarda en `~/lotes_respaldo_sim` |

## Instalar y construir (una sola vez)

```bash
sudo apt install -y python3-venv
cd ~/Documents/vermicompostaje/receptor_lab
bash construir_linux.sh
```

Esto crea `dist/ReceptorVermicompost` y dos accesos en el menú de aplicaciones: **Receptor vermicompost** y **Receptor vermicompost (simulación)**.

## Usar

```bash
./dist/ReceptorVermicompost            # huerto (tópicos reales)
./dist/ReceptorVermicompost --sim      # planta simulada de la Pi
```

O sin construir nada: `.venv/bin/python receptor_vermicompost.py --sim`.

- Si la ventana se cierra, el receptor se detiene solo a los pocos segundos.
- **Solicitar lote:** elige una fecha y pulsa el botón. La Pi (Node‑RED) responde por `.../lote/solicitado` en menos de 60 s.

## Si no conecta

```bash
timeout 5 bash -c '</dev/tcp/broker.hivemq.com/1883' && echo "broker alcanzable" || echo "sin salida al 1883"
```

- **Sin salida al 1883:** la red de la universidad bloquea MQTT. Prueba con otra red (por ejemplo, el celular).
- **Broker alcanzable pero no llegan lecturas:** revisa que la Pi tenga internet y que los dos usen el mismo modo (los dos `--sim`, o ninguno). En la Pi: `docker compose logs sensores | grep mqtt`.

## Windows (como antes)

`construir_exe.bat` sigue funcionando igual. El `.exe` resultante también acepta `--sim` desde un acceso directo.
