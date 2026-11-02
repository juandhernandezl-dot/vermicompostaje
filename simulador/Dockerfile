# Planta simulada: compila el firmware REAL del ESP32 para el PC y lo ejecuta
# junto a los sensores Modbus simulados y la fisica del v3.
# Contexto de construccion: la raiz del repo (docker-compose.sim.yml).
FROM python:3.12-slim AS compilar
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
COPY firmware/ /src/firmware/
RUN g++ -std=c++17 -O1 -I /src/firmware/pruebas -o /firmware_host /src/firmware/pruebas/host_main.cpp

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 FIRMWARE_BIN=/app/firmware_host
WORKDIR /app/simulador
COPY --from=compilar /firmware_host /app/firmware_host
COPY simulador/vermicompost_mz.py simulador/planta_simulada.py ./
EXPOSE 7001 7002 7003
CMD ["python", "planta_simulada.py"]
