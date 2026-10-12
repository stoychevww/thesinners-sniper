"""A visible Chrome window the agent drives through compact text snapshots.

Nothing here knows about any particular shop. Pages are described to the model as
text (title, main text, interactive elements with refs, images, alerts, cart
widgets, schema.org product data) and the model acts on refs.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Drop

CLICK_RETRIES = 2  # extra clicks when the click's POST failed with 5xx / network error

DEFAULT_BLOCK_HOSTS = [
    "facebook.net", "facebook.com", "google-analytics.com", "googletagmanager.com",
    "doubleclick.net", "pinterest.com", "hotjar.com", "tiktok.com", "clarity.ms",
]

SNAPSHOT_JS = r"""
(opts) => {
  const maxEls = opts.maxEls, maxText = opts.maxText;
  window.__aiRefCounter = window.__aiRefCounter || 0;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, n) => s.length > n ? s.slice(0, n) + '…' : s;
  const shown = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  // Text a person can actually see: skips text hidden by opacity/visibility on any ancestor
  // (themes keep "added to cart" messages in the DOM at opacity 0).
  const clipped = el => {   // pushed outside an overflow-hidden ancestor (how themes hide slide-in messages)
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return true;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      const ar = a.getBoundingClientRect();
      if (r.right <= ar.left || r.left >= ar.right || r.bottom <= ar.top || r.top >= ar.bottom) return true;
    }
    return false;
  };
  const seen = el => (el.checkVisibility ? el.checkVisibility({opacityProperty: true, visibilityProperty: true,
                                                                checkOpacity: true, checkVisibilityCSS: true}) : shown(el))
                     && !clipped(el);
  const visibleText = root => {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const t = n.nodeValue.trim();
      if (t && n.parentElement && seen(n.parentElement)) parts.push(t);
    }
    return clean(parts.join(' '));
  };
  const ref = el => { if (!el.dataset.aiRef) el.dataset.aiRef = 'e' + (++window.__aiRefCounter); return el.dataset.aiRef; };
  const labelOf = el => {
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
    return clean(t);
  };
  const main = document.querySelector('main, [role=main], #main, .main') || document.body;

  // ---- interactive elements
  const nodes = Array.from(document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [onclick]'));
  const seenHref = new Map();
  const items = [];
  nodes.forEach((el, order) => {
    const tag = el.tagName.toLowerCase();
    const isSelect = tag === 'select';
    const visible = shown(el);
    if (!visible && !isSelect) return;               // custom-styled selects are often hidden
    if (el.closest('[data-ai-skip]')) return;
    let kind = tag, text = '', extra = '';
    if (isSelect) {
      const opts = Array.from(el.options).map(o => {
        const label = clean(o.textContent) || '(empty)';
        return `${label}${o.value ? '=' + o.value : ''}${o.disabled ? ' [disabled]' : ''}`;
      });
      const cur = el.options[el.selectedIndex];
      text = labelOf(el);
      extra = `options: ${opts.join(' | ')}; selected: ${cur ? (clean(cur.textContent) || '(empty)') : '-'}${visible ? '' : '; (hidden select)'}`;
    } else if (tag === 'input' || tag === 'textarea') {
      const type = el.getAttribute('type') || 'text';
      kind = `input[${type}]`;
      text = labelOf(el);
      if (type === 'checkbox' || type === 'radio') extra = el.checked ? 'checked' : 'unchecked';
      else extra = `value="${cut(el.value || '', 40)}"`;
    } else {
      text = clean(el.innerText) || clean(el.getAttribute('aria-label')) || clean(el.getAttribute('title'));
      const img = el.querySelector('img[alt]');
      if (!text && img) text = 'image: ' + clean(img.alt);
      if (tag === 'a') {
        const href = el.getAttribute('href') || '';
        if (href.startsWith('javascript') || href === '#' || href === '') { kind = 'button-link'; }
        else {
          const abs = el.href;
          extra = abs;
          if (seenHref.has(abs)) {                     // collapse image link + title link + "details"
            const prev = seenHref.get(abs);
            if (text && (!prev.text || (prev.text.startsWith('image: ') && !text.startsWith('image: ')))) prev.text = text;
            return;
          }
        }
      }
    }
    const inForm = !!el.closest('form');
    const inMain = main.contains(el);
    const prio = (isSelect || kind.startsWith('input') || tag === 'button' || kind === 'button-link' || el.hasAttribute('onclick') || inForm) ? 0 : (inMain ? 1 : 2);
    const it = { ref: ref(el), kind, text: cut(text, 90), extra: cut(extra, 300), order, prio };
    if (tag === 'a' && kind === 'a') seenHref.set(el.href, it);
    items.push(it);
  });
  items.sort((a, b) => a.prio - b.prio || a.order - b.order);
  const total = items.length;
  const picked = items.slice(0, maxEls).sort((a, b) => a.order - b.order);

  // ---- images (for vision). Product images inside links say which page they lead to.
  const imgs = [];
  window.__aiImgs = {};
  const seenSrc = new Set();
  for (const im of document.querySelectorAll('img')) {
    if (imgs.length >= opts.maxImgs) break;
    if (im.closest('header, nav, footer, [class*=logo i]')) continue;
    const src = im.currentSrc || im.src;
    if (!src || seenSrc.has(src)) continue;
    const r = im.getBoundingClientRect();
    const link = im.closest('a[href]');
    const linksToPage = link && !/^(javascript|#|$)/.test(link.getAttribute('href') || '');
    const big = im.naturalWidth >= 120 || r.width >= 100 || (linksToPage && r.width >= 40);
    if (!big) continue;
    if (r.width < 1 && !main.contains(im)) continue;
    seenSrc.add(src);
    const id = 'i' + (imgs.length + 1);
    window.__aiImgs[id] = src;
    imgs.push({ id, src, alt: cut(clean(im.alt), 60), href: linksToPage ? link.href : '' });
  }

  // ---- alerts / status messages and cart widgets
  const pick = (sel, maxLen, limit) => {
    const out = [];
    for (const el of document.querySelectorAll(sel)) {
      if (out.length >= limit) break;
      if (!shown(el) || !seen(el)) continue;
      const t = visibleText(el);
      if (!t || t.length > maxLen) continue;
      if (out.some(o => o.el.contains(el) || el.contains(o.el))) continue;
      out.push({ el, t });
    }
    return out.map(o => o.t);
  };
  const alerts = pick('[class*=error i], [class*=alert i], [class*=notice i], [class*=msg i], [class*=message i], [class*=success i], [class*=stock i], [role=alert], [id*=msg i]', 200, 8);
  const cart = pick('[id*=cart i], [class*=cart i], [id*=basket i], [class*=basket i]', 160, 4);

  // ---- schema.org Product data (stock per variant, when the shop publishes it)
  const products = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      let d = JSON.parse(s.textContent); d = Array.isArray(d) ? d : [d];
      for (const p of d) {
        if (!p || p['@type'] !== 'Product') continue;
        const offers = [].concat(p.offers || []).map(o => ({
          avail: String(o.availability || '').split('/').pop(), price: o.price, url: o.url ? o.url.split('#').pop() : '', name: o.name || o.sku || ''
        }));
        products.push({ name: p.name, offers });
      }
    } catch (e) {}
  }

  const text = cut(clean(main.innerText), maxText);
  return { title: document.title, url: location.href, text, items: picked, total, imgs, alerts, cart, products };
}
"""

IMAGE_LOOKUP_JS = "(id) => (window.__aiImgs || {})[id] || null"

FIND_OPTION_JS = r"""
([ref, wanted]) => {
  const el = document.querySelector(`[data-ai-ref="${ref}"]`);
  if (!el || el.tagName.toLowerCase() !== 'select') return {error: 'not a select'};
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const w = clean(wanted);
  const opts = Array.from(el.options);
  const hit = opts.find(o => clean(o.textContent) === w) || opts.find(o => o.value === wanted)
           || opts.find(o => clean(o.textContent).startsWith(w + ' ') || clean(o.textContent).startsWith(w + '('));
  if (!hit) return {error: 'no option matching ' + JSON.stringify(wanted)};
  return {value: hit.value, disabled: hit.disabled};
}
"""

FORCE_SELECT_JS = r"""
([ref, value]) => {
  const el = document.querySelector(`[data-ai-ref="${ref}"]`);
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  if (window.jQuery) window.jQuery(el).trigger('change');
}
"""


@dataclass
class Snapshot:
    tab: str
    data: dict

    @property
    def looks_like_product(self) -> bool:
        return bool(self.data.get("products")) or any(i["kind"] == "select" for i in self.data["items"])

    def render(self) -> str:
        d = self.data
        lines = [f"[{self.tab}] {d['title']}  <{d['url']}>"]
        if d.get("alerts"):
            lines.append("ALERTS: " + " || ".join(d["alerts"]))
        if d.get("cart"):
            lines.append("CART WIDGET: " + " || ".join(d["cart"]))
        for p in d.get("products", []):
            offers = p.get("offers", [])
            in_stock = sum(1 for o in offers if o["avail"].lower() == "instock")
            detail = ", ".join(f"{o['url'] or o['name']}:{o['avail']}" for o in offers[:12])
            lines.append(f"PRODUCT DATA: {p.get('name')} — {len(offers)} offers, {in_stock} in stock ({detail})")
        lines.append("TEXT: " + d["text"])
        if d.get("imgs"):
            lines.append("IMAGES: " + " | ".join(
                f"{i['id']} {i['alt'] or '-'} {short_url(i['src'])}" + (f" → {short_url(i['href'])}" if i.get("href") else "")
                for i in d["imgs"]))
        lines.append(f"ELEMENTS ({len(d['items'])} of {d['total']}):")
        for it in d["items"]:
            extra = f" — {it['extra']}" if it["extra"] else ""
            lines.append(f"  [{it['ref']}] {it['kind']} \"{it['text']}\"{extra}")
        return "\n".join(lines)


def short_url(u: str) -> str:
    p = urlparse(u)
    return p.path + (("?" + p.query) if p.query else "")


class Browser:
    """Owns the persistent Chrome profile and the agent's tabs (t1, t2, ...)."""

    def __init__(self, drop: Drop, log):
        self.drop = drop
        self.log = log
        self.pw: Playwright | None = None
        self.ctx: BrowserContext | None = None
        self.tabs: dict[str, Page] = {}
        self._inflight: dict[Page, int] = {}
        self._last_activity: dict[Page, float] = {}
        self._net: dict[Page, list[tuple[float, str]]] = {}
        self._opening_tab = False
        self._n = 0
        block = DEFAULT_BLOCK_HOSTS + drop.browser.block_hosts
        self._block = [h.lower() for h in block]

    async def start(self) -> None:
        self.pw = await async_playwright().start()
        self.drop.browser.profile_dir.mkdir(parents=True, exist_ok=True)
        kwargs = dict(
            user_data_dir=str(self.drop.browser.profile_dir),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        if self.drop.browser.channel:
            kwargs["channel"] = self.drop.browser.channel
        self.ctx = await self.pw.chromium.launch_persistent_context(**kwargs)
        self.ctx.set_default_timeout(15000)
        # Glob patterns (not a catch-all handler) so ordinary requests never round-trip through Python.
        for host in self._block:
            for pattern in (f"**://{host}/**", f"**://*.{host}/**"):
                await self.ctx.route(pattern, lambda route: route.abort())
        self.ctx.on("page", self._on_new_page)
        for p in self.ctx.pages:  # the profile opens with one blank tab: reuse it
            self._register(p)

    async def stop(self) -> None:
        for closer in (self.ctx, self.pw):
            if closer is None:
                continue
            try:
                await (closer.close() if closer is self.ctx else closer.stop())
            except Exception:
                pass  # already closed by the user
        self.ctx = self.pw = None

    def _register(self, page: Page) -> str:
        for tid, p in self.tabs.items():
            if p is page:
                return tid
        self._n += 1
        tid = f"t{self._n}"
        self.tabs[tid] = page
        self._inflight[page] = 0
        self._last_activity[page] = time.perf_counter()

        def busy(req, d):
            if req.resource_type in ("document", "xhr", "fetch"):
                self._inflight[page] = max(0, self._inflight.get(page, 0) + d)
                self._last_activity[page] = time.perf_counter()

        page.on("request", lambda r: busy(r, +1))
        page.on("requestfinished", lambda r: busy(r, -1))
        page.on("requestfailed", lambda r: busy(r, -1))

        # What each action triggered on the network, so the agent can tell a failed add (5xx) from a real one.
        net: list[tuple[float, str]] = []
        self._net[page] = net

        def record(r, status):
            if r.resource_type in ("document", "xhr", "fetch"):
                net.append((time.perf_counter(), f"{r.method} {short_url(r.url)} → {status}"))
                del net[:-30]

        page.on("response", lambda resp: record(resp.request, resp.status))
        page.on("requestfailed", lambda r: record(r, "failed"))
        page.on("close", lambda _: self.tabs.pop(tid, None))
        return tid

    def _on_new_page(self, page: Page) -> None:
        tid = self._register(page)
        if not self._opening_tab:  # only pop-ups the site opened itself are worth a log line
            self.log("tool", f"new tab {tid} opened by the page")

    async def new_tab(self) -> str:
        self._opening_tab = True
        try:
            page = await self.ctx.new_page()
        finally:
            self._opening_tab = False
        return self._register(page)

    def page(self, tab: str) -> Page:
        if tab not in self.tabs:
            raise KeyError(f"no tab {tab!r}; open tabs: {', '.join(self.tabs) or 'none'}")
        return self.tabs[tab]

    async def settle(self, page: Page, quiet: float = 0.25, max_wait: float = 6.0) -> None:
        """Wait until no document/XHR/fetch request has been in flight for `quiet` seconds."""
        start = time.perf_counter()
        await asyncio.sleep(0.05)
        while time.perf_counter() - start < max_wait:
            idle_for = time.perf_counter() - self._last_activity.get(page, 0)
            if self._inflight.get(page, 0) == 0 and idle_for >= quiet:
                break
            await asyncio.sleep(0.05)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass

    async def goto(self, tab: str, url: str) -> str | None:
        page = self.page(tab)
        try:
            resp = await page.goto(self.drop.url(url), wait_until="domcontentloaded", timeout=20000)
            status = resp.status if resp else None
            await self.settle(page, quiet=0.15, max_wait=3.0)
            if status and status >= 400:
                return f"HTTP {status}"
        except Exception as e:  # timeouts under drop load are expected; snapshot what we have
            return f"navigation problem: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
        return None

    async def snapshot(self, tab: str, max_els: int = 70, max_text: int = 1400, max_imgs: int = 24) -> Snapshot:
        page = self.page(tab)
        last_err = None
        for _ in range(3):  # the page may be mid-navigation
            try:
                data = await page.evaluate(SNAPSHOT_JS, {"maxEls": max_els, "maxText": max_text, "maxImgs": max_imgs})
                return Snapshot(tab, data)
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.3)
        raise RuntimeError(f"snapshot failed: {last_err}")

    async def act(self, tab: str, steps: list[dict]) -> list[str]:
        """Run steps in order on one tab; returns a short note per step."""
        page = self.page(tab)
        notes = []
        for i, step in enumerate(steps, 1):
            action = step.get("action")
            ref = step.get("ref", "")
            value = step.get("value", "")
            loc = page.locator(f'[data-ai-ref="{ref}"]') if ref else None
            t_step = time.perf_counter()
            try:
                if action == "click":
                    note = f"{i}. clicked {ref}"
                    for attempt in range(1 + CLICK_RETRIES):
                        try:
                            if attempt:  # after a failed add the theme leaves its loading overlay on the button
                                raise RuntimeError("retry: click via JS")
                            await loc.click(timeout=1500)
                        except Exception:
                            await loc.evaluate("el => el.click()")
                        await self.settle(page)
                        note += self._requests_since(page, t_step)
                        # A POST that came back 5xx/failed was not processed: click again at once
                        # (overloaded shop at drop time) instead of spending a model turn on it.
                        if not self._post_failed(page, t_step) or attempt == CLICK_RETRIES:
                            break
                        note += " -> auto-retry"
                        await asyncio.sleep(0.4)
                        t_step = time.perf_counter()
                    notes.append(note)
                elif action == "select":
                    found = await page.evaluate(FIND_OPTION_JS, [ref, value])
                    if "error" in found:
                        notes.append(f"{i}. select {ref} FAILED: {found['error']}")
                        break
                    try:
                        await loc.select_option(value=found["value"], timeout=3000)
                    except Exception:
                        await page.evaluate(FORCE_SELECT_JS, [ref, found["value"]])
                    await self.settle(page, quiet=0.15, max_wait=2.0)
                    notes.append(f"{i}. selected {value!r} in {ref}" + (" (option is disabled!)" if found.get("disabled") else ""))
                elif action == "type":
                    await loc.fill(value, timeout=4000)
                    notes.append(f"{i}. typed into {ref}")
                elif action == "press":
                    await (loc.press(value) if loc else page.keyboard.press(value))
                    await self.settle(page)
                    notes.append(f"{i}. pressed {value}" + self._requests_since(page, t_step))
                elif action == "wait":
                    await asyncio.sleep(min(float(value or 1), 10))
                    notes.append(f"{i}. waited {value}s")
                else:
                    notes.append(f"{i}. unknown action {action!r}")
                    break
            except Exception as e:
                notes.append(f"{i}. {action} {ref} FAILED: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
                break
        return notes

    def _post_failed(self, page: Page, t: float) -> bool:
        posts = [desc for ts, desc in self._net.get(page, []) if ts >= t and desc.startswith("POST ")]
        return bool(posts) and all(re.search(r"→ (5\d\d|failed)$", d) for d in posts)

    def _requests_since(self, page: Page, t: float) -> str:
        reqs = [desc for ts, desc in self._net.get(page, []) if ts >= t]
        return f" (requests: {'; '.join(reqs[-4:])})" if reqs else " (no requests)"

    async def image_blocks(self, tab: str, image_ids: list[str], max_side: int = 512, limit: int = 6,
                           labels: dict[str, str] | None = None) -> list[dict]:
        page = self.page(tab)
        blocks = []
        for iid in image_ids[:limit]:
            src = await page.evaluate(IMAGE_LOOKUP_JS, iid)
            if not src:
                blocks.append({"type": "text", "text": f"{iid}: unknown image id"})
                continue
            try:
                resp = await self.ctx.request.get(src, timeout=10000)
                data = encode_image(await resp.body(), max_side)
                label = (labels or {}).get(iid) or f"{iid} ({short_url(src)})"
                blocks.append({"type": "text", "text": f"[{tab}] {label}:"})
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
            except Exception as e:
                blocks.append({"type": "text", "text": f"{iid}: could not load ({type(e).__name__})"})
        return blocks

    async def screenshot_block(self, tab: str) -> dict:
        png = await self.page(tab).screenshot(type="jpeg", quality=70)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encode_image(png, 1280)}}


def encode_image(raw: bytes, max_side: int) -> str:
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def file_image_block(path, max_side: int = 512) -> dict:
    with open(path, "rb") as f:
        data = encode_image(f.read(), max_side)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
