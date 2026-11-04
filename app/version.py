"""Qué versión de Nea está corriendo, para `/health`.

Responde lo mismo que el `/api/health` del CRM —«¿ya se desplegó mi
cambio?»— y con la misma regla sobre el commit (vocero-crm,
`src/lib/version.ts`):

- `NEA_BUILD_COMMIT` lo congela el Dockerfile a partir del build arg
  `SOURCE_COMMIT`. Salió del build: viaja como verificado.
- Si el build no lo recibió, se enseña el `SOURCE_COMMIT` que la plataforma
  ponga en el entorno al arrancar, pero como NO verificado: es la palabra de
  la plataforma, no de la imagen, y una variable escrita a mano una vez se
  queda quieta mientras la imagen se sigue actualizando debajo.

Por eso el Dockerfile no guarda el commit en `SOURCE_COMMIT` mismo: la
variable de entorno del contenedor lo pisaría y ya no habría forma de saber
de dónde salió.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Commit:
    corto: str  # vacío si no hay ninguno por ningún lado
    verificado: bool  # True SOLO si salió del build


def version() -> str:
    """`NEA_VERSION` del build (o del entorno); `dev` si nadie la puso."""
    return os.environ.get("NEA_VERSION", "").strip() or "dev"


def commit() -> Commit:
    del_build = os.environ.get("NEA_BUILD_COMMIT", "").strip()[:7]
    if del_build:
        return Commit(del_build, True)
    return Commit(os.environ.get("SOURCE_COMMIT", "").strip()[:7], False)
