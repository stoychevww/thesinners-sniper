"""Drop-day orchestration: wait for the open, pre-open tabs, run the agent, hand the browser to the user."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

from .agent import Agent
from .browser import Browser, short_url
from .config import Drop
from .timeline import Timeline
from .watcher import wait_for_open

PREWARM_EVERY = 240  # seconds; the prompt cache lives 5 minutes
OPEN_LOAD_WAIT = 3.0  # max seconds to wait for the pre-opened tabs to reload at the open
CATALOG_THUMBS = 16  # listing-page thumbnails shown to the agent in its first message


def _beep() -> None:
    if sys.platform == "win32":
        try:
            import winsound

            for f in (880, 1175, 1568):
                winsound.Beep(f, 140)
        except Exception:
            pass
    else:
        print("\a", end="", flush=True)


class Control:
    """Commands on stdin, one per line (typed in the terminal or sent by the web UI):
    Enter or `stop` stops the agent so you can take over (only while the agent is running);
    `quit` ends the run and closes the browser."""

    def __init__(self):
        self.stop = threading.Event()
        self.quit = threading.Event()
        self.armed = False
        self.auto_quit_after: float | None = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == "quit":
                    self.quit.set()
                    self.stop.set()
                elif cmd in ("", "stop") and self.armed:
                    self.stop.set()
        except Exception:
            return


class Quit(Exception):
    pass


async def run_drop(drop: Drop, *, now: bool, fast: bool, label: str, close_after: float | None = None) -> None:
    tl = Timeline(label)
    control = Control()
    tl.log("info", f"{drop.name} — {drop.base_url} — run log: {tl.dir}", phase="starting")
    browser = Browser(drop, tl.log)
    await browser.start()
    if close_after is not None:  # unattended tests: end the run a few seconds after handover
        control.auto_quit_after = close_after
    try:
        await _run(drop, tl, control, browser, now=now, fast=fast)
    except Quit:
        tl.log("warn", "run ended by user", phase="ended")
        await browser.stop()


async def _run(drop: Drop, tl: Timeline, control: Control, browser: Browser, *, now: bool, fast: bool) -> None:
    agent = Agent(drop, browser, tl, fast=fast)

    # One tab per page the agent should see the instant the shop opens.
    urls = [drop.start_url] + [u for u in drop.prefetch if u != drop.start_url]
    tabs = list(browser.tabs)[:1]
    while len(tabs) < len(urls):
        tabs.append(await browser.new_tab())
    await asyncio.gather(*(browser.goto(t, u) for t, u in zip(tabs, urls)))  # also warms connections
    tl.log("info", f"browser ready with {len(tabs)} tab(s)", phase="browser")

    if not now and drop.opens_at:
        start_at = drop.opens_at.timestamp() - drop.watch_from_seconds_before
        if start_at > time.time():
            tl.log("info", f"opens at {drop.opens_at:%Y-%m-%d %H:%M:%S %z}; watching from "
                           f"{datetime.fromtimestamp(start_at):%H:%M:%S} "
                           f"({(start_at - time.time()) / 60:.1f} min from now)", phase="waiting")
            while time.time() < start_at:
                if control.quit.is_set():
                    raise Quit
                await asyncio.sleep(0.5)

    try:
        await agent.prewarm()
    except Exception as e:
        tl.log("warn", f"pre-warm failed ({type(e).__name__}: {e}); continuing")
    last_warm = time.perf_counter()

    async def tick():
        nonlocal last_warm
        if control.quit.is_set():
            raise Quit
        if time.perf_counter() - last_warm > PREWARM_EVERY:
            last_warm = time.perf_counter()
            asyncio.create_task(agent.prewarm())

    tl.log("info", "watching for the shop to open…", phase="watching")
    opened_on = await wait_for_open(drop, tl.log, on_tick=tick)
    tl.mark_open()
    tl.log("open", f"SHOP IS OPEN (seen on {opened_on}) at {datetime.now():%H:%M:%S.%f}"[:-3], phase="open")

    result = {"status": "error", "summary": "", "cart_tab": None}
    try:
        # Reload every tab, but don't let one slow page (overloaded shop) hold the agent back.
        loads = {t: asyncio.create_task(browser.goto(t, u)) for t, u in zip(tabs, urls)}
        await asyncio.wait(loads.values(), timeout=OPEN_LOAD_WAIT)
        notes = {t: ("still loading (the shop is slow): snapshot it again shortly, or reload it" if not task.done()
                     else task.result()) for t, task in loads.items()}
        notes = {t: n for t, n in notes.items() if n}
        tl.log("info", f"{len(tabs) - len(notes)}/{len(tabs)} tab(s) loaded cleanly"
                       + (f"; {', '.join(f'{t}: {n.split(':')[0]}' for t, n in notes.items())}" if notes else ""))
        opening = await build_opening(browser, tabs, urls, drop, notes)
        control.armed = True
        tl.log("info", "agent starting (press Enter or the Stop button to take over)", phase="agent")
        agent_task = asyncio.create_task(agent.run(opening))
        while not agent_task.done():
            if control.stop.is_set():
                agent_task.cancel()
                tl.log("warn", "agent stopped by user")
                result = {"status": "stopped", "summary": "stopped by user", "cart_tab": None}
                break
            await asyncio.sleep(0.1)
        if agent_task.done() and not agent_task.cancelled():
            result = agent_task.result()
    except Exception as e:
        tl.log("warn", f"agent crashed: {type(e).__name__}: {e}. The browser is yours.")
        result = {"status": "error", "summary": str(e), "cart_tab": None}

    await hand_over(browser, tl, control, result, drop)


async def build_opening(browser: Browser, tabs: list[str], urls: list[str], drop: Drop,
                        notes: dict[str, str]) -> list[dict]:
    ready = [t for t in tabs if not (notes.get(t) or "").startswith("still loading")]
    snaps = dict(zip(ready, await asyncio.gather(*(browser.snapshot(t) for t in ready), return_exceptions=True)))
    blocks: list[dict] = [{
        "type": "text",
        "text": f"THE SHOP JUST OPENED ({datetime.now():%H:%M:%S}). I already opened these tabs for you "
                f"(the first is the shop's listing page, the others are product pages found during recon "
                f"and may or may not be targets). Tabs marked NOTE failed or are still loading: reload them. Go.",
    }]
    for tab, url in zip(tabs, urls):
        note = f"NOTE: {notes[tab]}\n" if tab in notes else ""
        snap = snaps.get(tab)
        if snap is None or isinstance(snap, Exception):
            blocks.append({"type": "text", "text": f"[{tab}] {url}\n{note}" + (f"could not read page ({snap})" if snap else "")})
            continue
        blocks.append({"type": "text", "text": note + snap.render()})
        if url == drop.start_url:
            # Listing page: product thumbnails, each labelled with the page it links to, so targets that
            # weren't pre-opened can be spotted in the first turn.
            linked = [i for i in snap.data.get("imgs", []) if i.get("href")][:CATALOG_THUMBS]
            if linked:
                blocks.append({"type": "text", "text": f"Product thumbnails on the listing page [{tab}]:"})
                blocks += await browser.image_blocks(tab, [i["id"] for i in linked], max_side=200, limit=CATALOG_THUMBS,
                                                     labels={i["id"]: f"{i['id']} → {short_url(i['href'])}" for i in linked})
        elif snap.data.get("imgs"):
            blocks += await browser.image_blocks(tab, ["i1"], max_side=384, labels={"i1": "i1 main product photo"})
    return blocks


async def hand_over(browser: Browser, tl: Timeline, control: Control, result: dict, drop: Drop) -> None:
    tab = result.get("cart_tab")
    cart_url = result.get("cart_url") or ""
    if not cart_url or urlparse(drop.url(cart_url)).hostname != urlparse(drop.base_url).hostname:
        cart_url = drop.cart_url  # only follow links on the shop's own host
    if cart_url and not (tab and tab in browser.tabs):
        tab = next(iter(browser.tabs), None) or await browser.new_tab()
        problem = await browser.goto(tab, cart_url)
        tl.log("info", f"opened the cart page {cart_url} in {tab}" + (f" ({problem})" if problem else ""))
    if tab and tab in browser.tabs:
        await browser.tabs[tab].bring_to_front()
    tl.log("done", f"HANDOVER — status: {result.get('status')}", phase="handover", result=result)
    for line in (result.get("summary") or "").splitlines():
        tl.log("done", "  " + line)
    tl.log("done", f"API cost this run: ${tl.cost:.2f}. The browser is yours: check out now. "
                   f"Close the browser window (or type quit) when you're done.")
    _beep()
    tl.save("result.json", {**result, "cost": round(tl.cost, 4)})
    closed = asyncio.create_task(browser.ctx.wait_for_event("close", timeout=0))
    deadline = time.time() + control.auto_quit_after if control.auto_quit_after is not None else None
    while not closed.done() and not control.quit.is_set():
        if deadline and time.time() > deadline:
            break
        await asyncio.sleep(0.2)
    tl.log("info", "run finished", phase="ended")
    await browser.stop()
