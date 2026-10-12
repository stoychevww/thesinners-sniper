"""thesinners-sniper — an Opus-driven drop agent.

    python -m sniper check     drops/fyut.toml          API key, fast mode, cache, site status
    python -m sniper browser   drops/fyut.toml          open the bot's Chrome profile (save your address for autofill)
    python -m sniper rehearse  drops/fyut.toml [--fast] [--obscure-names] [--drain] [--soldout hoodie:M]
    python -m sniper run       drops/fyut.toml [--now] [--no-fast]
    python -m sniper recon     drops/fyut.toml          re-scan hidden product ids / new photos (GombaShop)
    python -m sniper ui        drops/fyut.toml          web UI for all of the above at http://127.0.0.1:8800
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from dotenv import load_dotenv

from .config import ROOT, load_drop


def main() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")

    ap = argparse.ArgumentParser(prog="python -m sniper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="the real drop")
    p.add_argument("drop")
    p.add_argument("--now", action="store_true", help="start watching immediately instead of shortly before opens_at")
    p.add_argument("--no-fast", action="store_true", help="standard speed instead of fast mode")

    p = sub.add_parser("rehearse", help="full run against the local mock shop")
    p.add_argument("drop")
    p.add_argument("--fast", action="store_true", help="use fast mode (costs 2x; the real run uses it)")
    p.add_argument("--open-in", type=float, default=25)
    p.add_argument("--obscure-names", action="store_true")
    p.add_argument("--drain", action="store_true")
    p.add_argument("--soldout", action="append", default=[])
    p.add_argument("--surprise", action="store_true", help="tee not on a pre-opened tab; a look-alike is")
    p.add_argument("--chaos", action="store_true", help="overloaded shop: 503s and slow pages for 30 s after opening")
    p.add_argument("--close-after", type=float, default=None, help="close the browser N s after handover (unattended tests)")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("selftest", help="run every rehearsal scenario and grade them (about 3 min, $0.60)")
    p.add_argument("drop")

    p = sub.add_parser("check", help="pre-flight checks")
    p.add_argument("drop")
    p.add_argument("--no-fast", action="store_true")

    p = sub.add_parser("browser", help="open the bot's browser profile")
    p.add_argument("drop")
    p.add_argument("url", nargs="?", default="chrome://settings/addresses")

    p = sub.add_parser("ui", help="local web UI for all of the above")
    p.add_argument("drop")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--no-open", action="store_true", help="don't open a browser tab")

    p = sub.add_parser("recon", help="scan for hidden product ids and newly uploaded photos (GombaShop)")
    p.add_argument("drop")
    p.add_argument("--ids", default="440-520", help="product id range, e.g. 440-520")
    p.add_argument("--images-from", type=int, default=None, help="scan photo ids upward from here (default: after the newest saved one)")

    args = ap.parse_args()
    drop = load_drop(args.drop)

    if args.cmd in ("run", "rehearse", "check", "selftest") and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).")

    try:
        if args.cmd == "run":
            from .runner import run_drop

            asyncio.run(run_drop(drop, now=args.now, fast=drop.agent.fast_mode and not args.no_fast, label="live"))
        elif args.cmd == "rehearse":
            from mock.server import start as start_mock

            from .runner import run_drop

            try:
                srv, _ = start_mock(args.port, args.open_in, args.obscure_names, args.drain, args.soldout,
                                    surprise=args.surprise, chaos=args.chaos, log_path=ROOT / "runs" / "last-mock.log")
            except OSError:
                sys.exit(f"Port {args.port} is busy: a previous rehearsal may still be running. Close it and try again.")
            mock_url = f"http://127.0.0.1:{args.port}"
            print(f"[mock] shop at {mock_url} opens in {args.open_in:.0f}s (obscure names: {args.obscure_names}, "
                  f"drain: {args.drain}, surprise layout: {args.surprise}, overloaded: {args.chaos}, "
                  f"sold out: {args.soldout or 'none'})", flush=True)
            asyncio.run(run_drop(drop.with_base_url(mock_url), now=True, fast=args.fast, label="rehearsal",
                                 close_after=args.close_after))
            srv.shutdown()
        elif args.cmd == "check":
            asyncio.run(check(drop, fast=drop.agent.fast_mode and not args.no_fast))
        elif args.cmd == "selftest":
            from .selftest import selftest

            sys.exit(selftest(drop))
        elif args.cmd == "browser":
            asyncio.run(open_profile(drop, args.url))
        elif args.cmd == "ui":
            from .webui import serve

            serve(args.drop, args.port, open_browser=not args.no_open)
        elif args.cmd == "recon":
            from .recon import recon

            lo, hi = (int(x) for x in args.ids.split("-"))
            asyncio.run(recon(drop, lo, hi, args.images_from))
    except KeyboardInterrupt:
        print("\nbye")


async def check(drop, fast: bool) -> None:
    import httpx2 as httpx

    from .agent import Agent
    from .timeline import Timeline
    from .watcher import UA, probe

    tl = Timeline("check")
    tl.log("info", f"model {drop.agent.model}, effort {drop.agent.effort}, fast {fast}, fallbacks {drop.agent.fallbacks}")
    agent = Agent(drop, browser=None, tl=tl, fast=fast)
    t = time.perf_counter()
    await agent.prewarm()
    t = time.perf_counter()
    await agent.prewarm()  # second call should read the cache
    tl.log("info", f"cached prefix re-read in {time.perf_counter() - t:.1f}s")
    t = time.perf_counter()
    resp = await agent._create([{"role": "user", "content": agent.ref_blocks + [
        {"type": "text", "text": "Pre-flight check. Name the targets you see in the reference images in one short line each. Do not call tools."}]}], 2000)
    tl.add_usage(drop.agent.model, resp.usage, agent.fast)
    text = " ".join(c.text for c in resp.content if c.type == "text").strip()
    tl.log("info", f"sample turn: {time.perf_counter() - t:.1f}s, out {resp.usage.output_tokens} tok, fast={agent.fast}\n{text}")

    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=10, follow_redirects=True) as client:
        for path in drop.watch_urls:
            state, status, dt = await probe(client, drop, path)
            tl.log("info", f"site: {path} -> {state} (HTTP {status}, {dt * 1000:.0f} ms)")
    if drop.opens_at:
        left = drop.opens_at.timestamp() - time.time()
        tl.log("info", f"opens in {left / 3600:.2f} h ({drop.opens_at:%Y-%m-%d %H:%M %z})")
    tl.log("info", f"check cost: ${tl.cost:.3f}")


async def open_profile(drop, url: str) -> None:
    from .browser import Browser
    from .runner import Control

    control = Control()
    b = Browser(drop, print)
    await b.start()
    page = b.ctx.pages[0] if b.ctx.pages else await b.ctx.new_page()
    try:
        await page.goto(url)
    except Exception:
        pass
    print("Bot browser profile is open. Add your address/phone/email under Settings > Autofill, then close the window.", flush=True)
    closed = asyncio.create_task(b.ctx.wait_for_event("close", timeout=0))
    while not closed.done() and not control.quit.is_set():
        await asyncio.sleep(0.2)
    await b.stop()
    print("Browser closed.", flush=True)


if __name__ == "__main__":
    main()
