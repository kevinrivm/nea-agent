"""Los mensajes ante los que Nea PUEDE callar: un cierre que no pide nada.

«gracias», «ok», un 👍. Si el modelo devuelve un texto vacío ante uno de
estos, está bien: no hay nada que contestar. Ante cualquier otra cosa, un
texto vacío es un fallo y el turno lo corrige (`app/turn.py`).

Gemelo de `src/server/dispatch/cierres.ts` de vocerocrm-cloud, que decide lo
mismo del otro lado (qué NO relanza su red de seguridad). Si una lista crece,
que crezca la otra: si divergen, el CRM relanza lo que Nea calla a propósito.

Conservador: ante la duda, NO es cierre. Cuesta menos contestar un «gracias»
que dejar sin respuesta una pregunta. Y genérico: nada del giro de un negocio.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

PALABRAS_DE_CIERRE = frozenset({
    "ok", "oki", "okey", "okay", "okis", "oka", "va", "vale", "sale", "dale",
    "listo", "lista", "ya", "bueno", "perfecto", "genial", "excelente", "super",
    "gracias", "grax", "muchas", "mil", "muchisimas", "igualmente", "saludos",
    "de", "acuerdo", "entendido", "entiendo", "enterado", "enterada",
    "bye", "adios", "chao", "chau", "hasta", "luego", "pronto", "manana", "nos",
    "vemos", "buenas", "noches", "tardes", "feliz", "dia", "noche", "bonito",
    "si", "a", "ti", "usted", "te", "lo", "agradezco",
    "jaja", "jajaja", "jajajaja", "jeje", "jejeje", "jiji", "lol",
    "ah", "aja", "m", "mm", "hmm", "eh", "pues",
})
# Por sí solas no cierran («sí» contesta algo; «de» no dice nada): solo valen
# acompañadas de una palabra de cierre de verdad.
SOLO_ACOMPANADAS = frozenset({"de", "a", "ti", "usted", "te", "lo", "nos", "hasta", "si"})

MAX_CARACTERES = 40
MAX_PALABRAS = 5
TIPOS_SIN_PREGUNTA = frozenset({"reaction", "sticker"})

_JAJA = re.compile(r"^(ja|je|ji)+j?a?$")
_REPETIDAS = re.compile(r"(.)\1{2,}")


def _plano(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFD", texto)
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower()


def _palabras(texto: str) -> list[str]:
    # Todo lo que no es letra ni número (emojis, signos) separa palabras.
    limpio = "".join(c if c.isalnum() else " " for c in _plano(texto))
    salida = []
    for p in limpio.split():
        salida.append("jaja" if _JAJA.match(p) else _REPETIDAS.sub(r"\1", p))
    return salida


def es_cierre(tipo: str, texto: str | None) -> bool:
    """¿Este entrante cierra sin pedir nada?"""
    t = (texto or "").strip()
    if not t:
        return tipo in TIPOS_SIN_PREGUNTA
    if "?" in t or "¿" in t:
        return False
    if not any(c.isalnum() for c in t):
        return True  # solo emojis y signos: 👍, 🙏🏻, «!!»
    if len(t) > MAX_CARACTERES:
        return False
    palabras = _palabras(t)
    if not palabras or len(palabras) > MAX_PALABRAS:
        return False
    if not all(p in PALABRAS_DE_CIERRE for p in palabras):
        return False
    return not all(p in SOLO_ACOMPANADAS for p in palabras)


def pregunta_abierta(ultima_de_nea: str | None) -> bool:
    """¿Lo último que dijo Nea fue una pregunta? Entonces «ok» CONTESTA."""
    return bool(ultima_de_nea) and ("?" in ultima_de_nea or "¿" in ultima_de_nea)


def todo_es_cierre(
    pendientes: Iterable[tuple[str, str | None]], ultima_de_nea: str | None = None
) -> bool:
    """¿Todo lo que el lead mandó en este turno son cierres? Vacío no lo es."""
    if pregunta_abierta(ultima_de_nea):
        return False
    lista = list(pendientes)
    return bool(lista) and all(es_cierre(tipo, texto) for tipo, texto in lista)
