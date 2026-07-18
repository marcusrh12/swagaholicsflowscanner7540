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
from data import pricing

logger = logging.getLogger("flowscanner.claude")

_CONFIDENCE_RANK = {"high": 0, "medium": 1}

# ACTIONABLE = clears the option R/R floor at the price you can pay right now.
# WATCH = the same setup, failing that floor today but clearing it at its entry zone.
# A watch card is a PLAN with a trigger, never an entry -- see _validate_cards.
_STATE_RANK = {"actionable": 0, "watch": 1}


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


def _candidate_index(payload: dict) -> dict[str, dict[tuple, dict]]:
    """ticker -> {(strike, expiry): candidate} over the real chain we sent the model."""
    index: dict[str, dict[tuple, dict]] = {}
    for t in payload.get("tickers", []) or []:
        sym = str(t.get("symbol", "")).upper()
        by_key = {}
        for cand in (t.get("options") or {}).get("call_candidates", []) or []:
            strike = _coerce_number(cand.get("strike"))
            expiry = str(cand.get("expiry", "")).strip()
            if strike is not None and expiry:
                by_key[(round(strike, 2), expiry)] = cand
        if by_key:
            index[sym] = by_key
    return index


def _resolve_contract(
    ticker: str, contract: dict, index: dict[str, dict[tuple, dict]]
) -> Optional[dict]:
    """
    Match the model's chosen (strike, expiry) against the real chain and rebuild the
    contract from OUR data, not the model's echo of it.

    The model only gets to *choose*; every number shown to the user -- delta, ask,
    breakeven, open interest -- is taken from the chain we fetched. A contract that
    isn't in the candidate list is not tradable, so the card is dropped rather than
    published with a strike that may not exist.
    """
    candidates = index.get(ticker)
    if not candidates:
        return None  # no chain for this ticker -> can't stand behind any contract
    strike = _coerce_number(contract.get("strike"))
    expiry = str(contract.get("expiration", "")).strip()[:10]
    if strike is None or not expiry:
        return None
    cand = candidates.get((round(strike, 2), expiry))
    if cand is None:
        return None
    return {
        "expiration": cand["expiry"],
        "strike": cand["strike"],
        "delta": cand.get("delta"),
        "ask": cand.get("ask"),
        "mid": cand.get("mid"),
        "breakeven": cand.get("breakeven"),
        "open_interest": cand.get("open_interest"),
        "iv": cand.get("iv"),
        "dte": cand.get("dte"),
    }


def _model_zone_entry(
    *,
    record: dict,
    contract: dict,
    target: float,
    model_stop: float,
    price: float,
) -> Optional[dict]:
    """
    The same trade, bought at its entry zone instead of at the current print.

    THIS IS THE MOST DANGEROUS ARITHMETIC IN THE SCANNER, and it is worth being
    explicit about why. The zone is frequently built ON `nearest_support`, and the
    prompt anchors the stop just BELOW `nearest_support`. Feed those two into a
    reward/risk untouched and entry converges on stop, risk collapses toward zero,
    and the ratio goes to infinity: every WATCH card passes, the gate becomes a
    rubber stamp, and the feature makes the scanner strictly worse than not having
    it. Measured directly: entry 202.00 with a 201.50 stop models option_rr 5.97,
    and pricing.option_reward_risk does NOT catch it -- its risk<=0.01 refusal is in
    PREMIUM terms, and a 0.50-point stop still leaves dollars of premium at risk.

    So two guards, and both are load-bearing:

      1. RE-ANCHOR THE STOP under the zone, the way a trader actually would. You do
         not keep a stop that sits above your entry.
      2. REFUSE the whole calculation when the resulting stop distance is less than
         ZONE_MIN_STOP_ATR. A stop that tight is not a stop, it is a rounding error,
         and the honest output is "unavailable" -- not a spectacular number.

    Returns None when the zone R/R cannot be modelled honestly. None means UNKNOWN,
    never "fine".
    """
    zone = record.get("entry_zone") or {}
    if not zone.get("available"):
        return None
    zone_mid = zone.get("zone_mid")
    zone_low = zone.get("zone_low")
    atr14 = (record.get("daily") or {}).get("atr14")
    if zone_mid is None or zone_low is None or not atr14 or atr14 <= 0:
        return None
    if zone_mid <= 0 or zone_mid >= target:
        return None  # entering above the target is not a trade

    # Guard 1: the stop follows the entry down. Keep the model's stop if it is
    # already lower -- never RAISE a stop to make the arithmetic look better.
    stop_below = zone.get("stop_below_zone")
    if stop_below is None:
        stop_below = zone_low - config.ZONE_STOP_ATR_BUFFER * atr14
    zone_stop = min(model_stop, stop_below) if model_stop is not None else stop_below
    if zone_stop <= 0 or zone_stop >= zone_mid:
        return None

    # Guard 2: is the risk a real risk?
    if (zone_mid - zone_stop) < config.ZONE_MIN_STOP_ATR * atr14:
        logger.info(
            "%s: zone R/R unavailable -- stop distance %.2f is under %.2f "
            "(%.2f ATR); refusing to model a risk that isn't one",
            record.get("symbol"),
            zone_mid - zone_stop,
            config.ZONE_MIN_STOP_ATR * atr14,
            (zone_mid - zone_stop) / atr14,
        )
        return None

    premium = pricing.reprice_at_entry(
        spot_now=price,
        entry_spot=zone_mid,
        strike=contract.get("strike"),
        dte=contract.get("dte"),
        iv=contract.get("iv"),
        ask_now=contract.get("ask"),
    )
    if premium is None:
        return None

    opt = pricing.option_reward_risk(
        strike=contract.get("strike"),
        ask=contract.get("ask"),
        iv=contract.get("iv"),
        dte=contract.get("dte"),
        price_target=target,
        stop_level=zone_stop,
        entry_premium=premium,
    )
    if opt is None:
        return None

    return {
        "entry_at_zone": zone_mid,
        "premium_at_zone": premium,
        # The strike is fixed, so a lower entry is a more OTM contract: the same
        # trade participates LESS in the move it is waiting for. Surfaced, not
        # gated -- the point is to stop hiding it.
        "delta_at_zone": pricing.call_delta(
            zone_mid, contract.get("strike"), contract.get("dte"), contract.get("iv")
        ),
        "stop_at_zone": round(zone_stop, 2),
        "option_rr_at_zone": opt["option_rr"],
        "rr_at_zone": round((target - zone_mid) / (zone_mid - zone_stop), 2),
    }


def _validate_cards(parsed: dict, payload: dict) -> tuple[str, list[dict]]:
    """Filter, normalize and sort trade cards from the parsed model output."""
    summary = str(parsed.get("market_summary", "")).strip()
    raw_cards = parsed.get("trade_cards", [])
    if not isinstance(raw_cards, list):
        return summary, []

    index = _candidate_index(payload)
    records = {
        str(t.get("symbol", "")).upper(): t for t in (payload.get("tickers") or [])
    }
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

        # Caution flags: 1-4 "weigh this" warnings. Coerce to a clean list of strings
        # and cap at 4 so a runaway list can't blow out the card. Empty is fine.
        cautions = c.get("cautions") or []
        if not isinstance(cautions, list):
            cautions = [str(cautions)]
        cautions = [str(x).strip() for x in cautions if str(x).strip()][:4]
        count = c.get("confluence_count")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = len(signals)
        if count < config.MIN_CONFLUENCE_COUNT:
            continue

        ticker = str(c.get("ticker", "")).upper()
        contract = _resolve_contract(ticker, c.get("contract") or {}, index)
        if contract is None:
            # The model invented a contract that isn't in the live chain. Publishing
            # it would put an unbuyable strike in front of a trader -- drop the card.
            logger.warning(
                "%s: dropping card -- contract %s not found in the live chain",
                ticker,
                c.get("contract"),
            )
            continue

        target = _coerce_number(c.get("price_target"))
        breakeven = contract.get("breakeven")
        if target is not None and breakeven is not None and target <= breakeven:
            logger.warning(
                "%s: dropping card -- price target %.2f is at/below the contract "
                "breakeven %.2f (the trade loses money even if the thesis is right)",
                ticker,
                target,
                breakeven,
            )
            continue

        # The prompt states several hard rules. Prose is not enforcement -- one
        # instruction-following lapse ships a bad trade, so re-check them here
        # against the same payload the model was given.
        record = records.get(ticker, {})

        # 1. Structure veto: a daily downtrend is distribution, whatever else fired.
        trend = ((record.get("structure") or {}).get("daily") or {}).get("trend")
        if trend == "downtrend":
            logger.warning(
                "%s: dropping card -- daily structure is a downtrend (veto)", ticker
            )
            continue

        # 2. Never hold a contract through an earnings report.
        dte = contract.get("dte")
        days_to_earnings = (record.get("earnings") or {}).get("days_to_earnings")
        if dte is not None and days_to_earnings is not None and days_to_earnings < dte:
            logger.warning(
                "%s: dropping card -- earnings in %sd but the contract runs %sd "
                "(would be held through the report)",
                ticker,
                days_to_earnings,
                dte,
            )
            continue

        # 3. Reward/risk on the UNDERLYING -- this measures the quality of the thesis.
        #
        # Entry is OURS, not the model's. It used to be whatever number the model put
        # in `entry_reference`, coerced and published unchecked -- the only figure on
        # the card that was never verified, and the denominator of the very ratio
        # this gate turns on. The entry is the current price; that is not a judgement
        # call, so there is nothing to delegate.
        stop = _coerce_number(c.get("stop_level"))
        price = _coerce_number(record.get("price"))
        if price is None or stop is None or price <= stop:
            logger.warning(
                "%s: dropping card -- entry %s / stop %s cannot define a risk",
                ticker, price, stop,
            )
            continue

        # Recompute rather than trust. The gate ran on the model's STATED ratio, so
        # an arithmetic slip shipped silently past the floor it was supposed to hit.
        rr = round((target - price) / (price - stop), 2) if target is not None else None
        if rr is None:
            logger.warning("%s: dropping card -- no price target to measure", ticker)
            continue
        stated = _coerce_number(c.get("rr_ratio"))
        if stated is not None and rr > 0 and abs(stated - rr) / rr > config.RR_DIVERGENCE_TOLERANCE:
            # The numbers it reasoned from are not the numbers it reported. Whatever
            # the thesis says, it was not derived from this trade.
            logger.warning(
                "%s: dropping card -- stated R/R %.2f but entry/stop/target give "
                "%.2f (>%.0f%% divergence)",
                ticker, stated, rr, config.RR_DIVERGENCE_TOLERANCE * 100,
            )
            continue
        if rr < config.MIN_RR_RATIO:
            logger.warning(
                "%s: dropping card -- underlying reward/risk %.2f below the %.1f floor",
                ticker, rr, config.MIN_RR_RATIO,
            )
            continue

        # 4. Reward/risk on the OPTION -- this measures the quality of the TRADE, and
        # it is the one that decides whether the card ships. You are buying a call,
        # not the stock: your risk is the premium (less whatever the option is still
        # worth at the stop), your reward is non-linear in the underlying, and theta
        # is invisible to a price-distance ratio. A 2.4 on the chart can be a losing
        # option when the move takes five weeks instead of two.
        opt = pricing.option_reward_risk(
            strike=contract.get("strike"),
            ask=contract.get("ask"),
            iv=contract.get("iv"),
            dte=contract.get("dte"),
            price_target=target,
            stop_level=stop,
        ) if target is not None else None

        if opt is None:
            logger.warning(
                "%s: dropping card -- could not model the option's reward/risk "
                "(target=%s stop=%s contract=%s)",
                ticker, target, stop, contract,
            )
            continue

        zone = record.get("entry_zone") or {}
        zone_entry = _model_zone_entry(
            record=record,
            contract=contract,
            target=target,
            model_stop=stop,
            price=price,
        )

        # THE CRUX. The gate runs on the price you can actually pay RIGHT NOW, and it
        # does not move. A card that only clears the floor at a price that may never
        # print is a trade you cannot take dressed as one you can -- exactly what this
        # function exists to prevent -- so the zone R/R never rescues a card into
        # "buy this".
        #
        # But refusing to compute it throws away the real question ("the thesis is
        # fine, where should I actually get in?"). So a setup that fails now and
        # clears at its zone becomes a third thing: a WATCH card, published as a plan
        # with a trigger price and never as an entry. Every other gate above --
        # contract-in-chain, target>breakeven, downtrend veto, earnings, underlying
        # R/R -- has already been applied to it unchanged.
        state = "actionable"
        if opt["option_rr"] < config.MIN_OPTION_RR:
            zone_rr = (zone_entry or {}).get("option_rr_at_zone")
            zone_delta = (zone_entry or {}).get("delta_at_zone")
            # The strike was picked for TODAY's price. Down at the zone it may be so
            # far OTM that its reward/risk only looks good because the premium went to
            # pennies -- a lottery ticket, not the trade in the thesis. Refuse to
            # rescue on that basis. This makes the WATCH path STRICTER; it can never
            # let a card through that would otherwise have been dropped.
            delta_ok = zone_delta is None or zone_delta >= config.ZONE_MIN_DELTA_AT_ZONE
            # Only a pullback can rescue it. If price is already IN the zone, the
            # zone IS the current price and there is no better entry to wait for --
            # the trade simply doesn't work.
            if (
                zone.get("status") == "extended"
                and zone_rr is not None
                and zone_rr >= config.MIN_OPTION_RR
                and delta_ok
            ):
                state = "watch"
                logger.info(
                    "%s: WATCH -- option R/R %.2f now, %.2f at the %.2f-%.2f zone "
                    "(trigger %.2f)",
                    ticker, opt["option_rr"], zone_rr,
                    zone.get("zone_low"), zone.get("zone_high"), zone.get("zone_high"),
                )
            else:
                logger.warning(
                    "%s: dropping card -- option reward/risk %.2f below the %.1f "
                    "floor and no zone rescue (zone status=%s, zone R/R=%s)",
                    ticker, opt["option_rr"], config.MIN_OPTION_RR,
                    zone.get("status"), zone_rr,
                )
                continue

        cards.append(
            {
                "ticker": ticker,
                "bias": "calls",
                "state": state,
                "confidence": "High" if confidence == "high" else "Medium",
                "confluence_count": count,
                "confluence_signals": [str(s) for s in signals],
                "cautions": cautions,
                "thesis": str(c.get("thesis", "")).strip(),
                "contract": contract,
                # Ours, from the record -- see the entry gate above.
                "entry_reference": price,
                "entry_zone": zone,
                "zone_entry": zone_entry,
                "stop_level": stop,
                "price_target": target,
                "rr_ratio": rr,          # on the underlying: thesis quality
                "option_rr": opt["option_rr"],  # on the contract: trade quality
                "value_at_target": opt["value_at_target"],
                "value_at_stop": opt["value_at_stop"],
                "premium_at_risk": opt["premium_at_risk"],
                "iv_assessment": str(c.get("iv_assessment", "")).strip(),
            }
        )

    # Things you can act on today come first; plans come last.
    cards.sort(
        key=lambda x: (
            _STATE_RANK[x["state"]],
            _CONFIDENCE_RANK[x["confidence"].lower()],
            -x["confluence_count"],
        )
    )

    # A broad selloff can push half the universe out of reach at once. Keep the best
    # few plans and say what was dropped -- a silent cap reads as "that was all".
    watch = [c for c in cards if c["state"] == "watch"]
    if len(watch) > config.MAX_WATCH_CARDS:
        keep = set(id(c) for c in watch[: config.MAX_WATCH_CARDS])
        logger.info(
            "Capping WATCH cards at %s; dropping %s lower-ranked: %s",
            config.MAX_WATCH_CARDS,
            len(watch) - config.MAX_WATCH_CARDS,
            [c["ticker"] for c in watch[config.MAX_WATCH_CARDS :]],
        )
        cards = [c for c in cards if c["state"] != "watch" or id(c) in keep]

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
        self, system_prompt: str, user_content: str, model: str, payload: dict
    ) -> dict:
        """One call + parse. Raises on refusal, empty output, or unparseable JSON."""
        text = await self._call(system_prompt, user_content, model=model)
        if not text:
            raise ValueError("Empty response from Claude")
        parsed = _extract_json(text)
        if parsed is None:
            raise ValueError("Could not parse JSON from Claude response")
        summary, cards = _validate_cards(parsed, payload)
        logger.info(
            "Claude (%s) produced %s qualifying trade card(s)", model, len(cards)
        )
        return {"market_summary": summary, "trade_cards": cards, "error": None}

    async def _fallback_after_refusal(
        self, system_prompt: str, user_content: str, session: str, payload: dict
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
            return await self._analyze_once(system_prompt, user_content, fallback, payload)
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
                    system_prompt, user_content, config.CLAUDE_MODEL, payload
                )
            except ClaudeRefusal as refusal:
                # Retrying the same prompt on the same model would just refuse
                # again, so spend the attempt on the fallback model instead.
                logger.warning(
                    "Claude refused the request for session %s: %s", session, refusal
                )
                return await self._fallback_after_refusal(
                    system_prompt, user_content, session, payload
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
