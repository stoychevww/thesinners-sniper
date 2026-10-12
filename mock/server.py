"""Local stand-in for a GombaShop store, modelled on thesinners.eu, for rehearsing a drop.

Pages are rebuilt from archived copies of the real store (product page, catalog page,
lock page), so the agent sees realistic markup, scripts and noise. Static files and
product photos are fetched from the live site once and cached in mock/cache/.

    python -m mock.server --open-in 20 --obscure-names --drain
"""

from __future__ import annotations

import argparse
import copy
import gzip
import html
import json
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / "cache"
LIVE = "https://www.thesinners.eu"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

TEMPLATES = {
    "product.html": "https://web.archive.org/web/20260606222230id_/https://www.thesinners.eu/en-product-details-356.html",
    "catalog.html": "https://web.archive.org/web/20240418204039id_/https://www.thesinners.eu/en-catalog-details-9.html",
    "lock.html": LIVE + "/",
}

SIZES = [("XS", 1810), ("S", 1811), ("M", 1812), ("L", 1813), ("XL", 1814)]
SIZE_PARAM = 399
BGN = 1.95583

# Local stand-in photos (not on the live server yet): media id -> file
LOCAL_MEDIA = {9001: ROOT / "refs" / "fyut" / "tee.png"}


@dataclass
class Product:
    key: str
    pid: int
    name: str
    obscure_name: str
    price: float
    images: list[int]
    description: str
    stock: dict[str, int] = field(default_factory=dict)
    sellout_at: dict[str, float] = field(default_factory=dict)  # size -> seconds after open


SCENARIO = [
    Product("hoodie", 460, "FYUT HOODIE BLACK", "LITTLE DEVIL HOODIE", 95, [2021],
            "Oversized heavyweight hoodie with the 'little devil' hand graphic on the back."),
    Product("pants_red", 462, "FYUT TRACK PANTS BLACK/RED", "TRACK PANTS", 85, [2095, 2061, 2063, 2064],
            "Track pants with contrast piping and embroidered logos. Elasticated waist."),
    Product("pants_grey", 463, "FYUT TRACK PANTS BLACK/GREY", "TRACK PANTS", 85, [2069, 2070, 2071],
            "Track pants with contrast piping and embroidered logos. Elasticated waist."),
    Product("tee", 465, "FYUT T-SHIRT BLACK", "LITTLE DEVIL T-SHIRT", 50, [9001],
            "Heavyweight oversized T-shirt with front graphic."),
    Product("zip", 466, "FYUT ZIP HOODIE BLACK/RED", "ZIP HOODIE", 110, [2080, 2081],
            "Zip hoodie with contrast piping."),
    Product("hoodie_grey", 467, "FYUT HOODIE GREY", "LITTLE DEVIL HOODIE", 95, [2025],
            "Oversized heavyweight hoodie with the 'little devil' hand graphic on the back."),
    Product("tee_333", 468, "333 T-SHIRT BLACK", "T-SHIRT", 50, [2008], "Heavyweight T-shirt with 333 graphic."),
    Product("tee_hands", 469, "HANDS T-SHIRT BLACK", "T-SHIRT", 50, [1913], "Heavyweight T-shirt with hands graphic."),
]


# ------------------------------------------------------------------ fetching / caching

def _fetch(url: str, attempts: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == attempts - 1:
                raise
            time.sleep(10 * (i + 1))  # the Wayback Machine rate-limits bursts
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def ensure_templates() -> dict[str, str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, url in TEMPLATES.items():
        p = CACHE / name
        if not p.exists():
            print(f"[mock] fetching template {name} …")
            p.write_bytes(_fetch(url))
        out[name] = p.read_text(encoding="utf-8", errors="replace")
    if "temporarily unavailable" not in out["lock.html"]:
        raise SystemExit("[mock] cached lock.html is not a lock page; delete mock/cache/lock.html and refetch while the shop is locked")
    return out


_asset_lock = threading.Lock()


def cached_asset(path: str) -> tuple[bytes, str] | None:
    """Serve /static, /media and css paths from cache, fetching from the live site on first use."""
    m = re.match(r"^/media/\d+/(\d+)\.(jpg|png|jpeg)$", path)
    if m and int(m.group(1)) in LOCAL_MEDIA:
        f = LOCAL_MEDIA[int(m.group(1))]
        return f.read_bytes(), "image/png" if f.suffix == ".png" else "image/jpeg"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", path.strip("/")) or "index"
    p = CACHE / "assets" / safe
    ctype = guess_type(path)
    with _asset_lock:
        if not p.exists():
            try:
                data = _fetch(LIVE + path)
            except Exception:
                return None
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    return p.read_bytes(), ctype


def guess_type(path: str) -> str:
    path = path.split("?")[0]
    for ext, t in ((".css", "text/css"), (".js", "application/javascript"), (".png", "image/png"),
                   (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".gif", "image/gif"), (".svg", "image/svg+xml"),
                   (".ico", "image/x-icon"), (".woff2", "font/woff2"), (".woff", "font/woff"), (".ttf", "font/ttf")):
        if path.endswith(ext):
            return t
    if "css" in path:
        return "text/css"
    return "application/octet-stream"


# ------------------------------------------------------------------ shop state

class Shop:
    def __init__(self, open_in: float, obscure: bool, drain: bool, soldout: list[str], seed: int,
                 surprise: bool = False, log_path: Path | None = None, chaos: bool = False):
        self.t_start = time.time()
        self.open_at = self.t_start + open_in
        self.obscure = obscure
        self.chaos = chaos
        self.chaos_rng = random.Random(seed + 1)
        self.log_path = log_path
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        products = [copy.deepcopy(p) for p in SCENARIO]
        if surprise:
            # The tee moves to an id that is not pre-opened; the grey look-alike hoodie takes its tab.
            remap = {"tee": 470, "hoodie_grey": 465}
            for p in products:
                p.pid = remap.get(p.key, p.pid)
        self.products = {p.pid: p for p in products}
        self.carts: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        timer = threading.Timer(max(0.0, open_in), lambda: self.log(
            f"UNLOCKED at {time.strftime('%H:%M:%S', time.localtime(self.open_at))}.{int(self.open_at % 1 * 1000):03d}"))
        timer.daemon = True
        timer.start()
        rng = random.Random(seed)
        for p in self.products.values():
            for size, _ in SIZES:
                p.stock[size] = rng.randint(2, 8)
                p.sellout_at[size] = rng.uniform(25, 120) if drain else float("inf")
        for spec in soldout:  # e.g. "hoodie:M" or "tee"
            key, _, size = spec.partition(":")
            for p in self.products.values():
                if p.key == key:
                    for s, _ in SIZES:
                        if not size or s == size:
                            p.stock[s] = 0

    def log(self, msg: str) -> None:
        line = f"[mock {time.time() - self.open_at:+7.2f}s] {msg}"
        print(line, flush=True)
        if self.log_path:
            with self.lock, self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    @property
    def is_open(self) -> bool:
        return time.time() >= self.open_at

    CHAOS_SECONDS = 30

    def overloaded(self) -> tuple[bool, float]:
        """Chaos mode, first 30 s after opening: (answer 503?, extra delay) like a real drop-time server."""
        if not (self.chaos and self.is_open and time.time() - self.open_at < self.CHAOS_SECONDS):
            return False, 0.0
        with self.lock:
            fail = self.chaos_rng.random() < 0.3
            delay = self.chaos_rng.uniform(0.2, 1.5)
        return fail, delay

    def in_stock(self, p: Product, size: str) -> bool:
        if p.stock.get(size, 0) <= 0:
            return False
        return (time.time() - self.open_at) < p.sellout_at.get(size, float("inf"))

    def name(self, p: Product) -> str:
        return p.obscure_name if self.obscure else p.name


def money(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def bgn(v: float) -> str:
    return f"{v * BGN:,.2f}".replace(",", " ").replace(".", ",") + " лв."


# ------------------------------------------------------------------ rendering

ANALYTICS = re.compile(r"<script\b[^>]*>(?:(?!</script>).)*?(?:fbq\(|gtag\(|googletagmanager|pinterest|fbdynamicproducts)(?:(?!</script>).)*?</script>", re.S)


def strip_analytics(page: str) -> str:
    page = ANALYTICS.sub("", page)
    return re.sub(r'<script[^>]+src="[^"]*(googletagmanager|pinterest|facebook)[^"]*"[^>]*></script>', "", page)


class Renderer:
    def __init__(self, shop: Shop, t: dict[str, str]):
        self.shop = shop
        self.product_tpl = strip_analytics(t["product.html"])
        self.catalog_tpl = strip_analytics(t["catalog.html"])
        self.lock_tpl = strip_analytics(t["lock.html"])
        # The live shop injects a custom head script into every page, and it contains the lock
        # message text. The 2024 catalog snapshot predates it; add it so lock detection is tested honestly.
        m = re.search(r"<script>(?:(?!</script>).)*closedMessageElement.*?</script>", self.product_tpl, re.S)
        if m and "closedMessageElement" not in self.catalog_tpl:
            self.catalog_tpl = self.catalog_tpl.replace("</head>", m.group(0) + "\n</head>", 1)

    # -- lock / 404 / simple pages built on the "universal" checkout skeleton

    def lock(self, title: str = "THESINNERS") -> str:
        return self.lock_tpl.replace("<title>THESINNERS</title>", f"<title>{title}</title>", 1)

    def simple(self, title: str, body_html: str) -> str:
        page = self.lock_tpl.replace("<title>THESINNERS</title>", f"<title>{html.escape(title)}</title>", 1)
        return re.sub(r'<section class="checkout login">.*?</section>',
                      f'<section class="checkout"><div class="checkout-content">{body_html}</div></section>',
                      page, count=1, flags=re.S)

    # -- product page

    def variants(self, p: Product) -> dict:
        out = {}
        for i, (size, opt) in enumerate(SIZES):
            vid = str(p.pid * 10 + i)
            stock = self.shop.in_stock(p, size)
            out[vid] = {
                "Id": vid, "productId": str(p.pid), "parentId": "9", "brandId": "0",
                "sku": f"{p.pid}-*{SIZE_PARAM}-{opt}*", "optionsName": size, "imageId": "0", "code": "", "barcode": "",
                "orgPrice": f"{p.price:.2f}", "price": f"{p.price:.2f}", "qtty": str(p.stock[size] if stock else 0),
                "trackInventory": "1", "inventoryPolicy": "0", "orderId": "10000",
                "options": {str(SIZE_PARAM): {"paramId": str(SIZE_PARAM), "optId": str(opt)}},
                "map": {str(SIZE_PARAM): str(opt)}, "discount": 0,
                "priceF": money(p.price), "orgPriceF": money(p.price), "inStock": stock,
                "priceEuro": round(p.price * BGN, 2), "priceEuroF": bgn(p.price),
            }
        return out

    def product(self, p: Product) -> str:
        name = html.escape(self.shop.name(p))
        page = self.product_tpl
        thumbs = "".join(
            f'<div class="gs-thumbs {"active" if i == 0 else ""}" style=""><a href="" data-gallery-thumb>'
            f'<img id="tmbPic{img}" src="/media/78/{img}.jpg" data-big-image="/media/99/{img}.jpg" '
            f'data-zoom-image="/media/18/{img}.jpg" data-pop-image="/media/18/{img}.jpg" alt="{name}">'
            f'<span class="gs-pattern main-color"></span></a></div>'
            for i, img in enumerate(p.images)
        )
        page = re.sub(r'<div class="gs-detail-thumbs">.*?<div class="clearfix"></div>',
                      f'<div class="gs-detail-thumbs">{thumbs}<div class="clearfix"></div>', page, count=1, flags=re.S)
        page = page.replace("1434", str(p.images[0]))
        page = page.replace("DEATH T- SHIRT BLACK", name)
        page = re.sub(r"(product-details-)356(\.html)", rf"\g<1>{p.pid}\2", page)
        page = page.replace('value="356"', f'value="{p.pid}"').replace('data-product-id="356"', f'data-product-id="{p.pid}"')
        page = page.replace("['356']", f"['{p.pid}']")
        page = page.replace("43,00 €", money(p.price)).replace("/84,10 лв.", "/" + bgn(p.price))
        page = page.replace('content="43.00"', f'content="{p.price:.2f}"')
        page = re.sub(r'content="DEATH T-Shirt[^"]*"', f'content="{html.escape(p.description)}"', page)
        page = re.sub(r'(<div class="gs-rtf">)\s*<p>DEATH T-Shirt.*?(</div>)',
                      rf"\1<p>{html.escape(p.description)}</p><p>Made in Bulgaria.</p>\2", page, count=1, flags=re.S)
        options = "".join(f'<option value="{opt}" class="gs-sel-option">{size}</option>' for size, opt in SIZES)
        page = re.sub(r'(<select class="data-param"[^>]*>).*?(</select>)', rf'\1<option value=""></option>{options}\2',
                      page, count=1, flags=re.S)
        variants = self.variants(p)
        js = json.dumps(variants, ensure_ascii=True).replace("'", "\\u0027")
        page = re.sub(r"pub\.product\.variant\.init\('.*?', 'orderFrm', '1'\)",
                      lambda _: f"pub.product.variant.init('{js}', 'orderFrm', '1')", page, count=1, flags=re.S)
        offers = ",".join(
            f'{{"@type":"Offer","availability":"http://schema.org/{"InStock" if v["inStock"] else "OutOfStock"}",'
            f'"price":"{p.price:.2f}","priceCurrency":"EUR","url":"/bg-product-details-{p.pid}.html#variant={vid}"}}'
            for vid, v in variants.items()
        )
        ld = (f'<script type="application/ld+json">{{"@context":"http://schema.org","@type":"Product",'
              f'"name":{json.dumps(self.shop.name(p))},"image":["/media/14/{p.images[0]}.jpg"],"offers":[{offers}],"sku":"{p.pid}"}}</script>')
        page = re.sub(r'<script type="application/ld\+json">.*?</script>', lambda _: ld, page, count=1, flags=re.S)
        return page

    # -- catalog

    def catalog(self) -> str:
        page = self.catalog_tpl
        items = list(re.finditer(r'<div class="gs-grid-item gs-list-items" data-pid="\d+">.*?<div class="clearfix"></div>\s*</div>\s*</div>',
                                 page, flags=re.S))
        tpl = items[0].group(0)
        old_pid = re.search(r'data-pid="(\d+)"', tpl).group(1)
        old_name = re.search(r'alt="([^"]+)"', tpl).group(1)
        old_img = re.search(r"/media/106/(\d+)\.jpg", tpl).group(1)
        blocks = []
        for p in sorted(self.shop.products.values(), key=lambda x: x.pid, reverse=True):
            b = tpl.replace(old_pid, str(p.pid)).replace(old_name, html.escape(self.shop.name(p)))
            b = b.replace(f"/media/106/{old_img}.jpg", f"/media/106/{p.images[0]}.jpg")
            b = re.sub(r'<span class="gs-new-price">[^<]*</span>', f'<span class="gs-new-price">{money(p.price)}</span>', b)
            b = re.sub(r"(<p>\s*<span>).*?(</span>)", rf"\1{html.escape(p.description)}\2", b, count=1, flags=re.S)
            blocks.append(b)
        start, end = items[0].start(), items[-1].end()
        return page[:start] + "\n".join(blocks) + page[end:]

    # -- cart

    def cart_lines(self, sid: str) -> list[dict]:
        return self.shop.carts.get(sid, [])

    def cart_header(self, sid: str) -> str:
        lines = self.cart_lines(sid)
        count = sum(l["qty"] for l in lines)
        total = sum(l["qty"] * l["price"] for l in lines)
        return (f'<a href="/shopping-cart.html" class="gs-cart-btn" title="Cart"><span class="icon icon-ico_cart"></span>'
                f'<span class="gs-cart-count">{count}</span> <span>products</span> <strong>{money(total)}</strong></a>')

    def cart_page(self, sid: str) -> str:
        lines = self.cart_lines(sid)
        rows = "".join(
            f"<tr><td>{html.escape(l['name'])}</td><td>Size: {l['size']}</td><td>{l['qty']}</td><td>{money(l['price'] * l['qty'])}</td></tr>"
            for l in lines
        ) or '<tr><td colspan="4">Your shopping cart is empty.</td></tr>'
        total = sum(l["qty"] * l["price"] for l in lines)
        body = (f'<h1 id="title">Shopping cart</h1><table class="cart-table" border="1" cellpadding="6">'
                f"<tr><th>Product</th><th>Options</th><th>Qty</th><th>Price</th></tr>{rows}</table>"
                f"<p><strong>Total: {money(total)}</strong></p>"
                f'<p><a href="/mock-checkout.html" class="submit">Proceed to checkout</a> &nbsp; '
                f'<a href="/en-catalog-details-9.html">Back to the store</a></p>')
        return self.simple("Shopping cart", body)


# ------------------------------------------------------------------ HTTP

def make_handler(shop: Shop, r: Renderer):
    class H(BaseHTTPRequestHandler):
        server_version = "Apache"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: only cart events are printed
            pass

        def _sid(self) -> tuple[str, bool]:
            m = re.search(r"PHPSESSID=([A-Za-z0-9]+)", self.headers.get("Cookie", ""))
            return (m.group(1), False) if m else (uuid.uuid4().hex, True)

        def _send(self, code: int, body: bytes | str, ctype="text/html; charset=utf-8", sid=None, cache=False):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache, must-revalidate")
            if sid:
                self.send_header("Set-Cookie", f"PHPSESSID={sid}; path=/")
            self.end_headers()
            self.wfile.write(body)

        def _overloaded(self, path: str) -> bool:
            fail, delay = shop.overloaded()
            if delay:
                time.sleep(delay)
            if fail:
                shop.log(f"overloaded: 503 for {self.command} {path}")
                self._send(503, "<html><body><h1>Service Unavailable</h1><p>The server is temporarily unable to service "
                                "your request due to maintenance downtime or capacity problems.</p></body></html>")
            return fail

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            sid, new = self._sid()
            cookie = sid if new else None
            if path.startswith(("/static/", "/media/")) or path.endswith(".css") or path in ("/default.css", "/en-css-default-0.html", "/favicon.ico"):
                hit = cached_asset(path)
                return self._send(200, hit[0], hit[1], cache=True) if hit else self._send(404, "not found", "text/plain")
            if path == "/__mock/state":
                state = {"open": shop.is_open, "seconds_since_open": round(time.time() - shop.open_at, 1), "carts": shop.carts}
                return self._send(200, json.dumps(state, ensure_ascii=False, indent=1), "application/json")
            if not shop.is_open and path != "/__mock/state":
                m = re.match(r"^/(?:bg|en|de)-product-details-(\d+)\.html$", path)
                if m and int(m.group(1)) not in shop.products:
                    return self._send(404, r.lock("THESINNERS"), sid=cookie)
                return self._send(200, r.lock("{prdName}" if m else "THESINNERS"), sid=cookie)
            if self._overloaded(path):
                return

            m = re.match(r"^/(?:bg|en|de)-product-details-(\d+)\.html$", path)
            if m:
                p = shop.products.get(int(m.group(1)))
                return self._send(200, r.product(p), sid=cookie) if p else self._send(404, r.simple("Not found", "<h1>Page not found</h1>"), sid=cookie)
            if path in ("/", "/catalog.html", "/index.html") or re.match(r"^/(?:bg|en|de)-(?:catalog-details-\d+|index-index-0)\.html$", path):
                return self._send(200, r.catalog(), sid=cookie)
            if re.match(r"^/(?:(?:bg|en|de)-cart-axCartListHeader-0\.html|axCartListHeader\.php)$", path):
                return self._send(200, r.cart_header(sid), sid=cookie)
            if path in ("/shopping-cart.html", "/cart.html") or re.match(r"^/(?:bg|en|de)-cart-(?:list|index)-0\.html$", path):
                return self._send(200, r.cart_page(sid), sid=cookie)
            if path == "/mock-checkout.html":
                return self._send(200, r.simple("Checkout", "<h1>MOCK CHECKOUT</h1><p>On drop day this is where you take over.</p>"), sid=cookie)
            return self._send(404, r.simple("Not found", "<h1>Page not found</h1>"), sid=cookie)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            sid, new = self._sid()
            length = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if not re.match(r"^/(?:(?:bg|en|de)-cart-axAddToCart-0\.html|axAddToCart\.php)$", path):
                return self._send(404, "not found", "text/plain")
            if not shop.is_open:
                return self._send(200, r.cart_header(sid), sid=sid if new else None)
            if self._overloaded(path):
                return
            pid = int((form.get("Id") or ["0"])[0] or 0)
            opt = (form.get(f"params[{SIZE_PARAM}]") or [""])[0]
            qty = max(1, int((form.get("qtty") or ["1"])[0] or 1))
            p = shop.products.get(pid)
            size = next((s for s, o in SIZES if str(o) == opt), None)
            if not p or not size:
                shop.log(f"add-to-cart REJECTED (bad product/size) {form}")
            elif not shop.in_stock(p, size):
                shop.log(f"add-to-cart REJECTED: {shop.name(p)} ({p.key}, id {pid}) {size} is sold out")
            else:
                with shop.lock:
                    shop.carts.setdefault(sid, []).append({"pid": pid, "key": p.key, "name": shop.name(p), "size": size, "qty": qty, "price": p.price})
                shop.log(f"ADDED TO CART: {shop.name(p)} ({p.key}, id {pid}) size {size} x{qty}")
            return self._send(200, r.cart_header(sid), sid=sid if new else None)

    return H


class ExclusiveServer(ThreadingHTTPServer):
    """A stale mock from an earlier rehearsal must make this one fail, not silently share the port (Windows)."""

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start(port: int = 8765, open_in: float = 20, obscure: bool = False, drain: bool = False,
          soldout: list[str] | None = None, seed: int = 13, surprise: bool = False,
          log_path: Path | None = None, chaos: bool = False) -> tuple[ThreadingHTTPServer, Shop]:
    templates = ensure_templates()
    shop = Shop(open_in, obscure, drain, soldout or [], seed, surprise, log_path, chaos)
    renderer = Renderer(shop, templates)
    srv = ExclusiveServer(("127.0.0.1", port), make_handler(shop, renderer))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, shop


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open-in", type=float, default=20, help="seconds until the shop unlocks")
    ap.add_argument("--obscure-names", action="store_true", help="give look-alike products the same names (forces photo matching)")
    ap.add_argument("--drain", action="store_true", help="sizes sell out at random times 25-120 s after opening")
    ap.add_argument("--soldout", action="append", default=[], help="e.g. hoodie:M or tee (repeatable)")
    ap.add_argument("--surprise", action="store_true", help="tee not on a pre-opened tab; a look-alike is")
    ap.add_argument("--chaos", action="store_true", help="first 30 s after opening: 30%% of requests 503, the rest slow")
    args = ap.parse_args()
    srv, shop = start(args.port, args.open_in, args.obscure_names, args.drain, args.soldout,
                      surprise=args.surprise, chaos=args.chaos)
    print(f"[mock] http://127.0.0.1:{args.port}/ — opens in {args.open_in:.0f}s. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
