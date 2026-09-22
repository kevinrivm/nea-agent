#!/usr/bin/env python3
"""Prueba de punta a punta de Nea contra Vocero raíz, en modo estándar.

El par tal como se instala: Meta manda el webhook a Nea, Nea lo releva al CRM
y contesta por `/api/bot/*`, y el CRM envía por la Graph API — aquí, el
wa-mock del propio CRM, así que nada sale a Meta ni a WhatsApp. Este guion
hace de Meta y de cliente: arma los payloads con la forma real de la Cloud
API, los firma con META_APP_SECRET como Meta, y comprueba lo observable en la
API del CRM, su base y el outbox del wa-mock.

Levanta el CRM (`next dev`) y Nea (uvicorn) con un entorno fabricado para la
corrida (secretos aleatorios de prueba), pone entre Nea y OpenRouter un
medidor local que cuenta los tokens y corta al llegar al presupuesto, corre
los escenarios y apaga todo al salir. El Postgres —dos bases, una para cada
servicio— lo pone quien corre la prueba.

La llave del modelo sale de `LLM_API_KEY` en el entorno o de un `--env-file`;
nunca por argv, y nunca se imprime.

    python scripts/e2e_contra_raiz.py --crm-dir ../vocero-crm \
        --env-file ../.env --env-file ./runtime-e2e.env

Sale con 0 si todos los escenarios pasan, 1 si alguno falla y 2 si no se pudo
montar el par. Detalle: README, «Prueba de punta a punta contra Vocero raíz».
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

RAIZ_NEA = Path(__file__).resolve().parent.parent
OPENROUTER = "https://openrouter.ai/api/v1"
MODELO = "z-ai/glm-5.3-flash"
ZONA = "America/Mexico_City"
TZ = ZoneInfo(ZONA)
# Sala del conector «enlace fijo»: la confirmación tiene que traer ESTE enlace.
SALA = "https://meet.jit.si/vocero-e2e-sala"
WABA = "WABA-E2E-PAR"
PN = "PN-E2E-PAR"
PN_DISPLAY = "5215500000000"
TOKEN_WA = "tok-e2e-par"  # el wa-mock acepta cualquiera sin el sufijo -invalid
OPERADOR = {
    "email": "e2e-par@vocero.test",
    "password": "password-e2e-par-123",
    "name": "Operador E2E",
}
CLAVES_DE_ARCHIVO = ("LLM_API_KEY", "CRM_DATABASE_URL", "NEA_DATABASE_URL")
# Nada del entorno de quien corre la prueba que huela a secreto o a
# configuración de uno de los dos servicios llega a los procesos hijos: cada
# variable que leen la pone este guion.
_NO_HEREDAR = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|OPENROUTER|OPENAI|^LLM_|^META_|"
    r"^CRM_|^NEA_|VOCERO|^AGENDA|^ALLOWED_WA|^TESTER_WA|^WA_MOCK|^BOT_API|"
    r"^APP_BASE|^BETTER_AUTH|^ENCRYPTION|^NEXT_|^NODE_ENV$|^PORT$|^CHANNELS$|"
    r"^ATRIBUCION$|^ALLOW_SIGNUP$|^ZOOM_|^GOOGLE_|^MEDIA_DIR$|^AGENT_|"
    r"^HISTORY_WINDOW$|^COALESCE_|^FOLLOWUP_|^STALL_|^TYPING_|^BRIEF_PATH$|"
    r"^CAPTURE_PAYLOADS$|^SOURCE_COMMIT$|^VERIFY_TOKEN$|^TURN_RETRY_|^RELAY_",
    re.I,
)

# El negocio de la prueba: lo que un dueño llena en Ajustes → Agente y en la
# base de conocimiento. Sin esto Nea no tendría de qué hablar.
PERFIL = {
    "enabled": True,
    "name": "Nea",
    "tone": "Cálida, clara y breve; tutea con respeto.",
    "instructions": (
        "Atiendes el WhatsApp de Estudio Aurora, consultoría de marketing "
        "digital para negocios locales en la Ciudad de México. El objetivo es "
        "agendar una sesión de diagnóstico gratuita de 30 minutos por "
        "videollamada con Laura, la directora."
    ),
    "escalationRules": "Si piden hablar con una persona, pasa la conversación al equipo.",
    "greeting": None,
}
CONOCIMIENTO = (
    "Servicios de Estudio Aurora y precios (MXN, IVA incluido):\n"
    "- Sesión de diagnóstico de marketing: gratis, 30 minutos por videollamada.\n"
    "- Gestión de redes sociales (Instagram y Facebook): $4,500 al mes.\n"
    "- Campañas de anuncios en Meta: $6,000 al mes más la inversión en anuncios.\n"
    "- Página web de una sola sección: $9,000, pago único.\n"
    "Horario de atención: lunes a sábado de 9:00 a 19:00."
)


# ── Utilidades ──────────────────────────────────────────────────────────────


def leer_valores(archivos: list[str]) -> dict[str, str]:
    """Las claves que usa la prueba, de los `--env-file` y del entorno.

    De cada archivo solo se toman CLAVES_DE_ARCHIVO: el vault de operación
    trae más secretos, y ninguno tiene por qué llegar a un proceso hijo. El
    entorno manda sobre los archivos; entre archivos, gana el primero.
    """
    valores: dict[str, str] = {}
    for ruta in archivos:
        for linea in Path(ruta).read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            clave = clave.strip().removeprefix("export ").strip()
            if clave in CLAVES_DE_ARCHIVO and clave not in valores:
                valores[clave] = valor.strip().strip('"').strip("'")
    for clave in CLAVES_DE_ARCHIVO:
        if os.environ.get(clave):
            valores[clave] = os.environ[clave]
    return valores


def entorno_base() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _NO_HEREDAR.search(k)}


def puerto_ocupado(puerto: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def hasta(cond: Callable[[], Any], timeout: float, paso: float = 0.4) -> Any:
    """Sondea hasta que `cond` devuelva algo verdadero; None al vencer."""
    fin = time.monotonic() + timeout
    while True:
        try:
            valor = cond()
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            valor = None
        if valor:
            return valor
        if time.monotonic() > fin:
            return None
        time.sleep(paso)


def canonica(tel: str) -> str:
    """521XXXXXXXXXX → 52XXXXXXXXXX, igual que el CRM y Nea."""
    tel = (tel or "").strip()
    if tel.startswith("521") and len(tel) == 13 and tel.isdigit():
        return "52" + tel[3:]
    return tel


def firmar(secreto: str, cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def envoltura(value: dict[str, Any]) -> dict[str, Any]:
    """Un webhook de la Cloud API con la forma exacta que manda Meta."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": PN_DISPLAY,
                                "phone_number_id": PN,
                            },
                            **value,
                        },
                    }
                ],
            }
        ],
    }


_MARKDOWN = [
    ("negritas **", re.compile(r"\*\*")),
    ("subrayado __", re.compile(r"__")),
    ("encabezado #", re.compile(r"(?m)^\s{0,3}#{1,6}\s")),
    ("bloque ```", re.compile(r"```")),
    ("enlace [texto](url)", re.compile(r"\[[^\]]+\]\((?:https?://|www\.)[^)]+\)")),
]


# Lo que Nea no puede afirmar con el reparto a la vista: que un día solo tiene
# mañana (o tarde), o que en una franja no hay. El e2e contra raíz lo cazó:
# «Mañana martes solo tengo por la mañana: 10:00, 10:30 u 11:00». Decir lo
# que ve («solo alcanzo a ver…», «no veo si hay en la tarde») sí se vale.
_NIEGA_HORAS = re.compile(
    r"\b(solo|sólo|únicamente)\s+(tengo|hay|me quedan?|quedan?)\b[^.?!\n]*\b(mañana|tarde)\b"
    r"|\bno\s+(tengo|hay|me queda|quedan?)\b[^.?!\n]*\b(en|por)\s+la\s+(mañana|tarde)\b",
    re.I,
)
# Lo que Nea no puede ofrecer en modo estándar: el CRM raíz no manda recordatorios.
_RECORDATORIO = re.compile(r"recordatorio|recordarte|te recuerdo", re.I)


def artefactos_markdown(texto: str) -> list[str]:
    return [nombre for nombre, patron in _MARKDOWN if patron.search(texto or "")]


def _patrones_hora(h: int, m: int) -> list[str]:
    h12 = h % 12 or 12
    patrones = [rf"(?<![\d:]){h:02d}:{m:02d}\b", rf"(?<![\d:]){h}:{m:02d}\b", rf"(?<![\d:]){h12}:{m:02d}\b"]
    if m == 0:
        patrones += [
            rf"(?<![\d:]){h12}\s*(?:p\.?\s?m\.?|pm\b|de la tarde|de la noche|hrs?\b|horas)",
            rf"(?<![\d:]){h}\s*(?:hrs?\b|horas)",
        ]
    if m == 30:
        patrones.append(rf"(?<![\d:]){h12}\s*y\s*media")
    return patrones


def posicion_hora(texto: str, h: int, m: int) -> int | None:
    """Dónde nombra el texto esta hora del día (15:00, 3:00 p. m., 3 pm…)."""
    t = (texto or "").lower()
    posiciones = [x.start() for p in _patrones_hora(h, m) if (x := re.search(p, t))]
    return min(posiciones) if posiciones else None


def menciona_hora(texto: str, h: int, m: int) -> bool:
    return posicion_hora(texto, h, m) is not None


def en_la_zona(valor: datetime) -> datetime:
    """Un timestamp de la base del CRM (sin zona = UTC) en la hora del negocio."""
    return (valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)).astimezone(TZ)


def percentil(valores: list[float], p: float) -> float | None:
    """Percentil por rango más cercano (con pocas muestras es lo honesto)."""
    if not valores:
        return None
    orden = sorted(valores)
    k = max(0, min(len(orden) - 1, int(round(p / 100 * len(orden) + 0.5)) - 1))
    return orden[k]


def instante(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def consulta(dsn: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    """Lectura directa de la base del CRM (lo que la API no enseña)."""
    import asyncpg  # dependencia de Nea; se importa aquí para no exigirla antes

    async def _q() -> list[dict[str, Any]]:
        con = await asyncpg.connect(dsn)
        try:
            return [dict(r) for r in await con.fetch(sql, *args)]
        finally:
            await con.close()

    return asyncio.run(_q())


# ── Medidor del modelo ──────────────────────────────────────────────────────


class Medidor:
    """Pasarela local hacia OpenRouter: cuenta tokens y corta al tope.

    Nea le habla como si fuera OpenRouter (`LLM_BASE_URL` apunta aquí) y le
    manda su propia llave; el medidor reenvía los bytes tal cual. Lo único
    que añade: se niega a otro modelo que el de la prueba y, cuando lo gastado
    más una reserva por llamada alcanzaría el presupuesto, responde 402 sin
    llamar — para Nea es un proveedor caído (silencio + handoff `error`).
    """

    def __init__(self, destino: str, modelo: str, precios: dict[str, float], presupuesto: float):
        self.destino = destino.rstrip("/")
        self.modelo = modelo
        self.precios = precios
        self.presupuesto = presupuesto
        self.gastado = 0.0
        self.reservado = 0.0
        self.rechazadas = 0
        self.llamadas: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._server: ThreadingHTTPServer | None = None

    @property
    def agotado(self) -> bool:
        return self.rechazadas > 0 or self.gastado >= self.presupuesto

    def _reserva(self) -> float:
        mayor = max((c["costo_usd"] for c in self.llamadas), default=0.0)
        return max(0.01, 2 * mayor)

    def abrir(self) -> float | None:
        with self._lock:
            reserva = self._reserva()
            if self.gastado + self.reservado + reserva > self.presupuesto:
                self.rechazadas += 1
                return None
            self.reservado += reserva
            return reserva

    def costo(self, usage: dict[str, Any]) -> float:
        if isinstance(usage.get("cost"), (int, float)):
            return float(usage["cost"])  # lo que OpenRouter dice que cobró
        prompt = int(usage.get("prompt_tokens") or 0)
        cache = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        salida = int(usage.get("completion_tokens") or 0)
        p = self.precios
        return (prompt - cache) * p["prompt"] + cache * p.get("input_cache_read", p["prompt"]) + salida * p["completion"]

    def cerrar(self, reserva: float, estado: int, dur: float, datos: dict[str, Any]) -> None:
        usage = datos.get("usage") or {}
        costo = self.costo(usage) if usage else 0.0
        with self._lock:
            self.reservado -= reserva
            self.gastado += costo
            self.llamadas.append(
                {
                    "t": round(time.time(), 2),
                    "estado": estado,
                    "duracion_s": round(dur, 2),
                    "proveedor": datos.get("provider"),
                    "prompt": int(usage.get("prompt_tokens") or 0),
                    "cache": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                    "salida": int(usage.get("completion_tokens") or 0),
                    "costo_usd": round(costo, 6),
                    "costo_reportado": isinstance(usage.get("cost"), (int, float)),
                }
            )

    def resumen(self) -> dict[str, Any]:
        with self._lock:
            ok = [c for c in self.llamadas if c["estado"] == 200]
            return {
                "modelo": self.modelo,
                "presupuesto_usd": self.presupuesto,
                "gastado_usd": round(self.gastado, 6),
                "llamadas": len(self.llamadas),
                "llamadas_ok": len(ok),
                "rechazadas_por_presupuesto": self.rechazadas,
                "tokens_prompt": sum(c["prompt"] for c in self.llamadas),
                "tokens_cache": sum(c["cache"] for c in self.llamadas),
                "tokens_salida": sum(c["salida"] for c in self.llamadas),
                "duracion_mediana_s": round(statistics.median([c["duracion_s"] for c in ok]), 2) if ok else None,
                "precios_por_token": self.precios,
            }

    def arrancar(self, puerto: int) -> None:
        medidor = self

        class Pasarela(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:  # nada de cabeceras al log
                pass

            def _responder(self, estado: int, cuerpo: bytes, tipo: str = "application/json") -> None:
                self.send_response(estado)
                self.send_header("content-type", tipo)
                self.send_header("content-length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def _error(self, estado: int, mensaje: str) -> None:
                self._responder(estado, json.dumps({"error": {"message": mensaje, "code": estado}}).encode())

            def do_GET(self) -> None:
                self._pasar("GET")

            def do_POST(self) -> None:
                self._pasar("POST")

            def _pasar(self, metodo: str) -> None:
                largo = int(self.headers.get("content-length") or 0)
                cuerpo = self.rfile.read(largo) if largo else b""
                if not self.path.startswith("/api/v1/"):
                    return self._error(404, "medidor: ruta desconocida")
                es_chat = self.path.split("?")[0].rstrip("/").endswith("/chat/completions")
                reserva = 0.0
                if es_chat:
                    try:
                        modelo = (json.loads(cuerpo) or {}).get("model")
                    except ValueError:
                        modelo = None
                    if modelo != medidor.modelo:
                        return self._error(400, f"medidor: solo se permite {medidor.modelo}")
                    abierta = medidor.abrir()
                    if abierta is None:
                        return self._error(402, "medidor: presupuesto de la prueba agotado")
                    reserva = abierta
                fuera = {"host", "content-length", "accept-encoding", "connection", "keep-alive", "transfer-encoding"}
                cabeceras = {k: v for k, v in self.headers.items() if k.lower() not in fuera}
                t0 = time.monotonic()
                try:
                    r = medidor._http.request(metodo, medidor.destino + self.path[len("/api/v1"):], content=cuerpo, headers=cabeceras)
                except httpx.HTTPError as exc:
                    if es_chat:
                        medidor.cerrar(reserva, 0, time.monotonic() - t0, {})
                    return self._error(502, f"medidor: OpenRouter no respondió ({type(exc).__name__})")
                if es_chat:
                    try:
                        datos = r.json() if r.content else {}
                    except ValueError:
                        datos = {}
                    medidor.cerrar(reserva, r.status_code, time.monotonic() - t0, datos if isinstance(datos, dict) else {})
                self._responder(r.status_code, r.content, r.headers.get("content-type", "application/json"))

        class Servidor(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                # Nea apagándose con una conexión keep-alive abierta: no es error.
                if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        self._server = Servidor(("127.0.0.1", puerto), Pasarela)
        threading.Thread(target=self._server.serve_forever, name="medidor", daemon=True).start()

    def detener(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._http.close()


def precios_del_modelo(modelo: str) -> dict[str, float]:
    """Precio por token, del catálogo público de OpenRouter.

    Si el catálogo no contesta se usan precios pesimistas: con ellos el
    medidor corta ANTES de lo debido, nunca después.
    """
    try:
        catalogo = httpx.get(f"{OPENROUTER}/models", timeout=30).json()
        for m in catalogo.get("data", []):
            if m.get("id") == modelo:
                p = m.get("pricing") or {}
                return {k: float(p[k]) for k in ("prompt", "completion", "input_cache_read") if p.get(k) is not None}
    except (httpx.HTTPError, ValueError):
        pass
    return {"prompt": 2e-6, "completion": 8e-6}


def uso_de_la_llave(llave: str) -> float | None:
    """Uso acumulado de la llave según OpenRouter (informativo: puede ser
    compartida con otros servicios)."""
    r = httpx.get(f"{OPENROUTER}/key", headers={"authorization": f"Bearer {llave}"}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter rechazó la llave (GET /key → {r.status_code})")
    return (r.json().get("data") or {}).get("usage")


# ── Procesos ────────────────────────────────────────────────────────────────


class Proceso:
    def __init__(self, nombre: str, cmd: list[str], cwd: Path, env: dict[str, str], log: Path):
        self.nombre, self.cmd, self.cwd, self.env, self.log = nombre, cmd, cwd, env, log
        self.p: subprocess.Popen[bytes] | None = None
        self._f: Any = None

    def arrancar(self) -> None:
        self._f = open(self.log, "ab")
        self._f.write(f"\n===== {datetime.now(TZ).isoformat(timespec='seconds')} arranca {self.nombre} =====\n".encode())
        self._f.flush()
        extra: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        )
        self.p = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env, stdout=self._f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **extra
        )

    def vivo(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def detener(self) -> None:
        if self.vivo():
            assert self.p is not None
            if os.name == "nt":
                # El árbol entero: `next dev` deja hijos que se quedan con el puerto.
                subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(self.p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.p.wait(20)
            except subprocess.TimeoutExpired:
                self.p.kill()
        if self._f is not None:
            self._f.close()
            self._f = None


# ── Clientes HTTP ───────────────────────────────────────────────────────────


class Crm:
    """La API del CRM como la usa el operador (cookie de Better Auth)."""

    def __init__(self, base: str, bot_key: str):
        self.base = base
        self.bot_key = bot_key
        self.cookies: dict[str, str] = {}
        self.http = httpx.Client(timeout=90)

    def api(self, metodo: str, ruta: str, *, cuerpo: Any = None, bot: bool = False, headers: dict[str, str] | None = None, timeout: float = 90) -> httpx.Response:
        h = {"origin": self.base}  # Better Auth valida Origin
        if self.cookies:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if bot:
            h["x-api-key"] = self.bot_key
        h.update(headers or {})
        r = self.http.request(metodo, self.base + ruta, json=cuerpo, headers=h, timeout=timeout)
        for c in r.headers.get_list("set-cookie"):
            par = c.split(";", 1)[0]
            if "=" in par:
                k, v = par.split("=", 1)
                self.cookies[k.strip()] = v.strip()
        return r

    def json(self, metodo: str, ruta: str, **kw: Any) -> tuple[int, Any]:
        r = self.api(metodo, ruta, **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None


@dataclass
class Cliente:
    etiqueta: str
    telefono: str  # como lo manda Meta: 521 + 10 dígitos
    nombre: str
    conv_id: str | None = None
    contact_id: str | None = None
    vistos: set[str] = field(default_factory=set)  # wamids de salida ya contados
    eventos: list[dict[str, Any]] = field(default_factory=list)
    ultima_salida: dict[str, Any] | None = None
    cita: dict[str, Any] | None = None

    @property
    def canonico(self) -> str:
        return canonica(self.telefono)


def telefono_de_prueba() -> str:
    return "52155" + "".join(random.choice("0123456789") for _ in range(8))


# ── La prueba ───────────────────────────────────────────────────────────────


class Prueba:
    def __init__(self, args: argparse.Namespace, valores: dict[str, str]):
        self.args = args
        self.llave = valores["LLM_API_KEY"]
        self.crm_db = valores["CRM_DATABASE_URL"]
        self.nea_db = valores["NEA_DATABASE_URL"]
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.crm_dir = Path(args.crm_dir).resolve()
        self.crm_url = f"http://localhost:{args.crm_port}"
        self.nea_url = f"http://127.0.0.1:{args.nea_port}"
        self.sec = {
            "bot": secrets.token_hex(32),
            "webhook": secrets.token_hex(16),
            "app": secrets.token_hex(24),  # META_APP_SECRET, el mismo en los dos
            "auth": secrets.token_hex(32),
            "cifrado": base64.b64encode(secrets.token_bytes(32)).decode(),
            "verify": secrets.token_hex(16),
        }
        corrida = secrets.token_hex(2)
        self.clientes = {
            "A": Cliente("A", telefono_de_prueba(), f"Mariana Prueba {corrida}"),
            "B": Cliente("B", telefono_de_prueba(), f"Jorge Prueba {corrida}"),
            "C": Cliente("C", telefono_de_prueba(), f"Lucía Prueba {corrida}"),
        }
        self.crm = Crm(self.crm_url, self.sec["bot"])
        self.nea = httpx.Client(base_url=self.nea_url, timeout=30)
        self.medidor: Medidor | None = None
        self.proc_crm: Proceso | None = None
        self.proc_nea: Proceso | None = None
        self.resultados: list[dict[str, Any]] = []
        self.latencias: list[dict[str, Any]] = []
        self.uso_llave_inicio: float | None = None
        self.crm_sano_en = 0.0
        self.seleccion = args.escenarios

    # ── Montaje ─────────────────────────────────────────────────────────────

    def montar(self) -> None:
        for puerto in (self.args.crm_port, self.args.nea_port, self.args.medidor_port):
            if puerto_ocupado(puerto):
                raise RuntimeError(f"el puerto {puerto} ya está ocupado — apaga lo que lo use o elige otro")
        node = shutil.which("node")
        next_bin = self.crm_dir / "node_modules" / "next" / "dist" / "bin" / "next"
        if not node or not next_bin.exists():
            raise RuntimeError("falta node o el CRM sin dependencias (corre `pnpm install` en --crm-dir)")

        self.uso_llave_inicio = uso_de_la_llave(self.llave)
        print(f"llave del modelo: …{self.llave[-4:]} (válida)")
        self.medidor = Medidor(OPENROUTER, self.args.modelo, precios_del_modelo(self.args.modelo), self.args.presupuesto)
        self.medidor.arrancar(self.args.medidor_port)

        # CRM: migraciones y `next dev` con la bandera de agenda, el wa-mock
        # como Graph API y SIN proveedor de IA propio (Nea es el único cerebro).
        env = entorno_base() | {
            "NODE_ENV": "development",
            "NEXT_TELEMETRY_DISABLED": "1",
            "APP_BASE_URL": self.crm_url,
            "DATABASE_URL": self.crm_db,
            "BETTER_AUTH_SECRET": self.sec["auth"],
            "ENCRYPTION_KEY": self.sec["cifrado"],
            "META_WEBHOOK_VERIFY_TOKEN": self.sec["webhook"],
            "META_APP_SECRET": self.sec["app"],
            "META_GRAPH_API_VERSION": "v25.0",
            "META_GRAPH_BASE_URL": f"{self.crm_url}/api/dev/wa-mock/graph",
            "WA_MOCK_ENABLED": "true",
            "BOT_API_KEY": self.sec["bot"],
            "AGENDA": "on",
            "MEDIA_DIR": str(self.out / "crm-media"),
        }
        migrar = subprocess.run(
            [node, "scripts/migrate.mjs"],
            cwd=self.crm_dir,
            env=env | {"MIGRATIONS_DIR": str(self.crm_dir / "drizzle")},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if migrar.returncode != 0:
            raise RuntimeError(f"migraciones del CRM fallaron: {migrar.stdout[-400:]} {migrar.stderr[-400:]}")
        self.proc_crm = Proceso(
            "crm", [node, str(next_bin), "dev", "-p", str(self.args.crm_port)], self.crm_dir, env, self.out / "crm.log"
        )
        self.arrancar_crm()

        # El dueño: registro (o login si la base ya tiene organización),
        # número conectado, agente y agenda configurados.
        st, _ = self.crm.json("POST", "/api/auth/sign-up/email", cuerpo=OPERADOR)
        if st >= 400:
            st, cuerpo = self.crm.json("POST", "/api/auth/sign-in/email", cuerpo={k: OPERADOR[k] for k in ("email", "password")})
            if st >= 400:
                raise RuntimeError(f"ni registro ni login del operador ({st}): {cuerpo}")
        st, cuerpo = self.crm.json("PUT", "/api/settings/whatsapp", cuerpo={"wabaId": WABA, "phoneNumberId": PN, "token": TOKEN_WA})
        if st != 200:
            raise RuntimeError(f"conectar el número falló ({st}): {cuerpo}")
        st, cuerpo = self.crm.json("PUT", "/api/agent/profile", cuerpo=PERFIL)
        if st != 200:
            raise RuntimeError(f"perfil del agente ({st}): {cuerpo}")
        st, kb = self.crm.json("GET", "/api/kb")
        entradas = (kb or {}).get("entries") or (kb or {}).get("kb") or []
        if not any("Estudio Aurora" in json.dumps(e, ensure_ascii=False) for e in entradas):
            st, cuerpo = self.crm.json("POST", "/api/kb", cuerpo={"kind": "block", "content": CONOCIMIENTO})
            if st >= 400:
                raise RuntimeError(f"base de conocimiento ({st}): {cuerpo}")
        dias = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        st, cuerpo = self.crm.json(
            "PUT",
            "/api/calendar/settings",
            cuerpo={
                "weeklyHours": {d: [{"start": "09:00", "end": "19:00"}] for d in dias},
                "timezone": ZONA,
                "slotMinutes": 30,
                "connector": "enlace-fijo",
                "meetingLink": SALA,
            },
        )
        if st != 200:
            raise RuntimeError(f"Ajustes → Agenda ({st}): {cuerpo}")

        # Nea en modo estándar, fuera de su carpeta: así no lee ningún `.env`
        # que alguien haya dejado ahí; todo lo que usa lo pone este guion.
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=RAIZ_NEA, capture_output=True, text=True).stdout.strip()
        nea_env = entorno_base() | {
            "PYTHONPATH": str(RAIZ_NEA),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "VERIFY_TOKEN": self.sec["verify"],
            "META_APP_SECRET": self.sec["app"],
            "CRM_BASE_URL": self.crm_url,
            "CRM_WEBHOOK_URL": f"{self.crm_url}/api/webhooks/wa/{self.sec['webhook']}",
            "CRM_BOT_API_KEY": self.sec["bot"],
            "DATABASE_URL": self.nea_db,
            "LLM_API_KEY": self.llave,
            "LLM_BASE_URL": f"http://127.0.0.1:{self.args.medidor_port}/api/v1",
            "LLM_MODEL": self.args.modelo,
            "AGENT_TIMEZONE": ZONA,
            "HISTORY_WINDOW": "20",
            "ALLOWED_WA_IDS": ",".join(c.canonico for c in self.clientes.values()),
            "TESTER_WA_IDS": "",
            "VOCERO_MODE": "",
            "CRM_BRAIN_SECRET": "",
            "CRM_ORGANIZATION": "",
            "BRIEF_PATH": "",
            "PORT": str(self.args.nea_port),
            "SOURCE_COMMIT": commit,
        }
        nea_cwd = self.out / "nea-cwd"
        nea_cwd.mkdir(exist_ok=True)
        self.proc_nea = Proceso(
            "nea",
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.args.nea_port)],
            nea_cwd,
            nea_env,
            self.out / "nea.log",
        )
        self.proc_nea.arrancar()
        if not hasta(lambda: self.nea.get("/health").status_code == 200, 90, 0.5):
            raise RuntimeError("Nea no respondió /health a tiempo (ver nea.log)")

    def arrancar_crm(self, calentar: bool = True) -> None:
        assert self.proc_crm is not None
        self.proc_crm.arrancar()
        if not hasta(lambda: self.crm.api("GET", "/api/health", timeout=120).status_code == 200, 300, 1.0):
            raise RuntimeError("el CRM no respondió /api/health a tiempo (ver crm.log)")
        self.crm_sano_en = time.time()
        if calentar:
            self.calentar()

    def calentar(self) -> None:
        """`next dev` compila cada ruta en su primera petición (10-15 s). Se
        compilan antes para que la latencia medida sea la de Nea, no la del
        compilador. Todas son lecturas o cuerpos inválidos: ninguna escribe."""
        wh = self.sec["webhook"]
        rutas: list[tuple[str, str, bool, Any]] = [
            ("GET", f"/api/webhooks/wa/{wh}?hub.mode=subscribe&hub.verify_token={wh}&hub.challenge=calentar", False, None),
            ("GET", "/api/bot/context?waIdentity=520000000000", True, None),
            ("GET", "/api/bot/profile", True, None),
            ("POST", "/api/bot/messages", True, {}),
            ("POST", "/api/bot/typing", True, {}),
            ("GET", "/api/bot/availability", True, None),
            ("POST", "/api/bot/bookings", True, {}),
            ("POST", "/api/bot/handoff", True, {}),
            ("PUT", "/api/bot/ficha", True, {}),
            ("GET", "/api/conversations", False, None),
            ("GET", "/api/conversations/cv_calentar", False, None),
            ("GET", "/api/conversations/cv_calentar/messages", False, None),
            ("GET", "/api/contacts/ct_calentar", False, None),
            ("GET", "/api/bookings", False, None),
            ("GET", "/api/pipeline/stages", False, None),
            ("GET", "/api/dev/wa-mock/outbox", False, None),
            ("GET", f"/api/dev/wa-mock/graph/v25.0/{WABA}/subscribed_apps", False, None),
        ]
        for metodo, ruta, bot, cuerpo in rutas:
            try:
                self.crm.api(metodo, ruta, bot=bot, cuerpo=cuerpo, timeout=120)
            except httpx.HTTPError:
                pass

    def desmontar(self) -> None:
        for proc in (self.proc_nea, self.proc_crm):
            if proc is not None:
                proc.detener()
        if self.medidor is not None:
            self.medidor.detener()

    # ── El cliente y Meta ───────────────────────────────────────────────────

    def _postear(self, payload: dict[str, Any]) -> float:
        cuerpo = json.dumps(payload, ensure_ascii=False).encode()
        r = self.nea.post(
            "/webhook",
            content=cuerpo,
            headers={"content-type": "application/json", "x-hub-signature-256": firmar(self.sec["app"], cuerpo)},
        )
        t = time.time()
        if r.status_code != 200:
            raise RuntimeError(f"Nea rechazó el webhook ({r.status_code}): {r.text[:200]}")
        return t

    def enviar(self, c: Cliente, texto: str) -> float:
        wamid = "wamid.e2e." + secrets.token_hex(12)
        t = self._postear(
            envoltura(
                {
                    "contacts": [{"profile": {"name": c.nombre}, "wa_id": c.telefono}],
                    "messages": [
                        {"from": c.telefono, "id": wamid, "timestamp": str(int(time.time())), "type": "text", "text": {"body": texto}}
                    ],
                }
            )
        )
        c.eventos.append({"t": t, "quien": "cliente", "texto": texto, "wamid": wamid})
        return t

    def enviar_estado(self, c: Cliente, wamid: str, estado: str) -> float:
        t = self._postear(
            envoltura({"statuses": [{"id": wamid, "status": estado, "timestamp": str(int(time.time())), "recipient_id": c.telefono}]})
        )
        c.eventos.append({"t": t, "quien": "estado", "texto": f"Meta: {estado} de {wamid}"})
        return t

    @staticmethod
    def nota(c: Cliente, texto: str) -> None:
        """Un hecho del CRM o de la prueba, para que la transcripción se entienda."""
        c.eventos.append({"t": time.time(), "quien": "estado", "texto": texto})

    def outbox(self) -> list[dict[str, Any]]:
        return self.crm.json("GET", "/api/dev/wa-mock/outbox")[1]["outbox"]

    def _nuevos(self, c: Cliente) -> list[dict[str, Any]]:
        return [e for e in self.outbox() if canonica(e.get("to", "")) == c.canonico and e.get("waMessageId") not in c.vistos]

    @staticmethod
    def _texto(e: dict[str, Any]) -> str:
        cuerpo = e.get("body") or {}
        if cuerpo.get("type", "text") == "text":
            return str((cuerpo.get("text") or {}).get("body") or "")
        return json.dumps(cuerpo, ensure_ascii=False)

    def _registrar(self, c: Cliente, entradas: list[dict[str, Any]], t0: float, turno: str | None) -> list[dict[str, Any]]:
        salidas = []
        for e in sorted(entradas, key=lambda e: e.get("n", 0)):
            c.vistos.add(e["waMessageId"])
            s = {"wamid": e["waMessageId"], "texto": self._texto(e), "latencia_s": round(instante(e["at"]) - t0, 2), "at": e["at"]}
            c.eventos.append({"t": instante(e["at"]), "quien": "nea", **s})
            salidas.append(s)
        if salidas and turno:
            self.latencias.append({"turno": turno, "cliente": c.etiqueta, "s": salidas[0]["latencia_s"]})
        return salidas

    def esperar_respuesta(self, c: Cliente, t0: float, turno: str | None, timeout: float = 120) -> list[dict[str, Any]]:
        """Lo que Nea mandó a este cliente tras `t0`: espera el primero y
        recoge la ráfaga (un turno puede mandar más de un mensaje)."""
        primeros = hasta(lambda: self._nuevos(c), timeout)
        if not primeros:
            return []
        todos = primeros
        quieto = time.monotonic() + 3.0
        while time.monotonic() < quieto:
            time.sleep(0.5)
            mas = self._nuevos(c)
            if len(mas) > len(todos):
                todos, quieto = mas, time.monotonic() + 3.0
        return self._registrar(c, todos, t0, turno)

    def esperar_silencio(self, c: Cliente, t0: float, segundos: float) -> list[dict[str, Any]]:
        """Vacío si Nea calló durante `segundos`; si no, lo que mandó."""
        inesperados = hasta(lambda: self._nuevos(c), segundos)
        if not inesperados:
            c.eventos.append({"t": time.time(), "quien": "estado", "texto": f"silencio de Nea durante {segundos:.0f} s"})
            return []
        return self._registrar(c, inesperados, t0, None)

    # ── Lo que ve el operador ───────────────────────────────────────────────

    def conversacion(self, c: Cliente) -> dict[str, Any] | None:
        """La conversación como la pinta la bandeja (el CRM no tiene GET por id)."""
        convs = self.crm.json("GET", "/api/conversations")[1]["conversations"]
        mia = next(
            (
                v
                for v in convs
                if v["id"] == c.conv_id or (not c.conv_id and canonica((v.get("contact") or {}).get("phone") or "") == c.canonico)
            ),
            None,
        )
        if mia:
            c.conv_id, c.contact_id = mia["id"], mia["contact"]["id"]
        return mia

    def mensajes(self, c: Cliente) -> list[dict[str, Any]]:
        if not c.conv_id and not self.conversacion(c):
            return []
        return self.crm.json("GET", f"/api/conversations/{c.conv_id}/messages")[1]["messages"]

    def mensaje_en_crm(self, c: Cliente, texto: str, direccion: str, estado: str | None = None) -> dict[str, Any] | None:
        for m in self.mensajes(c):
            if m.get("direction") == direccion and (m.get("text") or "").strip() == texto.strip():
                if estado is None or m.get("status") == estado:
                    return m
        return None

    def esperar_entrante(self, c: Cliente, texto: str, t0: float, timeout: float) -> float | None:
        m = hasta(lambda: self.mensaje_en_crm(c, texto, "in"), timeout, 0.3)
        # Cuándo lo vio este guion: el `createdAt` del CRM es la hora de
        # WhatsApp del mensaje, no la de su llegada.
        return round(time.time() - t0, 2) if m else None

    def contacto(self, c: Cliente) -> dict[str, Any]:
        if not c.contact_id:
            self.conversacion(c)
        return self.crm.json("GET", f"/api/contacts/{c.contact_id}")[1]

    def ficha(self, c: Cliente) -> dict[str, Any]:
        return (self.contacto(c).get("contact") or {}).get("ficha") or {}

    def cita_de(self, c: Cliente) -> dict[str, Any] | None:
        citas = self.crm.json("GET", "/api/bookings")[1]["bookings"]
        return next(
            (b for b in citas if (b.get("contact") or {}).get("id") == c.contact_id and b.get("status") == "agendada"),
            None,
        )

    def salud_nea(self) -> dict[str, Any]:
        return self.nea.get("/health").json()

    # ── Escenarios ──────────────────────────────────────────────────────────

    def correr(self, n: int, nombre: str, fn: Callable[[], tuple[bool, dict[str, Any]]]) -> None:
        if n not in self.seleccion:
            return
        t = time.monotonic()
        if self.medidor is not None and self.medidor.agotado:
            ok, ev = False, {"error": "presupuesto del modelo agotado antes de correr"}
        else:
            try:
                ok, ev = fn()
            except Exception as exc:  # un escenario roto no tumba a los demás
                ok, ev = False, {"excepcion": f"{type(exc).__name__}: {exc}"}
        self.resultados.append({"n": n, "nombre": nombre, "ok": bool(ok), "duracion_s": round(time.monotonic() - t, 1), "evidencia": ev})
        print(f"{'PASS' if ok else 'FAIL'}  {n}. {nombre}", flush=True)
        if not ok:
            print("      " + json.dumps(ev, ensure_ascii=False, default=str)[:600], flush=True)

    def e1_primer_contacto(self) -> tuple[bool, dict[str, Any]]:
        a = self.clientes["A"]
        texto = "Hola, buenas tardes, ¿qué servicios tienen?"
        t0 = self.enviar(a, texto)
        relay_s = self.esperar_entrante(a, texto, t0, 30)
        resp = self.esperar_respuesta(a, t0, "1: primer contacto")
        ev: dict[str, Any] = {"relay_a_bandeja_s": relay_s, "conversationId": a.conv_id, "respuestas": resp}
        if not resp:
            return False, ev
        a.ultima_salida = resp[-1]
        # En WhatsApp el CRM guarda el saliente `pending` al aceptarlo la Graph
        # API; lo avanzan los estados de Meta (escenario 2).
        en_crm = [hasta(lambda s=s: self.mensaje_en_crm(a, s["texto"], "out"), 20) for s in resp]
        ev["mensajes_en_crm"] = [
            {k: m.get(k) for k in ("id", "status", "origin", "aiGenerated")} if m else None for m in en_crm
        ]
        ev["markdown"] = sorted({x for s in resp for x in artefactos_markdown(s["texto"])})
        ok = (
            relay_s is not None
            and relay_s <= 10
            and all(m and m.get("origin") == "ai" and m.get("status") in ("pending", "sent") for m in en_crm)
            and not ev["markdown"]
        )
        return ok, ev

    def e2_estados(self) -> tuple[bool, dict[str, Any]]:
        a = self.clientes["A"]
        s = a.ultima_salida
        if not s:
            return False, {"error": "no hay respuesta del escenario 1 a la cual mandarle estados"}
        ev: dict[str, Any] = {"wamid": s["wamid"]}
        for estado in ("delivered", "read"):
            t0 = self.enviar_estado(a, s["wamid"], estado)
            m = hasta(lambda estado=estado: self.mensaje_en_crm(a, s["texto"], "out", estado), 30)
            ev[estado] = {"segundos": round(time.time() - t0, 2), "messageId": m.get("id")} if m else None
        return bool(ev["delivered"] and ev["read"]), ev

    def e3_agenda(self) -> tuple[bool, dict[str, Any]]:
        a = self.clientes["A"]
        if not a.contact_id:
            return False, {"error": "el contacto del escenario 1 no está en el CRM"}
        antes = self.contacto(a).get("stage") or {}
        etapas = self.crm.json("GET", "/api/pipeline/stages")[1]["stages"]
        siguiente = next((e for e in etapas if e["position"] > antes.get("position", -1) and e.get("kind") == "open"), None)
        # Lo que la agenda del negocio tiene libre mañana en la tarde, según
        # la vista del operador (no registra oferta: solo lee).
        manana = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        libres = self.crm.json("GET", f"/api/calendar/availability?from={manana}&to={manana}")[1].get("slots") or []
        tarde_libre = sorted({s["time"] for s in libres if s.get("dayIso") == manana and str(s.get("time")) >= "12:00"})
        ev: dict[str, Any] = {
            "etapa_antes": antes.get("name"),
            "etapa_esperada": (siguiente or {}).get("name"),
            "libre_en_el_crm_manana_en_la_tarde": tarde_libre,
        }

        t0 = self.enviar(a, "¿Tienen espacio mañana en la tarde?")
        oferta = self.esperar_respuesta(a, t0, "3: pedir horario")
        ev["oferta"] = oferta
        texto_oferta = "\n".join(s["texto"] for s in oferta)
        ofrecidos = []
        for f in consulta(self.crm_db, "select start_utc, label from offered_slot where conversation_id = $1 order by start_utc", a.conv_id):
            local = en_la_zona(f["start_utc"])
            ofrecidos.append(
                {"local": local.strftime("%Y-%m-%d %H:%M"), "label": f["label"], "pos": posicion_hora(texto_oferta, local.hour, local.minute)}
            )
        citados = [o for o in ofrecidos if o["pos"] is not None]
        ev["oferta_registrada_en_el_crm"] = [{"local": o["local"], "citado_en_el_mensaje": o["pos"] is not None} for o in ofrecidos]

        t1 = self.enviar(a, "Me queda bien la primera opción")
        r = self.esperar_respuesta(a, t1, "3: elegir la primera")
        cita = hasta(lambda: self.cita_de(a), 20)
        confirmaciones = [s for s in r if "Enlace de la reunión" in s["texto"] or "confirmada" in s["texto"]]
        if not cita and not confirmaciones:
            # Nea puede pedir que se confirme antes de apartar: se confirma una vez.
            t2 = self.enviar(a, "Sí, confirmo")
            r += self.esperar_respuesta(a, t2, "3: confirmar")
            cita = hasta(lambda: self.cita_de(a), 20)
            confirmaciones = [s for s in r if "Enlace de la reunión" in s["texto"] or "confirmada" in s["texto"]]
        ev["respuestas_eleccion"] = r
        if cita:
            filas = consulta(self.crm_db, "select scheduled_at from booking where id = $1", cita["id"])
            cita = dict(cita, local=en_la_zona(filas[0]["scheduled_at"]).strftime("%Y-%m-%d %H:%M") if filas else None)
            ev["cita"] = {k: cita.get(k) for k in ("id", "status", "source", "local", "connector", "meetingLink", "conversationId")}
            if citados:
                # La «primera opción» es la hora que el mensaje nombra primero
                # (y, si esa hora se ofreció en varios días, el más próximo).
                primera = min(citados, key=lambda o: (o["pos"], o["local"]))
                ev["primera_opcion_del_mensaje"] = primera["local"]
                ev["primera_opcion_respetada"] = cita.get("local") == primera["local"]
        a.cita = cita
        despues = hasta(lambda: (e := self.contacto(a).get("stage") or {}) and e.get("id") != antes.get("id") and e, 15) or (
            self.contacto(a).get("stage") or {}
        )
        ev["etapa_despues"] = despues.get("name")
        conf = confirmaciones[-1]["texto"] if confirmaciones else ""
        checks = {
            "oferta_con_huecos_reales_del_crm": bool(citados),
            # Si el negocio tiene la tarde de mañana libre, pedir «mañana en la
            # tarde» tiene que traer alguna hora de esa tarde.
            "oferta_atiende_manana_en_la_tarde": not tarde_libre
            or any(o["local"].startswith(manana) and o["local"][11:] >= "12:00" for o in citados),
            "cita_creada": bool(cita) and cita.get("status") == "agendada",
            "lead_avanzo_una_etapa": siguiente is not None and despues.get("id") == siguiente["id"],
            "no_niega_horas_que_no_vio": not _NIEGA_HORAS.search(texto_oferta),
            "confirmacion_con_enlace_fijo": "Enlace de la reunión" in conf and SALA in conf,
            "confirmacion_sin_zoom": bool(conf) and "zoom" not in conf.lower(),
        }
        ev["checks"] = checks
        return all(checks.values()), ev

    def e4_contexto_de_la_cita(self) -> tuple[bool, dict[str, Any]]:
        a = self.clientes["A"]
        if not a.cita or not a.cita.get("local"):
            return False, {"error": "no hay cita del escenario 3"}
        # Lo mismo que lee Nea: el bloque `booking` del contexto del CRM.
        bloque = self.crm.json("GET", f"/api/bot/context?waIdentity={a.canonico}", bot=True)[1] or {}
        t0 = self.enviar(a, "¿A qué hora quedó mi cita?")
        r = self.esperar_respuesta(a, t0, "4: preguntar la hora")
        local = a.cita["local"]
        texto = "\n".join(s["texto"] for s in r)
        checks = {
            "menciona_la_hora_de_la_cita": bool(r) and menciona_hora(texto, int(local[11:13]), int(local[14:16])),
            # El CRM raíz no manda recordatorios: ofrecer uno es prometer algo
            # que nadie va a cumplir.
            "sin_ofrecer_recordatorio": not _RECORDATORIO.search(texto),
        }
        ev = {"cita": local, "booking_en_context": bloque.get("booking"), "respuestas": r, "checks": checks}
        return all(checks.values()), ev

    def e5_handoff(self) -> tuple[bool, dict[str, Any]]:
        a = self.clientes["A"]
        t0 = self.enviar(a, "Quiero hablar con una persona")
        r = self.esperar_respuesta(a, t0, "5: pedir humano")
        conv = hasta(lambda: (v := self.conversacion(a)) and v.get("handoffAt") and v, 30) or self.conversacion(a) or {}
        self.nota(a, f"CRM: handoffAt={conv.get('handoffAt')} motivo={conv.get('handoffReason')} aiEnabled={conv.get('aiEnabled')}")
        ev = {k: conv.get(k) for k in ("handoffAt", "handoffReason", "aiEnabled")} | {"despedida": r}
        return bool(r) and bool(conv.get("handoffAt")) and conv.get("aiEnabled") is False, ev

    def e6_candado(self) -> tuple[bool, dict[str, Any]]:
        b = self.clientes["B"]
        pasos = []
        for texto in ("ok", "gracias", "👍"):
            t0 = self.enviar(b, texto)
            pasos.append({"cliente": texto, "nea": self.esperar_respuesta(b, t0, f"6: relleno «{texto}»")})
        despedida = pasos[-1]["nea"]
        cerrada = hasta(lambda: (f := self.ficha(b)) and f.get("cierre_sin_rumbo") and f, 20) or self.ficha(b)
        self.nota(b, f"ficha del CRM: cierre_sin_rumbo={cerrada.get('cierre_sin_rumbo')}")
        t1 = self.enviar(b, "gracias")
        inesperado = self.esperar_silencio(b, t1, 25)
        t2 = self.enviar(b, "¿cuánto cuesta el servicio?")
        reapertura = self.esperar_respuesta(b, t2, "6: pregunta con contenido")
        reabierta = hasta(lambda: "cierre_sin_rumbo" not in self.ficha(b), 20)
        self.nota(b, "ficha del CRM: cierre_sin_rumbo borrado" if reabierta else "ficha del CRM: cierre_sin_rumbo SIGUE")
        ev = {
            "pasos": pasos,
            "ficha_tras_cierre": cerrada,
            "relleno_tras_cierre": inesperado or "silencio",
            "reapertura": reapertura,
            "ficha_tras_reabrir": self.ficha(b),
        }
        ok = (
            len(despedida) == 1
            and bool(cerrada.get("cierre_sin_rumbo"))
            and not inesperado
            and bool(reapertura)
            and bool(reabierta)
        )
        return ok, ev

    def e7_crm_caido(self) -> tuple[bool, dict[str, Any]]:
        """El CRM se apaga ~20 s y el cliente escribe dos veces mientras tanto.

        El relay encola los dos payloads y los entrega al volver; el turno,
        que no alcanzó al CRM, se reintenta con la ráfaga completa. Lo que se
        exige: los dos entrantes en la bandeja y UNA respuesta de Nea que
        contesta los dos (un solo mensaje del lead en su historial, con los
        dos textos), sin una segunda respuesta detrás.
        """
        c = self.clientes["C"]
        assert self.proc_crm is not None
        self.proc_crm.detener()
        if not hasta(lambda: not puerto_ocupado(self.args.crm_port), 30):
            return False, {"error": "el CRM no soltó el puerto al detenerlo"}
        self.nota(c, "CRM detenido")
        texto = "Hola, quisiera informes de sus servicios"
        segundo = "¿Y cuánto cuesta la página web?"
        t0 = self.enviar(c, texto)
        t_segundo: float | None = None
        cola = []
        fin = time.monotonic() + 20  # la caída que se simula: ~20 s
        while time.monotonic() < fin:
            if t_segundo is None and time.time() - t0 >= 8:
                # Pasado el coalesce del primero: abre su propio turno, que
                # tampoco alcanza al CRM y se lleva la ráfaga que esperaba.
                t_segundo = self.enviar(c, segundo)
            try:
                cola.append((self.salud_nea().get("relay") or {}).get("pendientes"))
            except httpx.HTTPError:
                cola.append(None)
            time.sleep(2)
        self.nota(c, "CRM arrancando de nuevo")
        self.arrancar_crm(calentar=False)
        self.nota(c, "CRM contesta /api/health")
        # La entrega la marca la cola de Nea: `pendientes` vuelve a 0 cuando el
        # CRM aceptó los payloads. Se mira antes de compilar nada más.
        vacia = hasta(lambda: (self.salud_nea().get("relay") or {}).get("pendientes") == 0, 180, 0.5)
        entregado_en = time.time() if vacia else None
        if entregado_en:
            self.nota(c, f"relay entregado al CRM ({entregado_en - self.crm_sano_en:.1f} s después de que el CRM volvió)")
        self.calentar()
        llegadas = [self.esperar_entrante(c, x, t0, 120) for x in (texto, segundo)]
        if all(x is not None for x in llegadas):
            self.nota(c, "los dos entrantes están en la bandeja del CRM")
        # Los reintentos de Nea suman como mucho 10 min (TURN_RETRY_DELAYS);
        # esta respuesta no cuenta para la latencia por turno: la mide la caída.
        r = self.esperar_respuesta(c, t0, None, timeout=600)
        respondio_en = instante(r[0]["at"]) if r else None
        otra = self.esperar_silencio(c, time.time(), 30) if r else []
        conv = self.conversacion(c) or {}
        historial = consulta(
            self.nea_db,
            "select m.content from bot_message m join bot_conversation v on v.id = m.conversation_id "
            "where v.wa_identity = $1 and m.role = 'user' order by m.id",
            c.canonico,
        )
        del_lead = [h["content"] for h in historial]
        log = (self.out / "nea.log").read_text(encoding="utf-8", errors="replace").splitlines()
        ev = {
            "pendientes_en_relay_durante_la_caida": cola,
            "crm_de_vuelta_tras_s": round(self.crm_sano_en - t0, 1),
            "relay_entregado_tras_volver_s": round(entregado_en - self.crm_sano_en, 1) if entregado_en else None,
            "segundo_mensaje_tras_s": round(t_segundo - t0, 1) if t_segundo else None,
            "entrantes_en_bandeja": [x is not None for x in llegadas],
            "respuesta_tras_volver_el_crm_s": round(respondio_en - self.crm_sano_en, 1) if respondio_en else None,
            "respuestas": r,
            "segunda_respuesta": otra,
            "mensajes_del_lead_en_nea": del_lead,
            "handoff": {k: conv.get(k) for k in ("handoffAt", "handoffReason", "aiEnabled")},
            "log_nea": [
                linea[-240:]
                for linea in log
                if c.canonico in linea and ("reintento" in linea or "ráfaga" in linea or "CRM no" in linea)
            ][-12:],
        }
        checks = {
            "relay_encolo_durante_la_caida": max((x or 0) for x in cola) >= 1,
            "relay_entrego_al_volver": entregado_en is not None,
            "los_dos_entrantes_en_la_bandeja": all(x is not None for x in llegadas),
            "el_cliente_recibio_respuesta": bool(r),
            "una_sola_respuesta": bool(r) and not otra,
            "un_turno_con_los_dos_mensajes": len(del_lead) == 1 and texto in del_lead[0] and segundo in del_lead[0],
            "sin_handoff": not conv.get("handoffAt"),
        }
        ev["checks"] = checks
        return all(checks.values()), ev

    def e8_override(self) -> tuple[bool, dict[str, Any]]:
        ruta = f"/api/dev/wa-mock/graph/v25.0/{WABA}/subscribed_apps"
        como_meta = {"authorization": f"Bearer {TOKEN_WA}"}
        nea_webhook = f"http://localhost:{self.args.nea_port}/webhook"

        def override() -> str | None:
            apps = self.crm.json("GET", ruta, headers=como_meta)[1].get("data") or []
            return next((x.get("override_callback_uri") for x in apps if x.get("override_callback_uri")), None)

        st_fijar, _ = self.crm.json("POST", ruta, headers=como_meta, cuerpo={"override_callback_uri": nea_webhook, "verify_token": self.sec["verify"]})
        antes = override()
        st_guardar, cuerpo = self.crm.json("PUT", "/api/settings/whatsapp", cuerpo={"wabaId": WABA, "phoneNumberId": PN, "token": TOKEN_WA})
        despues = override()
        ev = {"fijar": st_fijar, "override_antes": antes, "guardar_conexion": st_guardar, "override_despues": despues}
        if st_guardar != 200:
            ev["respuesta"] = cuerpo
        return antes == nea_webhook and st_guardar == 200 and despues == nea_webhook, ev

    def e9_salud(self) -> tuple[bool, dict[str, Any]]:
        h = hasta(lambda: (x := self.salud_nea()) and (x.get("relay") or {}).get("pendientes") == 0 and x, 90) or self.salud_nea()
        ok = (
            h.get("status") == "ok"
            and bool(h.get("version"))
            and h.get("mode") == "estándar"
            and (h.get("relay") or {}).get("pendientes") == 0
        )
        return ok, {"health": h}

    # ── Resultados ──────────────────────────────────────────────────────────

    def escribir(self) -> dict[str, Any]:
        for c in self.clientes.values():
            if not c.eventos:
                continue
            lineas = [f"# Cliente {c.etiqueta} — {c.nombre} ({c.canonico})", ""]
            for e in sorted(c.eventos, key=lambda e: e["t"]):
                hora = datetime.fromtimestamp(e["t"], TZ).strftime("%H:%M:%S")
                if e["quien"] == "cliente":
                    lineas.append(f"**[{hora}] Cliente:** {e['texto']}")
                elif e["quien"] == "nea":
                    lineas.append(f"**[{hora}] Nea** (+{e['latencia_s']} s): {e['texto']}")
                else:
                    lineas.append(f"_[{hora}] {e['texto']}_")
                lineas.append("")
            (self.out / f"transcripcion-{c.etiqueta}.md").write_text("\n".join(lineas), encoding="utf-8")
        segundos = [x["s"] for x in self.latencias]
        llm = self.medidor.resumen() if self.medidor else {}
        try:
            uso_fin = uso_de_la_llave(self.llave)
            if self.uso_llave_inicio is not None and uso_fin is not None:
                llm["delta_uso_de_la_llave_usd"] = round(uso_fin - self.uso_llave_inicio, 6)
        except (RuntimeError, httpx.HTTPError):
            pass
        resultado = {
            "fecha": datetime.now(TZ).isoformat(timespec="seconds"),
            "commits": {
                "crm": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.crm_dir, capture_output=True, text=True).stdout.strip(),
                "nea": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=RAIZ_NEA, capture_output=True, text=True).stdout.strip(),
            },
            "clientes": {k: c.canonico for k, c in self.clientes.items()},
            "escenarios": self.resultados,
            "latencia_por_turno": {
                "incluye": "del webhook a Nea hasta el envío en el wa-mock (coalesce de 4 s incluido)",
                "mediana_s": round(statistics.median(segundos), 2) if segundos else None,
                "p95_s": percentil(segundos, 95),
                "turnos": self.latencias,
            },
            "llm": llm,
        }
        (self.out / "resultado.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if self.medidor:
            (self.out / "medidor-llm.json").write_text(json.dumps(self.medidor.llamadas, indent=2), encoding="utf-8")
        return resultado


def rango(texto: str) -> set[int]:
    elegidos: set[int] = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = parte.split("-", 1)
            elegidos |= set(range(int(a), int(b) + 1))
        elif parte:
            elegidos.add(int(parte))
    return elegidos


def main() -> int:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except AttributeError:
            pass
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--crm-dir", default=os.environ.get("E2E_CRM_DIR"), help="checkout de vocero-crm con `pnpm install` hecho")
    p.add_argument("--env-file", action="append", default=[], help="archivo KEY=VALUE del que se leen LLM_API_KEY, CRM_DATABASE_URL y NEA_DATABASE_URL (repetible)")
    p.add_argument("--out", default=None, help="carpeta para transcripciones, logs y resultado.json")
    p.add_argument("--crm-port", type=int, default=3800)
    p.add_argument("--nea-port", type=int, default=8100)
    p.add_argument("--medidor-port", type=int, default=8190)
    p.add_argument("--modelo", default=MODELO)
    p.add_argument("--presupuesto", type=float, default=0.50, help="tope duro en USD para el modelo")
    p.add_argument("--escenarios", type=rango, default=set(range(1, 10)), help="p. ej. 1-5,9")
    args = p.parse_args()
    if not args.crm_dir:
        p.error("falta --crm-dir (o E2E_CRM_DIR)")
    args.out = args.out or str(Path.cwd() / f"e2e-contra-raiz-{datetime.now():%Y%m%d-%H%M%S}")
    valores = leer_valores(args.env_file)
    faltan = [k for k in CLAVES_DE_ARCHIVO if not valores.get(k)]
    if faltan:
        print(f"faltan {', '.join(faltan)} (entorno o --env-file)", file=sys.stderr)
        return 2

    prueba = Prueba(args, valores)
    print(f"salida: {prueba.out}")
    try:
        try:
            prueba.montar()
        except Exception as exc:
            print(f"no se pudo montar el par: {exc}", file=sys.stderr)
            return 2
        print("par montado: CRM y Nea arriba — corriendo escenarios", flush=True)
        prueba.correr(1, "primer contacto: relay a la bandeja y respuesta sin Markdown", prueba.e1_primer_contacto)
        prueba.correr(2, "estados delivered y read de la respuesta", prueba.e2_estados)
        prueba.correr(3, "agenda: huecos reales, cita, etapa y enlace fijo", prueba.e3_agenda)
        prueba.correr(4, "contexto: Nea sabe a qué hora quedó la cita y no ofrece recordatorios", prueba.e4_contexto_de_la_cita)
        prueba.correr(5, "handoff a una persona con despedida", prueba.e5_handoff)
        prueba.correr(6, "candado de cierre: despedida, silencio y reapertura", prueba.e6_candado)
        prueba.correr(7, "CRM caído: el relay entrega y el cliente recibe UNA respuesta al volver", prueba.e7_crm_caido)
        prueba.correr(8, "guardar la conexión no borra el override de la WABA", prueba.e8_override)
        prueba.correr(9, "/health de Nea: versión, modo estándar y relay en 0", prueba.e9_salud)
    finally:
        prueba.desmontar()
        resultado = prueba.escribir()
    lat = resultado["latencia_por_turno"]
    llm = resultado["llm"]
    print(f"latencia por turno: mediana {lat['mediana_s']} s, p95 {lat['p95_s']} s ({len(lat['turnos'])} turnos)")
    print(f"modelo: {llm.get('llamadas')} llamadas, US${llm.get('gastado_usd')} de US${llm.get('presupuesto_usd')}")
    fallas = [r for r in resultado["escenarios"] if not r["ok"]]
    print(f"{len(resultado['escenarios']) - len(fallas)}/{len(resultado['escenarios'])} escenarios en verde — {prueba.out}")
    return 1 if fallas or not resultado["escenarios"] else 0


if __name__ == "__main__":
    sys.exit(main())
