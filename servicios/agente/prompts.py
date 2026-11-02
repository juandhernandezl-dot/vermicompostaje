"""Prompts y plantillas de respuesta del agente de vermicompost."""

SYSTEM_PROMPT = (
    "Eres el asistente de la cama de vermicompostaje del HuertoLab UAO. Respondes "
    "siempre en español colombiano, de forma clara y amable, para una persona sin "
    "conocimientos técnicos. Máximo 3 oraciones. No inventes datos que no te den; "
    "si no tienes información suficiente, dilo. "
    "La cama tiene 3 zonas, cada una con un sensor de suelo y una válvula de riego. "
    "Cada zona está en modo manual (solo riega si alguien lo pide), PI o RL (el "
    "sistema riega solo). Los datos de sensores que recibes en cada mensaje son los "
    "más recientes; si dicen que no hay datos, dilo, aunque antes en la conversación "
    "hayas mencionado otros valores. Recibirás el ESTADO ACTUAL y un RESUMEN de las "
    "últimas horas; usa cada uno según la pregunta, y solo si la pregunta es sobre la "
    "cama. Tú nunca abres ni cierras válvulas ni cambias modos: si te lo piden, "
    "explica que debe pedirse con una orden directa, por ejemplo «abre la válvula de "
    "la zona 1» o «pon la zona 2 en manual». Nunca digas que hiciste algo en la cama."
)

RESPUESTA_LLM_VACIO = "Uy, no logré terminar la respuesta. ¿Me pregunta de nuevo, por favor?"
RESPUESTA_LLM_FALLO = ("En este momento no puedo pensar bien la respuesta (el modelo de lenguaje "
                       "no responde). Las órdenes de riego y las consultas de estado sí funcionan.")

RESPUESTA_ACCION_AMBIGUA = (
    "No estoy seguro de qué quiere que haga, así que no hice nada. Dígame una sola orden, "
    "por ejemplo «abre la válvula de la zona 1 por 2 minutos», «cierra la zona 2» o "
    "«pon la zona 3 en manual». Las zonas son 1, 2 y 3.")
RESPUESTA_ACCION_SIN_ZONA = "¿De cuál zona? Las zonas son 1, 2 y 3; por ejemplo «abre la válvula de la zona 1»."
RESPUESTA_MODO_AMBIGUO = (
    "¿Qué modo quiere? Puede ser manual (solo riega cuando se pide), PI o RL (el sistema "
    "riega solo). Por ejemplo: «pon la zona 2 en PI».")
RESPUESTA_DURACION_INVALIDA = "Puedo regar entre unos segundos y {max} minutos por orden. ¿Cuánto tiempo?"
RESPUESTA_SIN_CONEXION = ("No pude comunicarme con el sistema de riego, así que no se hizo ningún "
                          "cambio. Intente de nuevo en un momento.")
RESPUESTA_INCIERTA = ("El sistema de riego tardó demasiado en responder y no sé si la orden se aplicó. "
                      "Pregúnteme el estado de la zona en un momento.")
RESPUESTA_CANCELADA = "Listo, no hice nada."
RESPUESTA_VENCIDA = "Esa orden ya se venció y no la hice. Si todavía la necesita, pídamela otra vez."

_NOMBRE_MODO = {"manual": "manual", "pi": "PI", "rl": "RL (aprendizaje por refuerzo)"}


def nombre_modo(m: str) -> str:
    return _NOMBRE_MODO.get(m, m)


def duracion(segundos: int) -> str:
    if segundos < 90:
        return f"{segundos} segundos"
    m = segundos / 60
    return f"{m:.0f} minutos" if m == int(m) else f"{m:.1f} minutos"


def pregunta_confirmacion(accion: dict) -> str:
    t = accion["tipo"]
    if t == "abrir_valvula":
        return (f"¿Abro la válvula de la zona {accion['zona']} durante {duracion(accion['segundos'])}? "
                "Se cierra sola al terminar. Responda SÍ o NO.")
    if t == "cambiar_modo":
        donde = "todas las zonas" if accion["zona"] == "todas" else f"la zona {accion['zona']}"
        if accion["modo"] == "manual":
            extra = "El riego automático de esa zona se detiene y la válvula se cierra."
        else:
            extra = "Desde ese momento el sistema decide solo cuándo regar."
        return f"¿Paso {donde} a modo {nombre_modo(accion['modo'])}? {extra} Responda SÍ o NO."
    return "¿Confirma? Responda SÍ o NO."
