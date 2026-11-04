"""Detector determinista de conversación que no va a ningún lado (candado de cierre).

Por qué existe: hay hilos que se quedan dando vueltas — el lead contesta
"ok", "va", un emoji, o sigue platicando sin soltar nada del negocio — y Nea
seguía preguntando indefinidamente. Eso quema tokens, satura el inbox del
dueño y, sobre todo, se lee a necesitado: un negocio serio no persigue.

El conteo va aquí y no en el prompt por la misma razón que `hostility.py`:
contar entre turnos es justo lo que un LLM hace de forma no confiable. El
LLM solo pone la redacción del cierre; que el cierre OCURRA (y que después
haya silencio) lo garantiza `turn.py`.

Es deliberadamente conservador. Cerrarle a un lead vivo cuesta mucho más que
aguantarle un turno de más, así que los dos disparadores exigen evidencia
acumulada, no un mal mensaje suelto. Por lo mismo el cierre no es para
siempre: tras la despedida el relleno se contesta con silencio, pero un
mensaje con contenido reabre la conversación y los contadores vuelven a cero
(`trae_contenido`, y la reapertura en `turn.py`).

Los umbrales y el enfriamiento se ajustan por entorno (`STALL_MAX_TURNS`,
`STALL_FILLER_STREAK`, `STALL_COOLDOWN_HOURS`); las constantes de aquí son
sus valores por defecto.
"""
from __future__ import annotations

import re

# Mensajes que no aportan nada: acuses, muletillas, risas, emojis sueltos.
# Ojo con lo que NO está aquí: "no", "no me interesa", "ahorita no" — un "no"
# es una respuesta clarísima y merece la salida elegante del prompt, no el
# candado. Y cualquier cosa larga se asume con contenido.
# Ojo con lo que tampoco está: los saludos. Un "hola" es una jugada legítima
# de conversación, y contándolo como vacío se podía matar un hilo en el tercer
# mensaje — demasiado pronto para un lead que apenas está entrando en calor.
# Tras la despedida, el lead suele contestar con dos o tres de estas juntas
# ("va, gracias 🙏", "ok 👍"): cuentan igual que una sola. Antes solo contaba
# una palabra con puntuación detrás, y un "ok 👍" pasaba por contenido — tras
# el cierre, eso reabría la conversación para contestarle a un acuse.
_PALABRAS_RELLENO = (
    r"ok(?:ay|ey|a|i)?|va|vale|sale|ah|ajá|aja|mmm?|hmm?|eh|este|ya|bueno|pues|"
    r"gracias|grax|muchas|mil|igualmente|saludos|bye|adi[oó]s|"
    r"(?:ja|je|ji|ha){2,}|lol|:v"
)
# Emojis: símbolos (☺ ✌ ❤ ✅), pictogramas y caras (👍 👌 🙏 😂 🫡), con su
# selector de variación y el pegamento de los compuestos.
_EMOJI = "[%s-%s%s-%s%s%s]" % (
    chr(0x2600), chr(0x27BF), chr(0x1F000), chr(0x1FAFF), chr(0xFE0F), chr(0x200D)
)
_PIEZA = rf"(?:(?:{_PALABRAS_RELLENO})(?![^\W\d_])|{_EMOJI})"
_SEP = r"[\s.!¡?¿,]*"
_RELLENO = re.compile(rf"^{_SEP}{_PIEZA}(?:{_SEP}{_PIEZA})*{_SEP}$", re.I)
# Solo emojis/puntuación: tampoco aporta.
_SIN_LETRAS = re.compile(r"^[^\w]+$", re.UNICODE)

MAX_CARACTERES_VACIO = 24  # arriba de esto asumimos que dijo algo

# Mensajes de relleno seguidos del lead que disparan el cierre.
RACHA_VACIA = 3
# Mensajes del lead sin que la conversación llegue nunca a agendar/DIY/handoff.
MAX_MENSAJES_SIN_AVANCE = 14

# Clave de la ficha del CRM donde queda a la vista que Nea cerró la
# conversación (y cuándo). El panel del contacto la enseña como «Cierre sin
# rumbo»; se borra al reabrir.
FICHA_CIERRE = "cierre_sin_rumbo"

# Tipos de mensaje que no aportan aunque no traigan texto. Una nota de voz,
# una foto o un documento, en cambio, se asumen con contenido: transcribirlos
# solo para decidir si reabrir costaría más que contestarlos.
_TIPOS_SIN_CONTENIDO = frozenset({"sticker", "reaction", "unsupported", "unknown"})

ALERTA = (
    "ALERTA DEL SISTEMA (esto NO lo escribió el lead): esta conversación ya no "
    "avanza. En ESTE turno tu respuesta es únicamente UNA línea cálida de "
    "cierre que deja la puerta abierta — sin pregunta, sin pitch, sin "
    "invitación a la cita, sin links. Algo del estilo de \"te dejo por aquí, "
    "cuando quieras retomarlo me escribes y seguimos\". Despídete con dignidad: "
    "no ruegues, no resumas la conversación y no ofrezcas nada más."
)


def es_relleno(text: str) -> bool:
    """¿El mensaje del lead no aporta absolutamente nada?"""
    limpio = (text or "").strip()
    if not limpio:
        return True
    if len(limpio) > MAX_CARACTERES_VACIO:
        return False
    return bool(_RELLENO.match(limpio) or _SIN_LETRAS.match(limpio))


def trae_contenido(tipo: str, texto: str | None) -> bool:
    """¿Este mensaje entrante reabre una conversación ya cerrada?

    Con texto, decide `es_relleno`: un "gracias" no reabre; "¿cuánto
    cuesta?", sí. Sin texto, decide el tipo: un sticker no, una nota de voz sí.
    """
    if texto and texto.strip():
        return not es_relleno(texto)
    if tipo in ("text", "button", "interactive"):
        return False  # texto vacío: nada que leer
    return tipo not in _TIPOS_SIN_CONTENIDO


def racha_vacia(user_texts: list[str]) -> int:
    """Mensajes de relleno CONSECUTIVOS al final del hilo del lead.

    Uno con contenido corta la racha: el lead que por fin soltó algo vuelve a
    empezar de cero.
    """
    racha = 0
    for text in reversed(user_texts):
        if es_relleno(text):
            racha += 1
        else:
            break
    return racha


def sin_rumbo(
    user_texts: list[str],
    fase: str,
    *,
    racha: int = RACHA_VACIA,
    max_mensajes: int = MAX_MENSAJES_SIN_AVANCE,
) -> bool:
    """¿Toca cerrar amable y dejar de responder?

    Dos caminos, ambos con evidencia acumulada:
    - el lead lleva `racha` mensajes seguidos sin decir nada, o
    - la conversación se estiró `max_mensajes` mensajes sin salir nunca del
      descubrimiento (ni agendando, ni DIY, ni handoff).

    `user_texts` son los mensajes del lead desde la última reapertura, no
    todos los de la historia: tras reabrir, los contadores vuelven a cero.
    Un umbral en 0 apaga ese camino.
    """
    if fase in ("agendando", "cerrada"):
        return False  # ya hay rumbo: no es asunto del candado
    if racha > 0 and racha_vacia(user_texts) >= racha:
        return True
    return max_mensajes > 0 and len(user_texts) >= max_mensajes
