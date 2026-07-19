"""
Renderer: turn the analysis result + macro context into an HTML page.

Writes two files:
  * output/latest.html            -- the current session (auto-refreshing)
  * output/history/<timestamp>.html -- a timestamped archive of every session
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config

logger = logging.getLogger("flowscanner.renderer")

_env = Environment(
    loader=FileSystemLoader(str(config.OUTPUT_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _macro_cell(m: Optional[dict]) -> dict:
    """Build a display cell for SPY/QQQ from the macro record."""
    if not m or not m.get("available"):
        return {"text": "n/a", "trend_class": "neutral"}
    trend = m.get("trend", "neutral")
    cls = "neutral"
    if "bull" in trend:
        cls = "bullish"
    elif "bear" in trend:
        cls = "bearish"
    close = m.get("close")
    text = f"{trend.title()}"
    if close is not None:
        text += f" @ {close:.2f}"
    return {"text": text, "trend_class": cls}


def _vix_cell(vix: dict) -> dict:
    level = vix.get("level")
    regime = vix.get("regime", "unknown")
    cls = "neutral"
    if level is not None:
        if level < 20:
            cls = "bullish"
        elif level >= 28:
            cls = "bearish"
    text = f"{level:.2f} ({regime})" if level is not None else "n/a"
    return {"text": text, "cls": cls}


def _tape_cell(day_progress: Optional[dict]) -> dict:
    """
    The header's Tape chip: "is today actually a day to be buying?".

    The complaint this answers is that a broad down day was invisible on the page --
    you had to infer it from the cards, which is exactly backwards. Premarket says so
    honestly rather than dressing yesterday up as today.
    """
    if not day_progress:
        return {"text": "n/a", "cls": "neutral"}

    if day_progress.get("as_of") == "premarket":
        prior = day_progress.get("prior_session") or {}
        pos = prior.get("spy_range_position_pct")
        text = "Premarket"
        if pos is not None:
            text += f" &middot; prior close {pos:.0f}% of range"
        return {"text": text, "cls": "neutral"}

    tape = day_progress.get("tape", "unknown")
    breadth = day_progress.get("breadth") or {}
    green = breadth.get("pct_green")
    label = {
        "broad_selloff": "Broad selloff",
        "soft": "Soft",
        "mixed": "Mixed",
        "firm": "Firm",
        "broad_rally": "Broad rally",
        "unknown": "Unknown",
    }.get(tape, tape)
    cls = {
        "broad_selloff": "bearish",
        "soft": "bearish",
        "firm": "bullish",
        "broad_rally": "bullish",
    }.get(tape, "neutral")
    text = label
    if green is not None:
        text += f" &middot; {green:.0f}% green"
    return {"text": text, "cls": cls}


def _zone_pill(card: dict) -> Optional[dict]:
    """Status pill for a card's entry zone, or None when there is no zone to show."""
    zone = card.get("entry_zone") or {}
    if not zone.get("available"):
        return None
    if card.get("state") == "watch":
        hi = zone.get("zone_high")
        return {
            "cls": "zone-watch",
            "text": f"WATCH · trigger {hi:.2f}" if hi is not None else "WATCH",
        }
    status = zone.get("status")
    if status == "in_zone":
        return {"cls": "zone-in", "text": "IN ZONE"}
    if status == "extended":
        d = zone.get("dist_to_zone_pct")
        return {
            "cls": "zone-ext",
            "text": f"EXTENDED {abs(d):.1f}%" if d is not None else "EXTENDED",
        }
    if status == "below_zone":
        return {"cls": "zone-below", "text": "BELOW ZONE"}
    return None


def _zone_caption(card: dict) -> str:
    """The one-line "what does this band mean for me" under the zone numbers."""
    zone = card.get("entry_zone") or {}
    if not zone.get("available"):
        return "no defined support below"
    quality = zone.get("quality", "")
    status = zone.get("status")
    if status == "in_zone":
        return f"price is here now · {quality}"
    if status == "extended":
        d = zone.get("dist_to_zone_pct")
        atr = zone.get("dist_to_zone_atr")
        parts = []
        if d is not None:
            parts.append(f"{abs(d):.1f}% below")
        if atr is not None:
            parts.append(f"{atr:.1f} ATR")
        return " · ".join(parts + [quality]) if parts else quality
    if status == "below_zone":
        return f"support lost · {quality}"
    return quality


def _streak_badge_html(info: dict) -> str:
    """Inline-styled streak badge (styles inline since the template CSS is fixed)."""
    hits = int(info.get("hits", 0))
    if hits == 2:
        label = "2nd hit"
        style = "background:rgba(210,153,34,0.15);color:#d29922;border:1px solid #d29922;"
    elif hits >= 3:
        ordinal = info.get("ordinal", f"{hits}th")
        trend = info.get("trend", "stable")
        label = f"{ordinal} hit — {trend}"
        style = "background:rgba(63,185,80,0.15);color:#3fb950;border:1px solid #3fb950;"
    else:
        return ""
    return f'<span class="badge" style="{style}">{label}</span>'


def _inject_streak_badges(html: str, streaks: Optional[dict]) -> str:
    """
    Insert a streak badge immediately after the confluence-count badge for any
    ticker on a live streak. Done as a post-render string edit so the shared
    template.html stays untouched.
    """
    if not streaks:
        return html
    for ticker, info in streaks.items():
        badge = _streak_badge_html(info)
        if not badge:
            continue
        pattern = re.compile(
            r'(<div class="ticker">' + re.escape(ticker) +
            r'<small>[^<]*</small></div>\s*<div class="badges">\s*'
            r'<span class="badge count">[^<]*</span>)'
        )
        html = pattern.sub(lambda m: m.group(1) + badge, html, count=1)
    return html


# Break-state pill for a breakout card: where the setup is in the break sequence.
# Amber "approaching" (coiled, not triggered), green "breaking" (crossing now on
# volume), blue "extended" (already broke -- watching for a retest).
_BREAK_STATE_PILL = {
    "approaching": {"cls": "state-approaching", "text": "APPROACHING"},
    "breaking": {"cls": "state-breaking", "text": "BREAKING"},
    "extended": {"cls": "state-extended", "text": "EXTENDED"},
}


def _break_state_pill(card: dict) -> Optional[dict]:
    return _BREAK_STATE_PILL.get(str(card.get("break_state", "")).lower())


def _enrich_zone(card: dict) -> None:
    """Attach the precomputed zone display bits to one card, in place."""
    card["zone_pill"] = _zone_pill(card)
    card["zone_caption"] = _zone_caption(card)
    # An at-zone ratio earned by a strike that went far OTM is arithmetically
    # true and practically a lottery ticket. Flag it rather than let the big
    # number speak for itself.
    d = (card.get("zone_entry") or {}).get("delta_at_zone")
    card["zone_rr_is_thin"] = d is not None and d < config.ZONE_MIN_DELTA_AT_ZONE


def render(
    session_name: str,
    macro: dict,
    analysis: dict,
    tickers_scanned: int,
    streaks: Optional[dict] = None,
) -> tuple[str, str]:
    """Render HTML, write latest + history files, and return (latest path, HTML string)."""
    now = dt.datetime.now().astimezone()
    session_label = f"{session_name.title()} session"

    # Precompute the zone display bits here rather than in Jinja: the template should
    # place text, not decide what a zone status means. Both the core setups and the
    # breakout archetype run through the same enrichment -- a breakout card carries
    # every standard field plus its break-state pill.
    cards = analysis.get("trade_cards", [])
    for c in cards:
        _enrich_zone(c)

    breakout_cards = analysis.get("breakout_cards", []) or []
    for c in breakout_cards:
        _enrich_zone(c)
        c["state_pill"] = _break_state_pill(c)

    context = {
        "session_label": session_label,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "auto_refresh": config.AUTO_REFRESH_SECONDS,
        "macro": {
            "spy": _macro_cell(macro.get("spy")),
            "qqq": _macro_cell(macro.get("qqq")),
            "vix": _vix_cell(macro.get("vix", {})),
            "tape": _tape_cell(macro.get("day_progress")),
        },
        "tickers_scanned": tickers_scanned,
        "market_summary": analysis.get("market_summary", ""),
        "cards": cards,
        "breakout_cards": breakout_cards,
        "error": analysis.get("error"),
    }

    html = _env.get_template("template.html").render(**context)
    html = _inject_streak_badges(html, streaks)

    config.LATEST_HTML.write_text(html, encoding="utf-8")

    stamp = now.strftime("%Y%m%d_%H%M%S")
    history_path = config.HISTORY_DIR / f"{session_name}_{stamp}.html"
    history_path.write_text(html, encoding="utf-8")

    logger.info("Rendered HTML -> %s (archived %s)", config.LATEST_HTML, history_path.name)
    return str(config.LATEST_HTML), html
