"""
Pipeline del agente (el "Deterministic Supervisor"): cada mensaje pasa por
etapas y el LLM solo se invoca si ninguna etapa determinista lo resolvio.

  0) SI/NO a una orden pendiente     (comandos.resolver_confirmacion)
  1) ORDEN fisica                    (comandos.detectar_accion -> control)
  2) CONSULTA con respuesta exacta   (consultas.resolver -> control/Mongo)
  3) LLM                             (llm_client.ask_llm)

Independiente del canal: hoy lo usa chat.py (terminal); manana puede usarlo
una interfaz propia o el dashboard sin tocar esta logica.
"""
import logging

import comandos
import consultas
import llm_client
import prompts

log = logging.getLogger(__name__)


def procesar(chat_id, texto: str) -> str:
    r = comandos.resolver_confirmacion(chat_id, texto)
    if r is not None:
        log.info("confirmacion resuelta | %r", texto)
        llm_client.record_turn(chat_id, texto, r)
        return r
    accion = comandos.detectar_accion(texto)
    if accion is not None:
        log.info("orden detectada %s | %r", accion, texto)
        r = comandos.procesar_accion(chat_id, accion)
        llm_client.record_turn(chat_id, texto, r)
        return r
    r = consultas.resolver(texto)
    if r is not None:
        log.info("consulta resuelta sin LLM | %r", texto)
        llm_client.record_turn(chat_id, texto, r)
        return r
    try:
        return llm_client.ask_llm(chat_id, texto)
    except Exception:                                          # noqa: BLE001
        log.exception("fallo al consultar el LLM")
        return prompts.RESPUESTA_LLM_FALLO
