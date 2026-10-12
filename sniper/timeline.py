"""Console + JSONL timeline. Times are shown relative to the moment the shop opened."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT

# Set by the web UI: emit one machine-readable line per event instead of coloured text.
EVENTS_MODE = os.environ.get("SNIPER_EVENTS") == "1"
EVENT_PREFIX = "@@EVT "

# $ per million tokens: (input, output). Fast mode on Opus 5.5 is 2x.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.05

COLORS = {"info": "", "open": "\033[1;32m", "agent": "\033[36m", "tool": "\033[90m", "warn": "\033[33m", "done": "\033[1;35m"}
RESET = "\033[0m"


class Timeline:
    def __init__(self, label: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = ROOT / "runs" / f"{stamp}-{label}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.dir / "timeline.jsonl").open("a", encoding="utf-8")
        self.t0: float | None = None  # perf_counter at shop open
        self.cost = 0.0

    def mark_open(self) -> None:
        self.t0 = time.perf_counter()

    def rel(self) -> str:
        if self.t0 is None:
            return datetime.now().strftime("%H:%M:%S.%f")[:-3]
        return f"+{time.perf_counter() - self.t0:6.2f}s"

    def log(self, kind: str, msg: str, **data) -> None:
        rec = {"ts": time.time(), "rel": self.rel(), "kind": kind, "msg": msg, "cost": round(self.cost, 4),
               "run": self.dir.name, **data}
        rec_json = json.dumps(rec, ensure_ascii=False, default=str)
        if EVENTS_MODE:
            print(EVENT_PREFIX + rec_json, flush=True)
        else:
            line = f"[{self.rel()}] {msg}"
            print(f"{COLORS.get(kind, '')}{line}{RESET if COLORS.get(kind) else ''}", flush=True)
        self._log.write(rec_json + "\n")
        self._log.flush()

    def add_usage(self, model: str, usage, fast: bool) -> float:
        inp, out = PRICES.get(model, PRICES["claude-opus-5-5"])
        mult = 2.0 if fast else 1.0
        cost = mult * (
            (usage.input_tokens or 0) * inp
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * inp * CACHE_WRITE_MULT
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * inp * CACHE_READ_MULT
            + (usage.output_tokens or 0) * out
        ) / 1_000_000
        self.cost += cost
        return cost

    def save(self, name: str, obj) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return p
