"""
Claude engine: send the scan payload to Claude and parse the trade-card output.

Uses the official Anthropic SDK (AsyncAnthropic) with model claude-fable-5.
On failure it retries once after a delay before logging the failure. The model
response is parsed defensively into a validated list of trade cards, sorted by
confidence tier then by number of confluence signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from anthropic import AsyncAnthropic

import config
from analysis import prompt_builder

logger = logging.getLogger("flowscanner.claude")

_CONFIDENCE_RANK = {"high": 0, "medium": 1}


class ClaudeRefusal(Exception):
    """The model declined the request (stop_reason == "refusal")."""

    def __init__(self, model: str, category: Optional[str] = None):
        self.model = model
        self.category = category
        detail = f" (category: {category})" if category else ""
        super().__init__(f"{model} refused the request{detail}")


def _balanced_spans(text: str):
    """
    Yield every balanced top-level {...} span, brace-counting while ignoring braces
    inside JSON strings (and escaped characters within them).

    A naive find("{")..rfind("}") slice breaks whenever the model wraps the object
    in prose that itself contains a brace -- it swallows the trailing prose and the
    parse fails on valid output. Counting braces properly is what makes the parse
    robust to a stray sentence before or after the JSON.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield text[start : i + 1]


def _extract_json(text: str) -> Optional[dict]:
    """Pull the trade-card JSON object out of the model text."""
    text = text.strip()
    # Strip accidental markdown fences.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to balanced spans, preferring one that actually looks like the
    # contract -- the model may emit a small stray object alongside the real one.
    candidates = []
    for span in _balanced_spans(text):
        try:
            obj = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    for obj in candidates:
        if "trade_cards" in obj or "market_summary" in obj:
            return obj
    if candidates:
        return candidates[0]

    # Nothing parsed: log enough of the payload to actually diagnose it next time
    # rather than retrying blind.
    logger.error(
        "Failed to parse JSON from Claude response (%s chars). Head: %r ... Tail: %r",
        len(text),
        text[:300],
        text[-300:],
    )
    return None


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace("%", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _validate_cards(parsed: dict) -> tuple[str, list[dict]]:
    """Filter, normalize and sort trade cards from the parsed model output."""
    summary = str(parsed.get("market_summary", "")).strip()
    raw_cards = parsed.get("trade_cards", [])
    if not isinstance(raw_cards, list):
        return summary, []

    cards: list[dict] = []
    for c in raw_cards:
        if not isinstance(c, dict):
            continue
        confidence = str(c.get("confidence", "")).strip().lower()
        if confidence not in _CONFIDENCE_RANK:
            continue  # drop Low / unknown tiers
        signals = c.get("confluence_signals") or []
        if not isinstance(signals, list):
            signals = [str(signals)]
        count = c.get("confluence_count")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = len(signals)
        if count < config.MIN_CONFLUENCE_COUNT:
            continue

        contract = c.get("contract") or {}
        cards.append(
            {
                "ticker": str(c.get("ticker", "")).upper(),
                "bias": "calls",
                "confidence": "High" if confidence == "high" else "Medium",
                "confluence_count": count,
                "confluence_signals": [str(s) for s in signals],
                "thesis": str(c.get("thesis", "")).strip(),
                "contract": {
                    "expiration": str(contract.get("expiration", "")).strip(),
                    "strike": _coerce_number(contract.get("strike")),
                    "delta_target": str(contract.get("delta_target", "")).strip(),
                },
                "entry_reference": _coerce_number(c.get("entry_reference")),
                "stop_level": _coerce_number(c.get("stop_level")),
                "price_target": _coerce_number(c.get("price_target")),
                "rr_ratio": _coerce_number(c.get("rr_ratio")),
                "iv_assessment": str(c.get("iv_assessment", "")).strip(),
            }
        )

    cards.sort(
        key=lambda x: (_CONFIDENCE_RANK[x["confidence"].lower()], -x["confluence_count"])
    )
    return summary, cards


class ClaudeEngine:
    def __init__(self):
        self._client = AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=config.CLAUDE_TIMEOUT_SECONDS,
        )

    async def _call(
        self, system_prompt: str, user_content: str, model: Optional[str] = None
    ) -> Optional[str]:
        model = model or config.CLAUDE_MODEL
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        # Fable 5 uses adaptive extended thinking; bound it via output_config.effort
        # so reasoning doesn't consume the whole token budget and starve the JSON
        # output. Omitted when CLAUDE_EFFORT is blank (e.g. models without the knob).
        if getattr(config, "CLAUDE_EFFORT", ""):
            kwargs["output_config"] = {"effort": config.CLAUDE_EFFORT}
        message = await self._client.messages.create(**kwargs)

        # A safety refusal returns HTTP 200 with an empty content list, so this has
        # to be checked before reading content — otherwise it looks like a generic
        # empty response and burns the retry on a call that will refuse again.
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            raise ClaudeRefusal(model, getattr(details, "category", None))

        # Truncation must be named, not left to surface downstream as "unparseable
        # JSON": max_tokens covers thinking + output, so a rubric or payload that
        # grows can silently starve the JSON and produce a half-written card.
        if getattr(message, "stop_reason", None) == "max_tokens":
            usage = getattr(message, "usage", None)
            thinking = getattr(
                getattr(usage, "output_tokens_details", None), "thinking_tokens", None
            )
            logger.error(
                "Claude hit max_tokens (%s): %s thinking tokens left too little room "
                "for the JSON. Raise config.CLAUDE_MAX_TOKENS or lower CLAUDE_EFFORT.",
                config.CLAUDE_MAX_TOKENS,
                thinking,
            )

        # Concatenate all text blocks.
        parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        return "".join(parts) if parts else None

    async def _analyze_once(
        self, system_prompt: str, user_content: str, model: str
    ) -> dict:
        """One call + parse. Raises on refusal, empty output, or unparseable JSON."""
        text = await self._call(system_prompt, user_content, model=model)
        if not text:
            raise ValueError("Empty response from Claude")
        parsed = _extract_json(text)
        if parsed is None:
            raise ValueError("Could not parse JSON from Claude response")
        summary, cards = _validate_cards(parsed)
        logger.info(
            "Claude (%s) produced %s qualifying trade card(s)", model, len(cards)
        )
        return {"market_summary": summary, "trade_cards": cards, "error": None}

    async def _fallback_after_refusal(
        self, system_prompt: str, user_content: str, session: str
    ) -> dict:
        """Re-run the same prompt on the fallback model after a refusal."""
        fallback = config.CLAUDE_FALLBACK_MODEL
        logger.warning(
            "Falling back to %s for session %s after refusal by %s",
            fallback,
            session,
            config.CLAUDE_MODEL,
        )
        try:
            return await self._analyze_once(system_prompt, user_content, fallback)
        except ClaudeRefusal:
            message = (
                f"Claude refused the request for session {session}: "
                f"{config.CLAUDE_MODEL} and fallback {fallback} both refused"
            )
        except Exception as exc:
            message = (
                f"Claude refused the request for session {session} "
                f"({config.CLAUDE_MODEL}); fallback {fallback} failed: {exc}"
            )
        logger.error(message)
        return {"market_summary": "", "trade_cards": [], "error": message}

    async def analyze(self, payload: dict) -> dict:
        """
        Run the confluence analysis. Returns:
          {"market_summary": str, "trade_cards": [...], "error": Optional[str]}
        Retries once after config.CLAUDE_RETRY_DELAY_SECONDS on failure. A refusal
        is not retried on the same model — it falls back to CLAUDE_FALLBACK_MODEL.
        """
        system_prompt, user_content = prompt_builder.build_messages(payload)
        session = str(payload.get("scan_session", "unknown"))

        for attempt in (1, 2):
            try:
                return await self._analyze_once(
                    system_prompt, user_content, config.CLAUDE_MODEL
                )
            except ClaudeRefusal as refusal:
                # Retrying the same prompt on the same model would just refuse
                # again, so spend the attempt on the fallback model instead.
                logger.warning(
                    "Claude refused the request for session %s: %s", session, refusal
                )
                return await self._fallback_after_refusal(
                    system_prompt, user_content, session
                )
            except Exception as exc:
                logger.warning("Claude analysis attempt %s failed: %s", attempt, exc)
                if attempt == 1:
                    await asyncio.sleep(config.CLAUDE_RETRY_DELAY_SECONDS)
                else:
                    logger.error("Claude analysis failed after retry: %s", exc)
                    return {
                        "market_summary": "",
                        "trade_cards": [],
                        "error": str(exc),
                    }
        # Unreachable, but keeps type-checkers content.
        return {"market_summary": "", "trade_cards": [], "error": "unknown"}
