"""End-to-end self-test: real agent, real browser, against the local mock shop, graded automatically.

    python -m sniper selftest drops/fyut.toml          (about 3 minutes, about $0.60)

Each scenario is a full rehearsal in its own process. The grade comes from the mock shop's own
record of what was added to the cart, not from what the agent says it did.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import ROOT, Drop

RUNS = ROOT / "runs"
MOCK_LOG = RUNS / "last-mock.log"


def scenarios(drop: Drop) -> list[dict]:
    every = {(t.key, t.sizes[0] if t.sizes else None) for t in drop.targets}
    first = drop.targets[0].key if drop.targets else None
    hoodie = next((t.key for t in drop.targets if t.key == "hoodie"), first)
    return [
        {"name": "basic", "flags": [], "expect": every,
         "why": "pre-opened tabs are the targets"},
        {"name": "look-alikes + surprise layout", "flags": ["--obscure-names", "--surprise"], "expect": every,
         "why": "same names for look-alikes; tee only in the catalog; grey hoodie on a pre-opened tab"},
        {"name": f"{hoodie} sold out in M", "flags": ["--obscure-names", "--soldout", f"{hoodie}:M"],
         "expect": {e for e in every if e[0] != hoodie}, "why": "must skip it, never take another size"},
        {"name": "overloaded shop", "flags": ["--chaos"], "expect": every,
         "why": "30% of requests answer 503 and the rest are slow for 30 s after opening"},
    ]


def parse_mock_log() -> dict:
    out = {"adds": [], "unlocked": None, "rejected": [], "errors503": 0}
    if not MOCK_LOG.exists():
        return out
    for line in MOCK_LOG.read_text(encoding="utf-8").splitlines():
        m = re.search(r"\[mock\s+([+-][\d.]+)s\] ADDED TO CART: .*\((\w+), id (\d+)\) size (\w+) x(\d+)", line)
        if m:
            out["adds"].append({"t": float(m.group(1)), "key": m.group(2), "id": int(m.group(3)), "size": m.group(4)})
        elif "UNLOCKED at" in line:
            out["unlocked"] = line.split("UNLOCKED at ")[1].strip()
        elif "REJECTED" in line:
            out["rejected"].append(line)
        elif "overloaded: 503" in line:
            out["errors503"] += 1
    return out


def newest_run(after: float) -> Path | None:
    dirs = [d for d in RUNS.glob("*-rehearsal") if d.is_dir() and d.stat().st_mtime >= after]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def run_scenario(drop: Drop, sc: dict, out_dir: Path) -> dict:
    t0 = time.time()
    argv = [sys.executable, "-u", "-m", "sniper", "rehearse", str(drop.source), "--open-in", "10", "--close-after", "2",
            *sc["flags"]]
    env = {k: v for k, v in os.environ.items() if k != "SNIPER_EVENTS"}
    log_file = out_dir / (re.sub(r"\W+", "-", sc["name"]).strip("-") + ".log")
    with log_file.open("w", encoding="utf-8") as f:
        try:
            code = subprocess.run(argv, cwd=ROOT, env={**env, "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL,
                                  stdout=f, stderr=subprocess.STDOUT, timeout=300).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"

    mock = parse_mock_log()
    run_dir = newest_run(t0)
    result, opened_at, handover, turns = {}, None, None, 0
    if run_dir:
        if (run_dir / "result.json").exists():
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        for line in (run_dir / "timeline.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("phase") == "open":
                opened_at = e["msg"].rsplit(" at ", 1)[-1]
            if e.get("phase") == "handover":
                handover = e["rel"].strip()
            if e["kind"] == "agent" and e["msg"].startswith("turn "):
                turns += 1

    got = Counter((a["key"], a["size"]) for a in mock["adds"])
    problems = []
    for item in sc["expect"]:
        if got[item] == 0:
            problems.append(f"missing {item[0]} {item[1]}")
    for item, n in got.items():
        if item not in sc["expect"]:
            problems.append(f"WRONG item {item[0]} {item[1]}")
        elif n > 1:
            problems.append(f"duplicate {item[0]} {item[1]} x{n}")
    if code not in (0,):
        problems.append(f"process exit {code}")

    lag = None
    if opened_at and mock["unlocked"]:
        fmt = "%H:%M:%S.%f"
        lag = (datetime.strptime(opened_at, fmt) - datetime.strptime(mock["unlocked"], fmt)).total_seconds()
    adds_t = [a["t"] for a in mock["adds"]]
    return {
        "name": sc["name"], "why": sc["why"], "pass": not problems, "problems": problems,
        "carted": sorted(f"{k} {s}" for (k, s) in got),
        "first_add": min(adds_t) if adds_t else None, "last_add": max(adds_t) if adds_t else None,
        "detect_lag": lag, "handover": handover, "turns": turns, "cost": result.get("cost"),
        "status": result.get("status"), "rejected": mock["rejected"], "http_503s": mock["errors503"],
        "run": run_dir.name if run_dir else None, "log": str(log_file),
    }


def selftest(drop: Drop) -> int:
    out_dir = RUNS / f"{datetime.now():%Y%m%d-%H%M%S}-selftest"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    scs = scenarios(drop)
    print(f"Self-test: {len(scs)} scenarios against the mock shop, each a full agent run. Logs: {out_dir}", flush=True)
    for i, sc in enumerate(scs, 1):
        print(f"\n[{i}/{len(scs)}] {sc['name']}: {sc['why']} …", flush=True)
        r = run_scenario(drop, sc, out_dir)
        rows.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        timing = (f"detected {r['detect_lag']:.2f}s after unlock; carted +{r['first_add']:.1f}s…+{r['last_add']:.1f}s; "
                  if r["last_add"] is not None and r["detect_lag"] is not None else "")
        print(f"  {mark}  {', '.join(r['carted']) or 'nothing carted'}", flush=True)
        print(f"        {timing}handover {r['handover']}, {r['turns']} turns, ${r['cost'] or 0:.2f}"
              + (f", {r['http_503s']} injected 503s" if r["http_503s"] else ""), flush=True)
        for p in r["problems"]:
            print(f"        ! {p}", flush=True)
    (out_dir / "report.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    passed = sum(r["pass"] for r in rows)
    total_cost = sum(r["cost"] or 0 for r in rows)
    print(f"\n{'ALL PASSED' if passed == len(rows) else 'SOME FAILED'}: {passed}/{len(rows)} scenarios, "
          f"total cost ${total_cost:.2f}. Report: {out_dir / 'report.json'}", flush=True)
    return 0 if passed == len(rows) else 1
