# Voz del asistente: escuchar (faster-whisper) y hablar (Piper).
# Sin ffmpeg ni apt: faster-whisper decodifica el audio con PyAV (trae su propio
# ffmpeg) y Piper trae espeak-ng. Los modelos se bajan una vez al volumen
# /modelos y quedan ahi (docker compose down NO los borra; down -v si).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY servidor.py .
CMD ["python", "servidor.py"]
