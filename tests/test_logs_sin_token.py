"""El segmento secreto de `/webhook/<token>` no llega a los logs de accesos.

Nea 1.x recibía el webhook de Meta en `/webhook/<VERIFY_TOKEN>`. La 2.0 ya no
lo recibe (app/webhook_retirado.py), pero una instalación que actualiza sin
devolverle el webhook al CRM sigue recibiendo esas peticiones, y uvicorn
escribe la ruta de cada una.
"""
from __future__ import annotations

import logging


def test_tacha_el_verify_token_de_nea_en_el_log_de_accesos() -> None:
    from uvicorn.logging import AccessFormatter

    from app.main import SinTokenPropio

    def acceso(metodo: str, ruta: str) -> str:
        registro = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5000", metodo, ruta, "1.1", 200), None,
        )
        assert SinTokenPropio().filter(registro) is True
        # El formateador de uvicorn desempaca los cinco argumentos: si el
        # filtro los quita, cada petición deja un traceback en el log.
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(registro)
        return registro.getMessage()

    assert acceso("POST", "/webhook/s3cr3t-de-nea") == (
        '10.0.0.1:5000 - "POST /webhook/*** HTTP/1.1" 200'
    )
    assert "s3cr3t" not in acceso(
        "GET",
        "/webhook/s3cr3t-de-nea?hub.mode=subscribe&hub.verify_token=s3cr3t-de-nea&hub.challenge=1",
    )
    assert "s3cr3t" not in acceso(
        "GET", "/webhook?hub.verify_token=s3cr3t-de-nea&hub.challenge=1"
    )
    assert acceso("GET", "/health") == '10.0.0.1:5000 - "GET /health HTTP/1.1" 200'
