# thesinners-sniper

A Claude-driven agent for limited clothing drops. When the shop unlocks, Claude Opus 5.5 drives a visible Chrome window. It finds the items you described (by name and by comparing product photos with your reference images), picks your size and adds them to the cart. Then it hands the window to you to check out.

No CSS selectors are hardcoded. The agent reads each page as a compact text snapshot (main text, interactive elements, images, alerts, cart widget, schema.org product data) and decides what to click.

Built for TheSinners' "FYUT" drop (13.10.2026, 19:00 Sofia time), but the agent, browser layer and watcher are not tied to that shop. A drop is a TOML file.

## Setup (Windows, Python 3.11+)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # then put your ANTHROPIC_API_KEY in .env
```

It uses your installed Google Chrome (`channel = "chrome"` in the drop file). Without Chrome, set `channel = ""` and run `.venv\Scripts\python -m playwright install chromium`.

## Web UI (easiest)

On Windows, double-click **`sniper.bat`**. It sets everything up on first run, then starts the UI. Or run:

```powershell
.venv\Scripts\python -m sniper ui drops/fyut.toml
```

For the step-by-step drop-day plan, see **[DROP-DAY.md](DROP-DAY.md)**. `sniper-terminal.bat` is a no-UI backup.

The UI opens http://127.0.0.1:8800 with:
- a countdown to the drop and a live shop status (locked/open)
- your API key field (saved to `.env`) and your targets with their reference photos
- one-click pre-flight check, rehearsal (with options) and real run
- a live log with a phase tracker, model turns, browser actions and cost
- Stop/End buttons and a result card at handover
- past runs, a config editor and a gallery of the shop's photos

It runs the same commands as the CLI below in the background. The UI only listens on 127.0.0.1.

## Testing it

1. **Pre-flight check:** confirms the key works, fast mode and the prompt cache are available, and the shop is still locked.
2. **Plain rehearsal** (untick all options): a local copy of TheSinners opens after 25 s, and a Chrome window shows the agent carting the 4 items. The log shows `ADDED TO CART` lines from the mock shop. At the end the cart tab comes to the front, and you can click "Proceed to checkout" to practise the handover.
3. **Hard rehearsal:** tick "Look-alike names" and "Sizes sell out over time", or type `hoodie:M` under "Sold out from the start". The agent must tell look-alikes apart by photo and skip sold-out sizes instead of switching size.
4. **Fast mode rehearsal:** tick "Fast mode" to time the real setup. Check "Since open" when the result card appears.

## Commands

| Command | What it does |
|---|---|
| `python -m sniper ui drops/fyut.toml` | The web UI above. |
| `python -m sniper check drops/fyut.toml` | API key, fast mode, prompt cache, a sample model turn, and whether the shop is still locked. Costs a few cents. |
| `python -m sniper browser drops/fyut.toml` | Opens the bot's own Chrome profile. Save your name, address, phone and email under Autofill so checkout takes seconds. |
| `python -m sniper rehearse drops/fyut.toml` | Full run against a local copy of the shop (`mock/`) that unlocks after 25 s. Add `--obscure-names` (look-alike products share names, which forces photo matching), `--drain` (sizes sell out 25-120 s after opening), `--soldout hoodie:M`, `--fast`. |
| `python -m sniper run drops/fyut.toml` | The real thing. Opens the browser right away, starts watching `watch_from_seconds_before` before `opens_at`, and hands over when done. `--now` starts watching immediately. |
| `python -m sniper recon drops/fyut.toml` | Re-scans hidden product IDs and newly uploaded photos (GombaShop stores serve both while locked). Writes a contact sheet to `runs/`. |

Press **Enter** in the terminal at any time during the agent run to stop it and take over. The browser always stays open for you at the end, even if the agent fails.

## Drop day checklist (FYUT)

See [DROP-DAY.md](DROP-DAY.md). In short:
1. Before the drop: run a recon scan for new product IDs or tee photos, and add any new product URLs to `prefetch`.
2. 18:50: run the pre-flight check, then arm. Watching starts right away; it begins 15 minutes before `opens_at` and polls about once a second.
3. When it beeps, the cart tab is in front. Check out by hand: autofill, shipping, payment, 3DS on your phone.

## How it works

```
watcher (httpx, ~1 req/s)  ──open detected──►  reload pre-opened tabs in parallel
                                                     │ snapshots + product photos
                                                     ▼
                     Claude Opus 5.5 (effort low, fast mode, cached system + reference images)
                       │  parallel tool calls: one per tab
                       ▼
     open_url · act [select size → click Buy] · view_images · snapshot · screenshot · finish
                       │
                       ▼
           cart verified → cart tab to front → beep → you check out
```

- **Speed:** waiting for the open is plain code, not tokens. The system prompt, tools and reference images are cached and pre-warmed. Product tabs are pre-opened and their snapshots and photos go in the first message. The agent works on all items in parallel and batches "select size + Buy" into one call. A typical run is 3-5 model turns.
- **Safety rails:** the agent never touches checkout or payment. It never substitutes sizes, never adds non-targets, and treats page text as data, not instructions.
- **Degradation:** if fast mode is rate-limited or rejected, it switches to standard speed. If server-side refusal fallbacks are rejected, it turns them off. Overloaded API responses are retried immediately.
- **Logs:** every run writes `runs/<time>-<label>/` with `timeline.jsonl`, `transcript.json` and `result.json`.

## What we know about thesinners.eu (recon, 12.10.2026)

- **Platform:** GombaShop (Bulgarian SaaS, PHP/Apache). No Cloudflare and no queue system were seen. Add-to-cart is one AJAX POST.
- **Before a drop:** the whole shop serves a "temporarily unavailable" lock page with a password box. The owner removes it by hand, so the open can be a little early or late.
- **Hidden products:** product IDs that exist but are hidden return the lock page with HTTP 200, and unknown IDs return 404. Before FYUT, 460, 462, 463 and 465 were the newest.
- **Product photos** are served while locked. FYUT photos found: hands hoodie 2021, red-piping pants 2095 (and 2061/2063/2064), grey-piping pants 2069-2071, red-piping zip hoodie 2080/2081 (not a target), grey hands hoodie 2025 (not a target).
- **Product page:** "SIZE:" select, then "Buy" (AJAX, stays on the page). A sold-out size hides Buy and shows "Selected variant is not available at the moment." The theme's wishlist hearts under "Similar products" are labelled "Add to cart", which is a trap for naive bots.
- **Payment** (terms as of 2024): Stripe card or PayPal. Shipping in Bulgaria is Econt. The checkout pages were never archived anywhere, which is one reason the agent stops at the cart.

## Costs

Opus 5.5 is $4 / $20 per million input/output tokens, and $8 / $40 in fast mode. A drop run is roughly $0.50-3, and a rehearsal at standard speed is about half that. `check` costs a few cents. Every run prints its cost.

## Caveats

- Automated purchasing may break a shop's terms, and shops can cancel orders they consider botted. Use it for yourself, at a gentle request rate.
- `mock/` downloads the shop's pages and assets into `mock/cache/` for local rehearsal. `refs/` holds product photos. Both are gitignored: don't publish other people's content.
