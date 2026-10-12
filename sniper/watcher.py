"""Detects the moment the shop opens by polling a few URLs for the lock page to disappear."""

from __future__ import annotations

import asyncio
import itertools
import random
import time

import httpx2 as httpx

from .config import Drop

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


async def probe(client: httpx.AsyncClient, drop: Drop, path: str) -> tuple[str, int | None, float]:
    """Returns (state, status, seconds) where state is 'open', 'locked' or 'error'."""
    url = drop.url(path)
    sep = "&" if "?" in url else "?"
    t = time.perf_counter()
    try:
        r = await client.get(f"{url}{sep}_={int(time.time() * 1000)}")
    except Exception:
        return "error", None, time.perf_counter() - t
    dt = time.perf_counter() - t
    if r.status_code != 200:
        return ("error" if r.status_code >= 500 else "locked"), r.status_code, dt
    body = r.text
    if any(m in body for m in drop.locked_markers):
        return "locked", 200, dt
    return "open", 200, dt


async def wait_for_open(drop: Drop, log, on_tick=None) -> str:
    """Poll `watch_urls` round-robin until one serves a real page. Returns that path."""
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=4)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=8.0, limits=limits, follow_redirects=True) as client:
        last_report = 0.0
        last_state = None
        for path in itertools.cycle(drop.watch_urls):
            state, status, dt = await probe(client, drop, path)
            if state == "open":
                return path
            now = time.perf_counter()
            if state != last_state or now - last_report > 30:
                log("info", f"watch: {path} -> {state} (HTTP {status}, {dt * 1000:.0f} ms)")
                last_report, last_state = now, state
            if on_tick:
                await on_tick()
            # Gentle, jittered pacing: about one request per poll_interval in total.
            await asyncio.sleep(max(0.0, drop.poll_interval - dt) * random.uniform(0.85, 1.15))
    raise RuntimeError("unreachable")
