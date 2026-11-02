# Contexto de construccion: la RAIZ del repo (ver docker-compose.yml), porque
# el control importa el modelo y los agentes RL de simulador/ (una sola copia).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    SIMULADOR_DIR=/app/simulador \
    CONTROL_CONFIG=/app/control/control.json
WORKDIR /app

COPY servicios/control/requirements.txt control/requirements.txt
RUN pip install --no-cache-dir -r control/requirements.txt

COPY simulador/vermicompost_mz.py simulador/rl_common_mz.py simulador/rl_agentes.py simulador/
COPY simulador/tablas_rl/ simulador/tablas_rl/
COPY servicios/control/control.py servicios/control/control.json control/

CMD ["python", "control/control.py"]
