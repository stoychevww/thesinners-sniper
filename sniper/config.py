"""Drop configuration: loaded from a TOML file in drops/."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Target:
    key: str
    description: str
    refs: list[Path]
    sizes: list[str]
    qty: int = 1


@dataclass
class AgentSettings:
    model: str = "claude-opus-5-5"
    effort: str = "low"
    fast_mode: bool = True
    fallbacks: bool = True
    max_turns: int = 20
    max_seconds: float = 240.0
    max_tokens: int = 8000


@dataclass
class BrowserSettings:
    channel: str = "chrome"  # "chrome", "msedge", or "" for Playwright's bundled Chromium
    profile_dir: Path = ROOT / ".profile"
    block_hosts: list[str] = field(default_factory=list)


@dataclass
class Drop:
    name: str
    base_url: str
    opens_at: datetime | None
    watch_from_seconds_before: float
    start_url: str
    prefetch: list[str]
    watch_urls: list[str]
    locked_markers: list[str]
    poll_interval: float
    site_notes: str
    cart_url: str
    targets: list[Target]
    agent: AgentSettings
    browser: BrowserSettings
    source: Path

    def url(self, path_or_url: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", path_or_url)

    def with_base_url(self, base_url: str) -> "Drop":
        """Point the whole drop at another host (used by rehearsals against the mock shop)."""
        import copy

        clone = copy.copy(self)
        clone.base_url = base_url
        return clone


def load_drop(path: str | Path) -> Drop:
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    d = data["drop"]
    base_dir = path.parent

    def resolve(p: str) -> Path:
        """Relative to the drop file; falls back to the project root for paths that only exist there."""
        candidate = (base_dir / p).resolve()
        if candidate.exists():
            return candidate
        alt = (ROOT / p).resolve()
        return alt if alt.exists() else candidate

    targets = []
    for t in data.get("target", []):
        refs = [resolve(r) for r in t.get("refs", [])]
        missing = [str(r) for r in refs if not r.exists()]
        if missing:
            raise FileNotFoundError(f"target '{t['key']}': reference image(s) not found: {missing}")
        targets.append(
            Target(
                key=t["key"],
                description=t["description"],
                refs=refs,
                sizes=list(t.get("sizes", [])),
                qty=int(t.get("qty", 1)),
            )
        )
    if not targets:
        raise ValueError(f"{path}: no [[target]] entries")

    a = data.get("agent", {})
    b = data.get("browser", {})
    opens_at = d.get("opens_at")
    if isinstance(opens_at, str):
        opens_at = datetime.fromisoformat(opens_at)

    return Drop(
        name=d.get("name", path.stem),
        base_url=d["base_url"],
        opens_at=opens_at,
        watch_from_seconds_before=float(d.get("watch_from_seconds_before", 120)),
        start_url=d.get("start_url", "/"),
        prefetch=list(d.get("prefetch", [])),
        watch_urls=list(d.get("watch_urls", ["/"])),
        locked_markers=list(d.get("locked_markers", [])),
        poll_interval=float(d.get("poll_interval", 1.0)),
        site_notes=d.get("site_notes", "").strip(),
        cart_url=d.get("cart_url", ""),
        targets=targets,
        agent=AgentSettings(
            model=a.get("model", AgentSettings.model),
            effort=a.get("effort", AgentSettings.effort),
            fast_mode=bool(a.get("fast_mode", AgentSettings.fast_mode)),
            fallbacks=bool(a.get("fallbacks", AgentSettings.fallbacks)),
            max_turns=int(a.get("max_turns", AgentSettings.max_turns)),
            max_seconds=float(a.get("max_seconds", AgentSettings.max_seconds)),
            max_tokens=int(a.get("max_tokens", AgentSettings.max_tokens)),
        ),
        browser=BrowserSettings(
            channel=b.get("channel", "chrome"),
            profile_dir=resolve(b["profile_dir"]) if "profile_dir" in b else BrowserSettings().profile_dir,
            block_hosts=list(b.get("block_hosts", [])),
        ),
        source=path,
    )
