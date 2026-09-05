"""El motivo con el que Nea devuelve una conversación a un humano.

Importa más de lo que parece: es el texto que el miembro lee en su bandeja
cuando su agente se calló. Mientras todo escalaba como "error", quien no había
pegado su token de OpenRouter leía «Error del proveedor de IA» — y se iba a
revisar el estado de un proveedor al que nunca se llamó.
"""

from typing import Any

import pytest

from app.dispatch import _a_humano, _contexto_multiorg


class _CrmQueAnota:
    """Anota el motivo del handoff y nada más."""

    def __init__(self, config_ia: Any = None, contexto: Any = None) -> None:
        self.motivos: list[str] = []
        self._config_ia = config_ia
        self._contexto = contexto

    def registrar(self, identity: str, conversation_id: str) -> None:
        return None

    async def precargar(self, conversation_id: str) -> Any:
        return self._contexto

    def credenciales_llm(self) -> Any:
        return self._config_ia

    async def post_handoff(self, conversation_id: str, reason: str) -> None:
        self.motivos.append(reason)


class _RegistroDePrueba:
    def __init__(self, crm: _CrmQueAnota) -> None:
        self._crm = crm

    def cliente(self, org_id: str, slug: str) -> _CrmQueAnota:
        return self._crm


class _CtxDePrueba:
    """Lo mínimo que `_contexto_multiorg` mira antes de rendirse."""

    def __init__(self, registro: Any) -> None:
        self.registro = registro


PAYLOAD = {"organization": {"id": "org_1", "slug": "allok"}}


@pytest.mark.asyncio
async def test_sin_token_del_miembro_el_motivo_no_es_error() -> None:
    crm = _CrmQueAnota(config_ia=None, contexto={"algo": True})
    ctx = _CtxDePrueba(_RegistroDePrueba(crm))

    resultado = await _contexto_multiorg(ctx, PAYLOAD, "521999", "cv_1")

    assert resultado is None, "sin con qué pensar, no hay turno"
    # El fondo del asunto: al proveedor NO se le llamó, así que decir que
    # falló es mandar al miembro a buscar una avería que no existe.
    assert crm.motivos == ["sin_credencial"]


@pytest.mark.asyncio
async def test_si_el_crm_no_da_contexto_eso_si_es_un_error() -> None:
    crm = _CrmQueAnota(config_ia={"path": "/x", "model": "m"}, contexto=None)
    ctx = _CtxDePrueba(_RegistroDePrueba(crm))

    resultado = await _contexto_multiorg(ctx, PAYLOAD, "521999", "cv_1")

    assert resultado is None
    assert crm.motivos == ["error"]


@pytest.mark.asyncio
async def test_el_motivo_viaja_tal_cual_al_crm() -> None:
    crm = _CrmQueAnota()
    await _a_humano(crm, "cv_1", "sin_credencial")
    assert crm.motivos == ["sin_credencial"]


@pytest.mark.asyncio
async def test_un_handoff_que_falla_no_tumba_el_turno() -> None:
    class _CrmRoto:
        async def post_handoff(self, conversation_id: str, reason: str) -> None:
            raise RuntimeError("el CRM no contesta")

    # No debe propagar: el mensaje del cliente ya está guardado en la bandeja.
    await _a_humano(_CrmRoto(), "cv_1", "sin_credencial")
