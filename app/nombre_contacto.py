"""Nombre de trato para saludos de Nea, obtenido del contexto del CRM.

Un displayName es información suministrada por un usuario/plataforma, no
una identidad verificada. Preferimos un saludo sin nombre a adivinarlo.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_PRIMERAS_NO_PERSONA = {
    "cliente", "contacto", "usuario", "user", "lead", "prospecto",
    "desconocido", "desconocida", "unknown", "sin", "anonimo",
    "instagram", "whatsapp", "facebook", "messenger", "tiktok",
    "empresa", "negocio", "compania", "compañia", "compañía",
    "clinica", "clínica", "consultorio", "tienda", "restaurante",
    "fundacion", "fundación", "servicios", "soporte", "ventas",
    "atencion", "atención", "admin", "administrador", "equipo",
    "bot", "test", "prueba",
}

_COMPUESTOS = {
    "maría": {"jose", "josé", "fernanda", "camila"},
    "maria": {"jose", "josé", "fernanda", "camila"},
    "josé": {"luis", "manuel", "antonio"},
    "jose": {"luis", "manuel", "antonio"},
    "juan": {"carlos", "pablo", "josé", "jose"},
    "ana": {"maría", "maria", "lucia", "lucía"},
}

_PALABRA_HUMANA = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?\Z", re.UNICODE)


def nombre_de_trato(valor: Any) -> str | None:
    """Devuelve un nombre corto o None ante handles, empresas o etiquetas."""
    if not isinstance(valor, str):
        return None
    limpio = unicodedata.normalize("NFC", " ".join(valor.split()))
    if not limpio or len(limpio) > 90:
        return None
    palabras = limpio.split(" ")
    if len(palabras) > 5 or any(
        len(p) > 28 or not _PALABRA_HUMANA.fullmatch(p) for p in palabras
    ):
        return None
    primero = palabras[0]
    if len(primero) < 2 or primero.casefold() in _PRIMERAS_NO_PERSONA:
        return None

    def estilo(palabra: str) -> str:
        return palabra[:1].upper() + palabra[1:].lower()

    segundo = palabras[1] if len(palabras) > 1 else None
    if segundo and segundo.casefold() in _COMPUESTOS.get(primero.casefold(), set()):
        return f"{estilo(primero)} {estilo(segundo)}"
    return estilo(primero)


def nombre_fiable_desde_contexto(contacto: dict | None) -> str | None:
    """La corrección explícita del lead tiene prioridad sobre el perfil."""
    if not isinstance(contacto, dict):
        return None
    ficha = contacto.get("ficha")
    if isinstance(ficha, dict):
        for campo in ("nombre_preferido", "nombre_completo", "nombre"):
            preferido = nombre_de_trato(ficha.get(campo))
            if preferido:
                return preferido
    return nombre_de_trato(contacto.get("name"))
