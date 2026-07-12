"""
Standalone GitHub Pages publish test.

Publishes the already-rendered output/latest.html to GitHub Pages without
running a scan (no Anthropic API calls, no cost). Use this to iterate on the
publisher / credentials.

    python test_publish.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

import config
from output.publisher import publish_to_github


async def main() -> None:
    if not config.GH_TOKEN:
        print("GH_TOKEN is empty — set it in .env before testing.")
        sys.exit(1)

    html_path = config.LATEST_HTML
    if not html_path.exists():
        print(f"No rendered HTML at {html_path}. Run a scan once, or point at any HTML file.")
        sys.exit(1)

    html_content = html_path.read_text(encoding="utf-8")
    session_metadata = {
        "session_type": "test",
        "date": dt.date.today().isoformat(),
        "setups": 0,
    }
    await publish_to_github(html_content, session_metadata)


if __name__ == "__main__":
    config.setup_logging()
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
