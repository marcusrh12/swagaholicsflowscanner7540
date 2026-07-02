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


def render(
    session_name: str,
    macro: dict,
    analysis: dict,
    tickers_scanned: int,
    streaks: Optional[dict] = None,
) -> str:
    """Render HTML, write latest + history files, and return the latest path."""
    now = dt.datetime.now().astimezone()
    session_label = f"{session_name.title()} session"

    context = {
        "session_label": session_label,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "auto_refresh": config.AUTO_REFRESH_SECONDS,
        "macro": {
            "spy": _macro_cell(macro.get("spy")),
            "qqq": _macro_cell(macro.get("qqq")),
            "vix": _vix_cell(macro.get("vix", {})),
        },
        "tickers_scanned": tickers_scanned,
        "market_summary": analysis.get("market_summary", ""),
        "cards": analysis.get("trade_cards", []),
        "error": analysis.get("error"),
    }

    html = _env.get_template("template.html").render(**context)
    html = _inject_streak_badges(html, streaks)

    config.LATEST_HTML.write_text(html, encoding="utf-8")

    stamp = now.strftime("%Y%m%d_%H%M%S")
    history_path = config.HISTORY_DIR / f"{session_name}_{stamp}.html"
    history_path.write_text(html, encoding="utf-8")

    logger.info("Rendered HTML -> %s (archived %s)", config.LATEST_HTML, history_path.name)
    return str(config.LATEST_HTML)
