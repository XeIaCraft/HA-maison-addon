"""Interface Ingress de HA-maison Sync (bibliothèque standard uniquement)."""
import json
import os
import pathlib
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sync

HERE = pathlib.Path(__file__).resolve().parent
INGRESS_IP = "172.30.32.2"       # Supervisor : seule adresse autorisée à parler à l'add-on
DEV = os.environ.get("HA_MAISON_DEV") == "1"
LOCK = threading.Lock()
ACTIONS = {
    "/api/export": lambda body: sync.export(force=bool(body.get("force"))),
    "/api/preview": lambda body: sync.preview(),
    "/api/apply": lambda body: sync.apply(),
    "/api/rollback": lambda body: sync.rollback(),
    "/api/wire_preview": lambda body: sync.wire_preview(),
    "/api/wire": lambda body: sync.wire_apply(),
    "/api/restart": lambda body: sync.restart(),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HAmaisonSync/0.1"

    def log_message(self, *a):        # pas de bruit
        pass

    def _allowed(self):
        if DEV or self.client_address[0] == INGRESS_IP:
            return True
        self._json({"ok": False, "error": "Accès refusé (Ingress uniquement)."}, 403)
        return False

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._allowed():
            return
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            raw = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif u.path == "/api/status":
            refresh = parse_qs(u.query).get("refresh", ["0"])[0] == "1"
            if not LOCK.acquire(blocking=False):
                return self._json({"ok": False, "error": "Une opération est en cours…"}, 409)
            try:
                self._json({"ok": True, **sync.status(refresh)})
            finally:
                LOCK.release()
        else:
            self._json({"ok": False, "error": "Introuvable"}, 404)

    def do_POST(self):
        if not self._allowed():
            return
        fn = ACTIONS.get(urlparse(self.path).path)
        if not fn:
            return self._json({"ok": False, "error": "Introuvable"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        if not LOCK.acquire(blocking=False):
            return self._json({"ok": False, "error": "Une opération est déjà en cours."}, 409)
        try:
            self._json({"ok": True, **fn(body)})
        except sync.SyncError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"Erreur inattendue : {type(e).__name__}: {e}"})
        finally:
            LOCK.release()


if __name__ == "__main__":
    host = os.environ.get("HA_MAISON_HOST", "0.0.0.0")
    port = int(os.environ.get("HA_MAISON_PORT", "8099"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
