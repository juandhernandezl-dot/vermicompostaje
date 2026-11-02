# Agente de terminal (sin Telegram ni voz): liviano a proposito para la Pi.
# El contenedor queda vivo esperando; se conversa con:
#     docker compose exec -it agente python chat.py
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
CMD ["sleep", "infinity"]
