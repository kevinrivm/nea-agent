"""Real uvicorn + PostgreSQL, deterministic CRM/LLM over HTTP. No real messages.
Run with TEST_DATABASE_URL pointing at a disposable localhost PostgreSQL server.
Exercises multi-turn, tenant separation, persisted acceptance and crash recovery.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import PgStore
from app.multiorg import credencial_derivada

secret = secrets.token_hex(32)
received = []
handoffs = []
prompts = []
crm_port = 3247
nea_port = 3248


class Crm(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/brains/context"):
            conv = parse_qs(urlsplit(self.path).query).get("conversationId", ["cv_a"])[0]
            self.respond(200, {"contact": {"id": "ct_"+conv, "name": "Cliente", "waIdentity": "525555555555", "ficha": {}},
                "conversation": {"id": conv, "aiEnabled": True, "windowOpen": True, "handoffAt": None},
                "lead": {"id": "ld_"+conv, "stageName": "Nuevo"}, "booking": {"next": None},
                "llm": {"path": "/api/brains/llm", "model": "security-fixture"},
                "agent": {"name": "Nea", "businessName": "Prueba"}})
        else:
            self.respond(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        org = self.headers.get("X-Vocero-Organization")
        if org and self.headers.get("Authorization") != "Bearer " + credencial_derivada(secret, "org_"+org):
            self.respond(401, {})
            return
        if self.path == "/api/brains/llm/chat/completions":
            prompts.append((org, body))
            text = f"Gracias por contarme. ¿Qué necesitas resolver ahora? ({len(prompts)})"
            self.respond(200, {"id": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        elif self.path == "/api/brains/messages":
            received.append((org, body))
            self.respond(201, {"messageId": "msg_out"})
        elif self.path == "/api/brains/handoff":
            handoffs.append((org, body))
            self.respond(200, {})
        else:
            self.respond(200, {})

    def do_PUT(self):
        self.do_POST()


def event(org, identity, marker):
    return {"dispatchId": "dsp_"+marker, "organization": {"id": "org_"+org, "slug": org},
            "conversation": {"id": "cv_"+org}, "contact": {"identity": identity},
            "messages": [{"id": "msg_"+marker, "type": "text", "text": marker}]}


def wait_for(predicate, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError("observable result not reached before timeout")


async def main():
    dev_runtime = "--dev-runtime" in sys.argv and urlsplit(os.environ.get("CRM_BASE_URL", "")).hostname in ("vocero-cloud-dev", "dev.vocerocrm.com")
    source = os.environ["DATABASE_URL"] if dev_runtime else os.environ["TEST_DATABASE_URL"]
    assert dev_runtime or urlsplit(source).hostname in ("localhost", "127.0.0.1")
    name = "nea_live_" + uuid.uuid4().hex
    admin = await asyncpg.connect(source)
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = urlunsplit(urlsplit(source)._replace(path="/"+name))
    store = PgStore(dsn)
    await store.connect()
    await store.migrate(Path("migrations"))
    crm = ThreadingHTTPServer(("127.0.0.1", crm_port), Crm)
    threading.Thread(target=crm.serve_forever, daemon=True).start()
    env = {**os.environ, "DATABASE_URL": dsn, "VOCERO_MODE": "cloud", "CRM_ORGANIZATION": "",
           "CRM_BRAIN_SECRET": secret, "CRM_BASE_URL": f"http://127.0.0.1:{crm_port}",
           "TYPING_DELAY_SECONDS": "0", "ALLOWED_WA_IDS": "525555555555"}
    process = None
    log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")

    def start():
        child = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(nea_port)], env=env, stdout=log, stderr=log)
        def ready():
            try:
                return httpx.get(f"http://127.0.0.1:{nea_port}/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(ready)
        return child

    try:
        # Accepted work from a prior process starts after boot, without redelivery.
        await store.enqueue_dispatch("org_a", event("a", "525555555555", "marca_privada_A"))
        process = start()
        await asyncio.to_thread(wait_for, lambda: len(received) == 1)
        with httpx.Client(base_url=f"http://127.0.0.1:{nea_port}") as client:
            # 031: el webhook de Meta ya no entra; 503 para que Meta reintente.
            assert client.post("/webhook", content=b"{}").status_code == 503
            assert client.post("/vocero/dispatch", content=b"{}").status_code == 401
            for org, marker in [("a", "segundo_turno_A"), ("b", "marca_privada_B")]:
                body = json.dumps(event(org, "525555555555", marker)).encode()
                signature = "sha256="+hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                assert client.post("/vocero/dispatch", content=body, headers={"X-Vocero-Signature": signature}).status_code == 200
                await asyncio.to_thread(wait_for, lambda: any(row[1]["dispatchId"] == "dsp_"+marker for row in received))
                assert client.post("/vocero/dispatch", content=body, headers={"X-Vocero-Signature": signature}).status_code == 200
            await asyncio.sleep(1)
            assert len(received) == 3
        assert any("marca_privada_A" in json.dumps(p) and "segundo_turno_A" in json.dumps(p) for org,p in prompts if org=="a")
        assert all("marca_privada_A" not in json.dumps(p) for org,p in prompts if org=="b")
        print("PASS live multi-turn, HMAC, tenant isolation and duplicate ACK")
        process.kill()
        process.wait()
        pending = event("a", "525555555555", "turno_interrumpido")
        await store.enqueue_dispatch("org_a", pending)
        await store.pool.execute("UPDATE dispatch_inbox SET state='started',available_at=now()-interval '1 second' WHERE dispatch_id=$1", pending["dispatchId"])
        before = len(prompts)
        process = start()
        await asyncio.to_thread(wait_for, lambda: bool(handoffs))
        assert len(prompts) == before and len(received) == 3
        print("PASS restart recovers interrupted effects by handoff without another LLM/send")
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        log.close()
        crm.shutdown()
        await store.aclose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
