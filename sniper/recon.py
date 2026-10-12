"""Pre-drop recon for GombaShop stores, which keep working while the shop is locked:

- product pages of existing (hidden) products return 200 with the lock page; unknown ids return 404
- product photos under /media/<size>/<id>.jpg are publicly served (/media/7/ is the 1600 px original)

New photos are saved to refs/site-images/. Requests are sequential and paced.
"""

from __future__ import annotations

import asyncio

import httpx2 as httpx
from PIL import Image, ImageDraw

from .config import ROOT, Drop
from .timeline import Timeline
from .watcher import UA

PACE = 0.12
PHOTOS = ROOT / "refs" / "site-images"


async def _get(c: httpx.AsyncClient, url: str):
    for attempt in range(4):
        try:
            return await c.get(url)
        except httpx.TransportError:  # Apache drops idle keep-alive connections
            await asyncio.sleep(1 + attempt)
    return None


async def recon(drop: Drop, id_lo: int, id_hi: int, images_from: int | None) -> None:
    tl = Timeline("recon")
    PHOTOS.mkdir(parents=True, exist_ok=True)
    have = sorted(int(p.stem) for p in PHOTOS.glob("*.jpg") if p.stem.isdigit())
    start = images_from if images_from else (have[-1] + 1 if have else 1876)

    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as c:
        tl.log("info", f"scanning product ids {id_lo}-{id_hi}…")
        found = []
        for pid in range(id_lo, id_hi + 1):
            r = await _get(c, drop.url(f"/en-product-details-{pid}.html"))
            if r is not None and r.status_code == 200:
                found.append(pid)
            await asyncio.sleep(PACE)
        tl.log("info", f"existing product ids: {found}")

        tl.log("info", f"scanning photos from id {start}…")
        new, misses, i = [], 0, start
        while misses < 60:
            if (PHOTOS / f"{i}.jpg").exists():
                misses, i = 0, i + 1
                continue
            r = await _get(c, drop.url(f"/media/7/{i}.jpg"))
            if r is not None and r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                (PHOTOS / f"{i}.jpg").write_bytes(r.content)
                new.append(i)
                misses = 0
            else:
                misses += 1
            i += 1
            await asyncio.sleep(PACE)
        tl.log("open" if new else "info", f"new photos: {new or 'none'} (saved to {PHOTOS})")

    if new:
        sheet = contact_sheet([PHOTOS / f"{i}.jpg" for i in new])
        sheet.save(tl.dir / "new-photos.png")
        tl.log("info", f"contact sheet of the new photos: {tl.dir / 'new-photos.png'}")


def contact_sheet(paths, w=220, h=280, cols=8) -> Image.Image:
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * (h + 18)), "white")
    draw = ImageDraw.Draw(sheet)
    for k, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((w, h))
        x, y = (k % cols) * w, (k // cols) * (h + 18)
        sheet.paste(im, (x, y + 18))
        draw.text((x + 4, y + 3), p.stem, fill="red")
    return sheet
