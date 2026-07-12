"""
Publisher: push the rendered HTML report to GitHub Pages.

Commits the freshly rendered report to `index.html` on the repo's default
branch via the GitHub Contents API, which GitHub Pages then serves. Failure is
non-fatal: any API error is logged and the scan continues.
"""

from __future__ import annotations

import base64
import logging

import httpx

import config

logger = logging.getLogger("flowscanner.publisher")

_CONTENTS_URL = "https://api.github.com/repos/{user}/{repo}/contents/index.html"


async def publish_to_github(html_content: str, session_metadata: dict) -> None:
    """
    Publish `html_content` to `index.html` on GitHub Pages.

    Retrieves the current file SHA, base64-encodes the HTML, and commits it back
    via a PUT. Logs the live Pages URL on success. Any failure is logged and
    swallowed so a publishing hiccup never crashes the scan.
    """
    url = _CONTENTS_URL.format(user=config.REPO_OWNER, repo=config.REPO_NAME)
    headers = {
        "Authorization": f"Bearer {config.GH_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    session_type = session_metadata.get("session_type", "session")
    date = session_metadata.get("date", "")
    n_setups = session_metadata.get("setups", 0)
    commit_message = f"FlowScanner {session_type} {date} — {n_setups} setups"

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            # 1) Fetch the current file SHA (required to update an existing file).
            get_resp = await client.get(url, headers=headers)
            get_resp.raise_for_status()
            sha = get_resp.json().get("sha")

            # 2) Commit the new content.
            body = {
                "message": commit_message,
                "content": base64.b64encode(html_content.encode("utf-8")).decode("ascii"),
                "sha": sha,
            }
            put_resp = await client.put(url, headers=headers, json=body)
            put_resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- publishing must never crash the scan
        logger.error("GitHub Pages publish failed: %s", exc)
        return

    logger.info("Published report to GitHub Pages -> %s", config.PAGES_URL)
