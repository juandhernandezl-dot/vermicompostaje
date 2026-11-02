"""
Cliente del LLM local (Ollama del stack por defecto; "openai" = LM Studio u
otro servidor compatible). Igual que en el agente del huerto: un unico
mensaje system con el contexto fresco, historial corto por chat y el modo de
razonamiento de Qwen3.5 desactivado.
"""
import logging
from collections import defaultdict, deque

import httpx

import config
import estado
import prompts

log = logging.getLogger(__name__)
_history = defaultdict(lambda: deque(maxlen=config.MAX_HISTORY_TURNS * 2))


def record_turn(chat_id, user_text: str, reply: str) -> None:
    _history[chat_id].append({"role": "user", "content": user_text})
    _history[chat_id].append({"role": "assistant", "content": reply})


def ask_llm(chat_id, user_text: str) -> str:
    est = estado.estado_actual()
    contexto = "\n\n".join([
        prompts.SYSTEM_PROMPT,
        "ESTADO ACTUAL: " + estado.texto_estado(est),
        "RESUMEN: " + estado.texto_historial(estado.resumen_historial(), est),
    ])
    mensajes = [{"role": "system", "content": contexto}, *_history[chat_id]]
    if config.LLM_BACKEND == "ollama":
        r = httpx.post(f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/chat", timeout=config.REQUEST_TIMEOUT_S, json={
            "model": config.LLM_MODEL, "messages": [*mensajes, {"role": "user", "content": user_text}],
            "think": False, "stream": False, "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.7, "top_p": 0.8, "num_predict": 300, "num_ctx": config.OLLAMA_NUM_CTX}})
        r.raise_for_status()
        reply = (r.json().get("message", {}).get("content") or "").strip()
    else:
        r = httpx.post(f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions", timeout=config.REQUEST_TIMEOUT_S,
                       headers={"Authorization": f"Bearer {config.LLM_API_KEY}"}, json={
            "model": config.LLM_MODEL, "temperature": 0.7, "top_p": 0.8, "max_tokens": 300,
            "messages": [*mensajes, {"role": "user", "content": f"{user_text}\n\n/no_think"}],
            "chat_template_kwargs": {"enable_thinking": False}})
        r.raise_for_status()
        reply = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not reply:
        log.warning("El LLM devolvio contenido vacio (backend=%s)", config.LLM_BACKEND)
        reply = prompts.RESPUESTA_LLM_VACIO
    record_turn(chat_id, user_text, reply)
    return reply
