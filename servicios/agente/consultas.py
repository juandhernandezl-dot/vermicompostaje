"""
Resolutor determinista de consultas (Decision 5): si la pregunta tiene una
respuesta exacta calculable, se responde con los datos y el LLM no se invoca.
"""
import re

import estado
from texto import normalizar

_FORMAS_RIEGO = ("riego", "regando", "regado", "rego", "regaron", "riegos")
_CUES_HISTORIAL = re.compile(r"\b(hoy|cuanto|cuanta|cuantas|cuantos|total|veces|minutos|horas|promedio|ha estado|historial|ayer)\b")
_CUE_ZONA = re.compile(r"\b(?:zona|valvula)\s*(1|2|3|uno|una|dos|tres)\b|\b(primera|segunda|tercera)\s+(?:zona|valvula)\b")
_ZONA_NUM = {"1": 1, "uno": 1, "una": 1, "primera": 1, "2": 2, "dos": 2, "segunda": 2,
             "3": 3, "tres": 3, "tercera": 3}
_FRASES_ESTADO = ("estado", "como esta la cama", "como estan", "como va", "que tal la cama", "como esta el vermi")
# Palabras que solo aparecen al preguntar por el estado. "lombrices" o "cama" a
# secas no: "¿que comen las lombrices?" es una pregunta general para el LLM.
_PALABRAS_ESTADO = re.compile(r"\b(valvulas?|regando|abiertas?|cerradas?|modo|humedad|temperatura|sensor(es)?)\b")


def resolver(user_text: str):
    t = normalizar(user_text)
    historial = bool(_CUES_HISTORIAL.search(t))

    if historial and (any(f in t for f in _FORMAS_RIEGO) or "humedad" in t):
        est = estado.estado_actual()
        return estado.texto_historial(estado.resumen_historial(), est)

    m = _CUE_ZONA.search(t)
    if m:
        zona = _ZONA_NUM[m.group(1) or m.group(2)]
        return estado.texto_estado(estado.estado_actual(), zona)

    if any(f in t for f in _FRASES_ESTADO) or _PALABRAS_ESTADO.search(t):
        return estado.texto_estado(estado.estado_actual())
    return None
