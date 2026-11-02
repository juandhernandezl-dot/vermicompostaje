"""
Configuracion del agente de vermicompost. Unico punto de lectura de
os.environ (misma convencion que el agente del huerto, Capitulo 8 §8.4).
"""
import os

# --- LLM (contenedor ollama del stack; "openai" = LM Studio u otro compatible)
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b" if LLM_BACKEND == "ollama" else "qwen3.5-4b")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "lm-studio")
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "4"))
# En CPU (Raspberry Pi 5) la primera respuesta incluye cargar el modelo en RAM.
REQUEST_TIMEOUT_S = int(os.environ.get("REQUEST_TIMEOUT_S", "180"))

# --- Servicio de control: unica ruta de escritura hacia las valvulas
CONTROL_URL = os.environ.get("CONTROL_URL", "http://control:8100").rstrip("/")
ACCION_TIMEOUT_S = int(os.environ.get("ACCION_TIMEOUT_S", "20"))
CONFIRMACION_TTL_S = int(os.environ.get("CONFIRMACION_TTL_S", "60"))
RIEGO_MANUAL_DEFECTO_S = int(os.environ.get("RIEGO_MANUAL_DEFECTO_S", "120"))
RIEGO_MANUAL_MAX_S = int(os.environ.get("RIEGO_MANUAL_MAX_S", "900"))   # = tope del firmware

# --- MongoDB (historial)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://mongodb:27017")
MONGO_DB = os.environ.get("MONGO_DB", "vermicompost")
SITIO = os.environ.get("SITIO", "huertolab")
HISTORIAL_HORAS = int(os.environ.get("HISTORIAL_HORAS", "24"))
LECTURA_VIEJA_MIN = int(os.environ.get("LECTURA_VIEJA_MIN", "15"))
