"""
Utilidades de texto compartidas entre comandos.py y consultas.py: ambos
módulos necesitan normalizar el mensaje del usuario antes de aplicar sus
propios patrones deterministas, y viven en el mismo nivel (ninguno
depende del otro), así que la normalización se factoriza aquí en vez de
duplicarse o crear una dependencia cruzada entre ellos.
"""
import unicodedata


def normalizar(texto: str) -> str:
    """minúsculas y sin tildes, para que la detección no dependa de cómo
    escriba el usuario o de cómo Whisper transcriba los acentos."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sin_tildes.lower().strip()
