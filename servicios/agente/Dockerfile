# Agente (sin Telegram ni voz): liviano a proposito para la Pi.
# El proceso principal es la API interna que usa el dashboard (servidor.py,
# POST /chat en :8200, sin puerto publicado). La terminal sigue igual:
#     docker compose exec -it agente python chat.py
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
CMD ["python", "servidor.py"]
