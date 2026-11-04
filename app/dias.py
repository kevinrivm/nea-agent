"""¿El lead nombró un día? Decide si `propose_slots` puede consultar una fecha.

En tienda-bachi (16 oct 2026, un viernes a la 1:39 pm, agenda de lunes a
viernes de 9 a 6) el lead escribió «¿cuándo podría ser?» y el modelo llamó
`propose_slots` con fecha=lunes 19, luego martes 20 y miércoles 21. Nunca
consultó sin fecha, que es el reparto que empieza HOY, y al lead le ofreció
solo el lunes con el viernes libre. El CRM estaba bien: la fecha la inventó el
modelo.

La regla de la herramienta ya decía «con fecha cuando pide un día concreto».
El modelo no la siguió, así que aquí se vuelve código. Si nadie en la
conversación reciente nombró un día, la fecha se ignora y se consulta el
reparto.

Es a propósito generoso: ante la duda, deja pasar la fecha. Ignorar una fecha
que sí se pidió es el error caro (el lead dice «el jueves» y le ofrecen otra
cosa). Dejar pasar una que no se pidió es el comportamiento de antes.
"""
from __future__ import annotations

import re
import unicodedata

_DIAS = r"lunes|martes|miercoles|jueves|viernes|sabado|domingo"
_MESES = (
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
    r"octubre|noviembre|diciembre"
)

_NOMBRA_UN_DIA = re.compile(
    r"\b(?:"
    rf"{_DIAS}|{_MESES}"
    r"|hoy|pasado manana|semana|finde|quincena"
    # Pedir OTRO día también es nombrar uno: el modelo elige cuál consultar.
    r"|otro dia|otra fecha|otros dias|mas adelante|despues"
    r"|siguiente|proxim[oa]s?"
    # Fechas con número: «el 20», «día 20», «20 de», «20/10».
    r"|el \d{1,2}|dia \d{1,2}|\d{1,2} de|\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2}"
    r")\b"
)

# «mañana» es día, pero «en la mañana»/«por las mañanas» es una franja.
_MANANA_DIA = re.compile(r"(?<!\bla )(?<!\blas )\bmanana\b")


def _plano(texto: str) -> str:
    """Minúsculas y sin acentos: el lead escribe «miercoles» y «miércoles»."""
    sin = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sin if not unicodedata.combining(c))


def nombra_un_dia(texto: str) -> bool:
    plano = _plano(texto)
    return bool(_NOMBRA_UN_DIA.search(plano) or _MANANA_DIA.search(plano))
