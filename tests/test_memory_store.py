"""MemoryStore se porta como PgStore donde el turno lo nota.

Casi toda la suite corre contra MemoryStore. Cada diferencia con Postgres es
un sitio donde una prueba pasa en verde y producción revienta: así se escondió
el fallo del relay (ver tests/test_pg_store.py, que prueba PgStore de verdad).
"""
from __future__ import annotations

import pytest

from app.db import _CONV_COLUMNS
from app.state import COLUMNAS_DE_CONVERSACION, MemoryStore

IDENTITY = "525550001111"


def test_las_dos_aceptan_las_mismas_columnas():
    assert _CONV_COLUMNS is COLUMNAS_DE_CONVERSACION


async def test_una_columna_que_postgres_no_tiene_revienta_tambien_aqui():
    """Antes MemoryStore aceptaba cualquier campo con setattr: un nombre mal
    escrito pasaba las pruebas y en producción el turno moría con ValueError."""
    store = MemoryStore()
    conv = await store.get_or_create_conversation(IDENTITY)
    with pytest.raises(ValueError, match="columnas desconocidas"):
        await store.update_conversation(conv.id, fase="cerrada")
    await store.update_conversation(conv.id, phase="cerrada")
    assert conv.phase == "cerrada"


async def test_el_slug_se_refresca_como_en_postgres():
    """El ON CONFLICT de PgStore refresca el slug (el miembro renombró su
    subdominio; el id es el mismo). MemoryStore se quedaba con el viejo."""
    store = MemoryStore()
    antes = await store.get_or_create_conversation(IDENTITY, "org_a", "viejo")
    despues = await store.get_or_create_conversation(IDENTITY, "org_a", "nuevo")
    assert despues.id == antes.id
    assert despues.organization_slug == "nuevo"
