"""
Deteccion determinista de ORDENES (guardarrail central, igual que en el agente
del huerto). Corre ANTES del LLM: el modelo nunca decide ni ejecuta una accion
fisica. La unica salida de este modulo es la API del servicio de control, que
es el arbitro del modo de cada zona; el agente no conoce al puente ni al ESP32.

Reglas (consejo del 20 oct 2026, acciones-agente):
  - Lo que abre agua o cambia quien manda la zona (abrir valvula, cambiar de
    modo) pide SI/NO repitiendo la orden en palabras. Lo que corta el agua
    (cerrar una zona, cerrar todo) se ejecuta directo.
  - Ante ambiguedad (varias zonas, zona inexistente, ordenes mezcladas, una
    negacion antes del verbo) se rechaza y se pide repetir. Nunca se adivina.
  - Solo una respuesta afirmativa del control (que a su vez espera la
    confirmacion del ESP32) produce un "listo".
"""
import logging
import re
import threading
import time

import httpx

import config
import estado
import prompts
from texto import normalizar

log = logging.getLogger(__name__)

_NUM = {"uno": 1, "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
        "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20, "treinta": 30,
        "medio": 0.5, "media": 0.5}
_ZONA_PALABRAS = {"1": "1", "uno": "1", "una": "1", "primera": "1", "2": "2", "dos": "2",
                  "segunda": "2", "3": "3", "tres": "3", "tercera": "3"}

_V_ABRIR = r"(?:abr(?:e|a|as|ir|iste|amos)|abrela|rieg(?:a|ue|ues)|regar|activ(?:a|e|ar)|enciend(?:e|a)|encender|prend(?:e|a|er))"
_V_CERRAR = r"(?:cierr(?:a|e|es)|cerrar|cierrela|apag(?:a|ue|ar)|desactiv(?:a|e|ar)|par(?:a|e|ar)|deten(?:er|ga)?|cort(?:a|e|ar))"
_V_MODO = r"(?:cambi(?:a|e|ar)|pon(?:er|ga|gas|la|lo)?|pas(?:a|e|ar)(?:la|lo)?|dej(?:a|e|ar)(?:la|lo)?|activ(?:a|e|ar)|us(?:a|e|ar)|vuelv(?:e|a))"
_PALABRAS_MODO = r"\b(?:modo|manual|pi|rl|automatic[oa]|refuerzo|aprendizaje|inteligente)\b"

_P_MODO = re.compile(rf"\b{_V_MODO}\b.*{_PALABRAS_MODO}")
_P_CERRAR_TODO = re.compile(rf"\b{_V_CERRAR}\b.*\b(?:todo|todas|riego)\b")
_P_CERRAR = re.compile(rf"\b{_V_CERRAR}\b.*\b(?:valvulas?|zonas?)\b")
_P_ABRIR = re.compile(rf"\b{_V_ABRIR}\b.*\b(?:valvulas?|zonas?)\b")

_ZONA_TRAS = re.compile(r"\b(?:valvulas?|zonas?)\s+(?:de\s+la\s+zona\s+|numero\s+|#\s*)?(\d+|uno|una|dos|tres)\b")
_ZONA_COORD = re.compile(r"\s*(?:,|\by\b|\be\b)\s*(?:(?:la|las)\s+)?(?:(?:valvula|zona)s?\s+)?(\d+|uno|una|dos|tres)\b")
_ZONA_ORDINAL = re.compile(r"\b(primera|segunda|tercera)\s+(?:valvula|zona)\b")
_TODAS = re.compile(r"\b(?:todas?|todo)\b")
_DURACION = re.compile(r"\b(\d+(?:[.,]\d+)?|uno|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
                       r"quince|veinte|treinta|medio|media)\s*(segundos?|seg|s|minutos?|min|m|horas?|h)\b")
_MENCIONA_TIEMPO = re.compile(r"\b(?:segundos?|minutos?|horas?|rato|ratico)\b")

_AFIRMATIVOS = {"si", "dale", "claro", "ok", "okay", "confirmo", "hagale", "correcto", "listo", "bueno"}
_NEGATIVOS = {"no", "cancela", "cancelar", "cancele", "espera", "espere", "mejor"}

_pendientes: dict = {}
_lock = threading.Lock()


def _zonas(texto: str) -> set:
    crudas = _ZONA_ORDINAL.findall(texto)
    for m in _ZONA_TRAS.finditer(texto):
        crudas.append(m.group(1))
        fin = m.end()
        while (c := _ZONA_COORD.match(texto, fin)):
            crudas.append(c.group(1))
            fin = c.end()
    return {_ZONA_PALABRAS.get(z, "invalida") for z in crudas}


def _modo(texto: str):
    if re.search(r"\bmanual\b", texto):
        return "manual"
    if re.search(r"\b(?:rl|refuerzo|aprendizaje|inteligente)\b", texto):
        return "rl"
    if re.search(r"\bpi\b", texto):
        return "pi"
    return None                              # "automatico" a secas: PI o RL, se pregunta


def _segundos(texto: str):
    """Duracion pedida en segundos; la de defecto si no se menciona ninguna, y
    None si se menciona un tiempo que no se entiende ("un rato"): mejor
    preguntar que regar un tiempo distinto del que se pidio."""
    m = _DURACION.search(texto)
    if not m:
        return None if _MENCIONA_TIEMPO.search(texto) else config.RIEGO_MANUAL_DEFECTO_S
    n = m.group(1).replace(",", ".")
    valor = float(n) if n[0].isdigit() else _NUM[n]
    u = m.group(2)
    s = valor * (1 if u.startswith("s") else 3600 if u.startswith("h") else 60)
    return int(round(s))


def detectar_accion(texto_usuario: str):
    """dict con la orden, {"tipo": "ambigua"} si parece orden pero no es clara,
    o None si no es una orden (sigue hacia consultas / LLM)."""
    t = normalizar(texto_usuario)
    candidatos = []
    if _P_MODO.search(t) and re.search(_PALABRAS_MODO, t):
        candidatos.append(("cambiar_modo", _P_MODO.search(t)))
    else:
        for tipo, patron in (("cerrar_valvula", _P_CERRAR), ("abrir_valvula", _P_ABRIR)):
            m = patron.search(t)
            if m:
                candidatos.append((tipo, m))
        if not candidatos and _P_CERRAR_TODO.search(t):
            candidatos.append(("cerrar_todo", _P_CERRAR_TODO.search(t)))
    if not candidatos:
        return None
    if len(candidatos) > 1:
        return {"tipo": "ambigua"}
    tipo, m = candidatos[0]
    if re.search(r"\bno\b", t[: m.start()]):
        return {"tipo": "ambigua"}

    zonas = _zonas(t)
    if len(zonas) > 1 or "invalida" in zonas:
        return {"tipo": "ambigua"}
    zona = next(iter(zonas), None)
    if tipo == "cerrar_valvula" and zona is None and _TODAS.search(t):
        return {"tipo": "cerrar_todo"}
    if tipo == "cambiar_modo":
        if zona is None and _TODAS.search(t):
            zona = "todas"
        return {"tipo": tipo, "zona": zona, "modo": _modo(t)}
    if tipo == "abrir_valvula":
        return {"tipo": tipo, "zona": zona, "segundos": _segundos(t)}
    return {"tipo": tipo, "zona": zona}


def procesar_accion(chat_id, accion: dict) -> str:
    tipo = accion["tipo"]
    if tipo == "ambigua":
        return prompts.RESPUESTA_ACCION_AMBIGUA
    if tipo == "cerrar_todo":
        return ejecutar(accion, origen_de(chat_id))
    if accion.get("zona") is None:
        return prompts.RESPUESTA_ACCION_SIN_ZONA
    if tipo == "cambiar_modo" and accion.get("modo") is None:
        return prompts.RESPUESTA_MODO_AMBIGUO
    if tipo == "abrir_valvula" and (accion["segundos"] is None
                                    or not 1 <= accion["segundos"] <= config.RIEGO_MANUAL_MAX_S):
        return prompts.RESPUESTA_DURACION_INVALIDA.format(max=config.RIEGO_MANUAL_MAX_S // 60)
    if tipo == "cerrar_valvula":
        return ejecutar(accion, origen_de(chat_id))
    if tipo == "abrir_valvula":
        # Si la zona no esta en manual, el control rechazaria la orden: se dice
        # ya, en vez de pedir un SI para algo que no se va a poder hacer.
        est = estado.estado_actual()
        modo = next((z["modo"] for z in (est or {}).get("zonas", []) if z["zona"] == int(accion["zona"])), None)
        if modo and modo != "manual":
            return _fallo(409, {"error": "requiere_manual", "modo": modo},
                          f"abrir la válvula de la zona {accion['zona']}")
    with _lock:
        _pendientes[chat_id] = (accion, time.monotonic() + config.CONFIRMACION_TTL_S)
    return prompts.pregunta_confirmacion(accion)


def resolver_confirmacion(chat_id, texto_usuario: str):
    """Resuelve un SI/NO pendiente. Cualquier otro mensaje descarta la orden
    pendiente y devuelve None (el mensaje sigue el flujo normal)."""
    with _lock:
        pendiente = _pendientes.pop(chat_id, None)
    if pendiente is None:
        return None
    accion, vence = pendiente
    palabras = re.findall(r"[a-z]+", normalizar(texto_usuario))
    primera = palabras[0] if palabras else ""
    if primera in _NEGATIVOS:
        return prompts.RESPUESTA_CANCELADA
    if primera not in _AFIRMATIVOS:
        return None
    if time.monotonic() > vence:
        return prompts.RESPUESTA_VENCIDA
    return ejecutar(accion, origen_de(chat_id))


def pendiente_s(chat_id):
    """Segundos que le quedan a la orden que espera SI/NO en este chat, o None."""
    with _lock:
        p = _pendientes.get(chat_id)
    if p is None:
        return None
    resta = int(p[1] - time.monotonic())
    return resta if resta > 0 else None


def origen_de(chat_id) -> str:
    """Quien dio la orden, para los registros de control y del puente:
    'agente:terminal', 'agente:dashboard:<8 primeros de la sesion>'. Sin
    cuentas por persona, la sesion del navegador es lo mas fino que hay."""
    c = str(chat_id)
    if c.startswith("web:"):
        return "agente:dashboard:" + re.sub(r"[^\w-]", "", c[4:])[:8]
    return "agente:" + (re.sub(r"[^\w-]", "", c)[:20] or "desconocido")


def _post(ruta, cuerpo, origen="agente"):
    cuerpo = dict(cuerpo, origen=origen)
    try:
        r = httpx.post(f"{config.CONTROL_URL}{ruta}", json=cuerpo, timeout=config.ACCION_TIMEOUT_S)
    except httpx.ConnectError:
        return None, None
    except httpx.HTTPError:
        return "incierta", None
    try:
        datos = r.json()
    except ValueError:
        datos = {}
    log.info("POST %s %s -> %s %s", ruta, cuerpo, r.status_code, datos)
    return r.status_code, datos if isinstance(datos, dict) else {}


def _fallo(codigo, datos, que):
    if codigo is None:
        return prompts.RESPUESTA_SIN_CONEXION
    if codigo == "incierta":
        return prompts.RESPUESTA_INCIERTA
    err = str(datos.get("error", ""))
    if datos.get("error") == "requiere_manual":
        return (f"No puedo {que} porque esa zona la está controlando "
                f"{prompts.nombre_modo(datos.get('modo', ''))}. Si quiere hacerlo a mano, primero "
                f"dígame «pon la zona en manual».")
    m = re.search(r"BLOQUEADA \d+ (\d+)", err)
    if m:
        return (f"No puedo {que}: la válvula quedó bloqueada por seguridad porque estuvo abierta "
                f"demasiado tiempo seguido. Se desbloquea en unos {int(m.group(1)) // 60 + 1} minutos.")
    if codigo == 503 or "no esta conectado" in err or "no respondio" in err or "puente no disponible" in err:
        return ("No puedo " + que + " porque el controlador de las válvulas no está respondiendo. "
                "No se hizo ningún cambio.")
    return f"No pude {que}; el sistema respondió: {err or codigo}. No se hizo ningún cambio."


def ejecutar(accion: dict, origen: str = "agente") -> str:
    """Nunca lanza excepcion; solo un 200 del control produce un mensaje de exito."""
    t = accion["tipo"]
    if t == "abrir_valvula":
        z, s = int(accion["zona"]), int(accion["segundos"])
        codigo, d = _post("/manual/abrir", {"zona": z, "segundos": s}, origen)
        if codigo == 200:
            aplicado = int(d.get("segundos_aplicados", s))
            return (f"Listo, abrí la válvula de la zona {z}; el controlador de las válvulas lo confirmó. "
                    f"Se cierra sola en {prompts.duracion(aplicado)}.")
        return _fallo(codigo, d or {}, f"abrir la válvula de la zona {z}")
    if t == "cerrar_valvula":
        z = int(accion["zona"])
        codigo, d = _post("/manual/cerrar", {"zona": z}, origen)
        if codigo == 200:
            return f"Listo, cerré la válvula de la zona {z}; el controlador de las válvulas lo confirmó."
        return _fallo(codigo, d or {}, f"cerrar la válvula de la zona {z}")
    if t == "cerrar_todo":
        codigo, d = _post("/cerrar_todo", {}, origen)
        if codigo == 200:
            return ("Listo, cerré todas las válvulas y dejé las tres zonas en modo manual: nada riega "
                    "solo hasta que alguien lo pida o vuelva a activar PI o RL.")
        return _fallo(codigo, d or {}, "cerrar todas las válvulas")
    if t == "cambiar_modo":
        zonas = [1, 2, 3] if accion["zona"] == "todas" else [int(accion["zona"])]
        bien, mal = [], []
        for z in zonas:
            codigo, d = _post("/modo", {"zona": z, "modo": accion["modo"]}, origen)
            (bien if codigo == 200 else mal).append((z, codigo, d or {}))
        partes = []
        if bien:
            nombres = ", ".join(str(z) for z, _, _ in bien)
            partes.append(f"Listo, la zona {nombres} quedó en modo {prompts.nombre_modo(accion['modo'])}.")
            if accion["modo"] == "manual":
                partes.append("Su válvula quedó cerrada.")
        for z, codigo, d in mal:
            partes.append(_fallo(codigo, d, f"cambiar la zona {z} a {prompts.nombre_modo(accion['modo'])}"))
        return " ".join(partes)
    return prompts.RESPUESTA_ACCION_AMBIGUA
