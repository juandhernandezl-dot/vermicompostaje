# Subir el repo a GitHub (`juandhernandezl-dot/vermicompostaje`)

Desde la carpeta del repo en el PC (`~/Documents/vermicompostaje`), con tu usuario de git ya configurado:

```bash
git init -b main
git add .
git status                      # comprueba que NO aparecen .env, dist/, .venv/ ni datos
git commit -m "Unificación del vermicompostaje: stack Docker, control RL/PI/MANUAL, firmware, agente, simulador y receptor"
git remote add origin https://github.com/juandhernandezl-dot/vermicompostaje.git
git push -u origin main
```

## Rama `legado` (arquitectura descartada, fuera de `main`)

Guarda `planta.py`, `hardware_mz.py`, `modbus_rtu.py`, `esp32_valvulas.ino`, `planta.json`, `README_PLANTA.md`, `esp32_pi_control_BOMBAAUTOM.ino` y `leer_sensores_sn3002.py`. Siguen teniendo valor como referencia del bus Modbus y para el capítulo de la tesis, pero **no se flashean ni se ejecutan** junto al stack.

```bash
git switch --orphan legado      # rama sin historia; la carpeta queda vacía de lo versionado
mkdir legado && cd legado
scp -r gitcode1@raspberrypi.local:'~/vermicompostaje/despliegue/{planta.py,hardware_mz.py,modbus_rtu.py,esp32_valvulas.ino,planta.json,README_PLANTA.md}' .
scp gitcode1@raspberrypi.local:'~/vermicompostaje/{esp32_pi_control_BOMBAAUTOM.ino,leer_sensores_sn3002.py}' .
printf '# Legado\nArquitectura descartada el 27 oct 2026: planta.py era maestro Modbus de sensores y de un ESP32 esclavo Modbus,\nen conflicto con el stack (dueño único del RS-485 y puente HTTP). NO EJECUTAR junto al stack. Ver main.\n' > README.md
cd .. && git add legado && git commit -m "Legado: despliegue Modbus y PI con DAC (descartados)"
git push -u origin legado
git switch main
```
