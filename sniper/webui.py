"""Local web UI.

    python -m sniper ui drops/fyut.toml          -> http://127.0.0.1:8800

Runs the same CLI commands (check / rehearse / run / browser / recon) as a child process and
streams their events to the page. Binds to 127.0.0.1 only; every action needs the per-session
token embedded in the page, so other websites cannot trigger runs.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx2 as httpx

from .config import ROOT, load_drop
from .timeline import EVENT_PREFIX
from .watcher import UA

INDEX = Path(__file__).parent / "webui.html"
REFS = ROOT / "refs"
RUNS = ROOT / "runs"
ENV = ROOT / ".env"
MAX_EVENTS = 5000


class ExclusiveServer(ThreadingHTTPServer):
    """On Windows the stdlib default (SO_REUSEADDR) lets a second server bind the same port silently."""

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Hub:
    """Event log shared by all browser tabs, plus the one child process that may be running."""

    def __init__(self, drop_path: Path):
        self.drop_path = drop_path
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.seq = 0
        self.job: dict | None = None
        self.proc: subprocess.Popen | None = None
        self._site = {"t": 0.0, "data": None}

    # -- events
    def push(self, ev: dict) -> None:
        with self.cond:
            self.seq += 1
            ev["seq"] = self.seq
            if self.job:
                ev.setdefault("job", self.job["id"])
                if ev.get("phase"):
                    self.job["phase"] = ev["phase"]
                if "cost" in ev:
                    self.job["cost"] = ev["cost"]
                if ev.get("result"):
                    self.job["result"] = ev["result"]
            self.events.append(ev)
            del self.events[:-MAX_EVENTS]
            self.cond.notify_all()

    def since(self, seq: int) -> list[dict]:
        return [e for e in self.events if e["seq"] > seq]

    # -- jobs
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd: str, args: list[str]) -> dict:
        if self.running():
            raise RuntimeError("a job is already running")
        env = {**os.environ, "SNIPER_EVENTS": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        argv = [sys.executable, "-u", "-m", "sniper", cmd, str(self.drop_path), *args]
        self.proc = subprocess.Popen(
            argv, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.job = {"id": secrets.token_hex(4), "cmd": cmd, "args": args, "started": time.time(),
                    "phase": "starting", "cost": 0.0, "result": None, "exit": None}
        self.push({"kind": "job", "msg": f"started: {cmd} {' '.join(args)}".strip(), "phase": "starting"})
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return self.job

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith(EVENT_PREFIX):
                try:
                    self.push(json.loads(line[len(EVENT_PREFIX):]))
                    continue
                except json.JSONDecodeError:
                    pass
            self.push({"kind": "mock" if line.startswith("[mock") else "raw", "msg": line})
        code = proc.wait()
        if self.job:
            self.job["exit"] = code
        self.push({"kind": "exit", "msg": f"process exited with code {code}", "phase": "exited", "code": code})

    def send(self, command: str) -> None:
        if self.running():
            try:
                self.proc.stdin.write(command + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    def kill(self) -> None:
        if self.running():
            self.proc.kill()

    # -- shop status (cached; at most one request per 15 s)
    def site_status(self) -> dict:
        if time.time() - self._site["t"] < 15 and self._site["data"]:
            return self._site["data"]
        drop = load_drop(self.drop_path)
        path = drop.watch_urls[0]
        t = time.perf_counter()
        try:
            r = httpx.get(drop.url(path), headers={"User-Agent": UA}, timeout=8, follow_redirects=True)
            if r.status_code != 200:
                state = "error"
            else:
                state = "locked" if any(m in r.text for m in drop.locked_markers) else "open"
            data = {"state": state, "status": r.status_code, "ms": round((time.perf_counter() - t) * 1000), "url": path}
        except Exception as e:
            data = {"state": "error", "status": None, "ms": None, "url": path, "error": type(e).__name__}
        data["checked"] = time.time()
        self._site = {"t": time.time(), "data": data}
        return data


# ---------------------------------------------------------------------------- helpers

def read_key() -> str:
    if not ENV.exists():
        return ""
    m = re.search(r"^ANTHROPIC_API_KEY=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def write_key(key: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [l for l in lines if not l.startswith("ANTHROPIC_API_KEY=")] + [f"ANTHROPIC_API_KEY={key}"]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ref_url(p: Path) -> str | None:
    try:
        return "/img/refs/" + p.resolve().relative_to(REFS.resolve()).as_posix()
    except ValueError:
        return None


def drop_summary(drop_path: Path) -> dict:
    d = load_drop(drop_path)
    return {
        "name": d.name, "base_url": d.base_url, "file": str(drop_path),
        "opens_at": d.opens_at.isoformat() if d.opens_at else None,
        "opens_at_ts": d.opens_at.timestamp() if d.opens_at else None,
        "watch_from": d.watch_from_seconds_before,
        "prefetch": d.prefetch,
        "agent": vars(d.agent),
        "targets": [{"key": t.key, "description": t.description, "sizes": t.sizes, "qty": t.qty,
                     "refs": [u for u in (ref_url(r) for r in t.refs) if u]} for t in d.targets],
    }


def list_runs() -> list[dict]:
    out = []
    if not RUNS.exists():
        return out
    for d in sorted(RUNS.iterdir(), reverse=True)[:60]:
        if not d.is_dir():
            continue
        res = {}
        if (d / "result.json").exists():
            try:
                res = json.loads((d / "result.json").read_text(encoding="utf-8"))
            except Exception:
                pass
        label = d.name.split("-", 2)[-1] if d.name.count("-") >= 2 else d.name
        out.append({"name": d.name, "label": label, "status": res.get("status"), "cost": res.get("cost"),
                    "summary": res.get("summary")})
    return out


def run_args(cmd: str, o: dict) -> list[str]:
    a: list[str] = []
    if cmd == "rehearse":
        if o.get("fast"): a.append("--fast")
        if o.get("obscure"): a.append("--obscure-names")
        if o.get("drain"): a.append("--drain")
        if o.get("surprise"): a.append("--surprise")
        if o.get("chaos"): a.append("--chaos")
        a += ["--open-in", str(int(o.get("open_in") or 25))]
        for s in re.split(r"[,\s]+", o.get("soldout") or ""):
            if re.fullmatch(r"[A-Za-z0-9_]+(:[A-Za-z0-9]+)?", s):
                a += ["--soldout", s]
    elif cmd == "run":
        if o.get("now"): a.append("--now")
        if o.get("no_fast"): a.append("--no-fast")
    elif cmd == "check":
        if o.get("no_fast"): a.append("--no-fast")
    elif cmd == "recon":
        if re.fullmatch(r"\d+-\d+", o.get("ids") or ""): a += ["--ids", o["ids"]]
        if str(o.get("images_from") or "").isdigit(): a += ["--images-from", str(o["images_from"])]
    return a


# ---------------------------------------------------------------------------- HTTP

def make_handler(hub: Hub, token: str, port: int):
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=300")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            origin = self.headers.get("Origin")
            return self.headers.get("X-Token") == token and (origin is None or origin in origins)

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            if path == "/":
                html = INDEX.read_text(encoding="utf-8").replace("__TOKEN__", token)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if path == "/api/state":
                key = read_key() or os.environ.get("ANTHROPIC_API_KEY", "")
                try:
                    drop = drop_summary(hub.drop_path)
                    drop_error = None
                except Exception as e:
                    drop, drop_error = None, f"{type(e).__name__}: {e}"
                return self._json({
                    "drop": drop, "drop_error": drop_error, "job": hub.job, "running": hub.running(),
                    "key": (key[:10] + "…" + key[-4:]) if len(key) > 20 else None, "now": time.time(), "seq": hub.seq,
                })
            if path == "/api/events":
                return self._sse(int(parse_qs(u.query).get("since", ["0"])[0] or 0))
            if path == "/api/site":
                return self._json(hub.site_status())
            if path == "/api/config":
                return self._json({"text": hub.drop_path.read_text(encoding="utf-8"), "file": str(hub.drop_path)})
            if path == "/api/runs":
                return self._json(list_runs())
            m = re.fullmatch(r"/api/runs/([\w.-]+)", path)
            if m:
                d = RUNS / m.group(1)
                if not d.is_dir():
                    return self._json({"error": "not found"}, 404)
                evs = []
                tl = d / "timeline.jsonl"
                if tl.exists():
                    for line in tl.read_text(encoding="utf-8").splitlines():
                        try:
                            evs.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                return self._json({"name": d.name, "events": evs})
            if path == "/api/photos":
                folder = REFS / "site-images"
                ids = sorted((int(p.stem) for p in folder.glob("*.jpg") if p.stem.isdigit()), reverse=True) if folder.exists() else []
                return self._json(ids)
            if path.startswith("/img/refs/"):
                p = (REFS / unquote(path[len("/img/refs/"):])).resolve()
                if REFS.resolve() in p.parents and p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    return self._file(p, "image/png" if p.suffix.lower() == ".png" else "image/jpeg")
            return self._json({"error": "not found"}, 404)

        def _sse(self, since: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            cursor = since
            try:
                while True:
                    with hub.cond:
                        hub.cond.wait_for(lambda: hub.seq > cursor, timeout=15)
                        batch = hub.since(cursor)
                    if batch:
                        for ev in batch:
                            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
                        cursor = batch[-1]["seq"]
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        def do_POST(self):
            if not self._authorized():
                return self._json({"error": "forbidden"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            path = urlparse(self.path).path
            if path == "/api/start":
                cmd = body.get("cmd")
                if cmd not in ("check", "rehearse", "run", "browser", "recon", "selftest"):
                    return self._json({"error": "unknown command"}, 400)
                if cmd in ("check", "rehearse", "run", "selftest") and not (read_key() or os.environ.get("ANTHROPIC_API_KEY")):
                    return self._json({"error": "Add your Anthropic API key first."}, 400)
                try:
                    return self._json(hub.start(cmd, run_args(cmd, body.get("opts") or {})))
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 409)
            if path == "/api/control":
                action = body.get("action")
                if action in ("stop", "quit"):
                    hub.send(action)
                elif action == "kill":
                    hub.kill()
                else:
                    return self._json({"error": "unknown action"}, 400)
                return self._json({"ok": True})
            if path == "/api/config":
                text = body.get("text", "")
                tmp = hub.drop_path.with_suffix(".tmp.toml")
                tmp.write_text(text, encoding="utf-8")
                try:
                    load_drop(tmp)
                except Exception as e:
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
                finally:
                    tmp.unlink(missing_ok=True)
                hub.drop_path.write_text(text, encoding="utf-8")
                return self._json({"ok": True})
            if path == "/api/key":
                key = (body.get("key") or "").strip()
                if not re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]{20,}", key):
                    return self._json({"error": "That doesn't look like an Anthropic API key (sk-ant-…)."}, 400)
                write_key(key)
                os.environ["ANTHROPIC_API_KEY"] = key
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return H


def serve(drop_path: str, port: int = 8800, open_browser: bool = True) -> None:
    hub = Hub(Path(drop_path).resolve())
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}/"
    try:
        srv = ExclusiveServer(("127.0.0.1", port), make_handler(hub, token, port))
    except OSError:
        print(f"Something is already running on port {port}, probably the UI from another window. Opening it: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"thesinners-sniper UI: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if hub.running():
            hub.send("quit")
            time.sleep(3)
            hub.kill()
        srv.server_close()
