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
# A watch card is a PLAN with a trigger, never an entry -- see _process_card.
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


# Setup-archetype registry. Every scanned ticker is scored against the CORE rubric
# and, in parallel, against each ARCHETYPE rubric (see prompt_builder). An archetype
# is a self-contained block: the model scores it and emits its own card array, and
# this registry says which array to read, its qualifying threshold, and its watch cap.
# The per-card machinery in _process_card is shared, so adding an archetype
# (flat-base, pullback-continuation, ...) is a new entry here plus a rubric block in
# prompt_builder -- no rearchitecting. The "breakout" flag toggles the one archetype-
# specific hard gate (_breakout_gate: break state + volume confirmation).
_ARCHETYPES = {
    "core": {
        "cards_key": "trade_cards",
        "min_confluence": config.MIN_CONFLUENCE_COUNT,
        "max_watch": config.MAX_WATCH_CARDS,
        "breakout": False,
    },
    "breakout": {
        "cards_key": "breakout_cards",
        "min_confluence": config.MIN_BREAKOUT_CONFLUENCE_COUNT,
        "max_watch": config.MAX_BREAKOUT_WATCH_CARDS,
        "breakout": True,
    },
}


def _breakout_fields(ticker: str, c: dict, record: dict) -> Optional[dict]:
    """
    Validate a breakout card's archetype-only fields and enforce the break-state
    volume gate. Returns {break_state, breakout_level} to merge into the card, or
    None to DROP it.

    Volume expansion on the break is the single most important breakout signal, so it
    is a HARD requirement for the "breaking" and "extended" states -- a break there on
    flat/light volume does not qualify. "approaching" is exempt: by definition the
    surge has not happened yet. rel_volume missing counts as unconfirmed, not as a
    pass -- an unverifiable break is not a confirmed one.
    """
    state = str(c.get("break_state", "")).strip().lower()
    if state not in config.BREAKOUT_STATES:
        logger.warning(
            "%s: dropping breakout card -- invalid break_state %r",
            ticker, c.get("break_state"),
        )
        return None
    if state in ("breaking", "extended"):
        rel_vol = (record.get("daily") or {}).get("rel_volume")
        if rel_vol is None or rel_vol < config.BREAKOUT_MIN_RVOL_ON_BREAK:
            logger.warning(
                "%s: dropping breakout card -- state '%s' requires rel_volume >= %.2f "
                "(volume confirmation) but got %s",
                ticker, state, config.BREAKOUT_MIN_RVOL_ON_BREAK, rel_vol,
            )
            return None
    return {"break_state": state, "breakout_level": _coerce_number(c.get("breakout_level"))}


def _process_card(
    c: dict, spec: dict, index: dict, records: dict
) -> Optional[dict]:
    """
    Validate, normalize and gate ONE model card for a given archetype `spec`.

    Everything here is archetype-agnostic except two touch points: the qualifying
    `min_confluence` threshold, and (for breakout) the break-state/volume gate via
    _breakout_fields. The contract-in-chain, breakeven, downtrend-veto, earnings,
    underlying-R/R and option-R/R gates apply to every archetype unchanged -- a
    breakout call is still a call you have to be able to buy at an acceptable price.
    Returns the finished card dict, or None to drop it.
    """
    if not isinstance(c, dict):
        return None
    confidence = str(c.get("confidence", "")).strip().lower()
    if confidence not in _CONFIDENCE_RANK:
        return None  # drop Low / unknown tiers
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
    if count < spec["min_confluence"]:
        return None

    ticker = str(c.get("ticker", "")).upper()
    # The prompt states several hard rules. Prose is not enforcement -- one
    # instruction-following lapse ships a bad trade, so re-check them here against
    # the same payload the model was given.
    record = records.get(ticker, {})

    # Archetype-specific gate (breakout: break state + volume confirmation). Kept
    # ahead of the shared gates so a mis-stated break state is dropped cheaply.
    extra: dict = {}
    if spec["breakout"]:
        bo = _breakout_fields(ticker, c, record)
        if bo is None:
            return None
        extra.update(bo)

    contract = _resolve_contract(ticker, c.get("contract") or {}, index)
    if contract is None:
        # The model invented a contract that isn't in the live chain. Publishing
        # it would put an unbuyable strike in front of a trader -- drop the card.
        logger.warning(
            "%s: dropping card -- contract %s not found in the live chain",
            ticker,
            c.get("contract"),
        )
        return None

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
        return None

    # 1. Structure veto: a daily downtrend is distribution, whatever else fired.
    #    Applied to breakouts too -- a genuine break clears the last pivot high, so
    #    structure reads uptrend/range, not downtrend; a "breakout" still tagged a
    #    downtrend is one breaking down, and the veto correctly kills it.
    trend = ((record.get("structure") or {}).get("daily") or {}).get("trend")
    if trend == "downtrend":
        logger.warning(
            "%s: dropping card -- daily structure is a downtrend (veto)", ticker
        )
        return None

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
        return None

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
        return None

    # Recompute rather than trust. The gate ran on the model's STATED ratio, so
    # an arithmetic slip shipped silently past the floor it was supposed to hit.
    rr = round((target - price) / (price - stop), 2) if target is not None else None
    if rr is None:
        logger.warning("%s: dropping card -- no price target to measure", ticker)
        return None
    stated = _coerce_number(c.get("rr_ratio"))
    if stated is not None and rr > 0 and abs(stated - rr) / rr > config.RR_DIVERGENCE_TOLERANCE:
        # The numbers it reasoned from are not the numbers it reported. Whatever
        # the thesis says, it was not derived from this trade.
        logger.warning(
            "%s: dropping card -- stated R/R %.2f but entry/stop/target give "
            "%.2f (>%.0f%% divergence)",
            ticker, stated, rr, config.RR_DIVERGENCE_TOLERANCE * 100,
        )
        return None
    if rr < config.MIN_RR_RATIO:
        logger.warning(
            "%s: dropping card -- underlying reward/risk %.2f below the %.1f floor",
            ticker, rr, config.MIN_RR_RATIO,
        )
        return None

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
        return None

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
            return None

    card = {
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
    card.update(extra)  # archetype-specific fields (breakout: break_state, level)
    return card


def _finalize(cards: list[dict], max_watch: int, label: str) -> list[dict]:
    """Sort one archetype's cards (actionable first) and cap its WATCH plans."""
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
    if len(watch) > max_watch:
        keep = set(id(c) for c in watch[:max_watch])
        logger.info(
            "Capping %s WATCH cards at %s; dropping %s lower-ranked: %s",
            label,
            max_watch,
            len(watch) - max_watch,
            [c["ticker"] for c in watch[max_watch:]],
        )
        cards = [c for c in cards if c["state"] != "watch" or id(c) in keep]

    return cards


def _validate_archetype(
    spec: dict, label: str, parsed: dict, index: dict, records: dict
) -> list[dict]:
    """Filter, normalize, sort and cap one archetype's cards from the model output."""
    raw_cards = parsed.get(spec["cards_key"], [])
    if not isinstance(raw_cards, list):
        return []
    cards = [
        card
        for c in raw_cards
        if (card := _process_card(c, spec, index, records)) is not None
    ]
    return _finalize(cards, spec["max_watch"], label)


def _validate_output(parsed: dict, payload: dict) -> tuple[str, list[dict], list[dict]]:
    """Return (market_summary, core trade cards, breakout cards) from parsed output."""
    summary = str(parsed.get("market_summary", "")).strip()
    index = _candidate_index(payload)
    records = {
        str(t.get("symbol", "")).upper(): t for t in (payload.get("tickers") or [])
    }
    core = _validate_archetype(_ARCHETYPES["core"], "core", parsed, index, records)
    breakout = _validate_archetype(
        _ARCHETYPES["breakout"], "breakout", parsed, index, records
    )
    return summary, core, breakout


class ClaudeEngine:
    def __init__(self):
        self._client = AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=config.CLAUDE_TIMEOUT_SECONDS,
            # This class runs its OWN retry policy (analyze() retries once, and a
            # refusal falls back to a second model). The SDK's default max_retries=2
            # stacks ON TOP of that: a call whose generation genuinely exceeds the
            # timeout was retried 3x internally before surfacing, turning a single
            # slow scan into a ~9-minute stall per attempt. Let our loop be the sole
            # retry authority so a timeout surfaces promptly and is handled once.
            max_retries=0,
        )

    @staticmethod
    def _log_usage(model: str, usage: Any) -> None:
        """Log tokens and an estimated USD cost for one call, so overruns surface in
        the scan logs immediately rather than days later on the billing dashboard.
        The cost is an estimate from config.MODEL_PRICING (list pricing), not billing."""
        if usage is None:
            return
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        prices = getattr(config, "MODEL_PRICING", {}).get(model)
        if prices:
            in_price, out_price = prices
            cost = (in_tok * in_price + out_tok * out_price) / 1_000_000
            cost_str = f"~${cost:.3f}"
        else:
            cost_str = "cost n/a (no pricing for model)"
        logger.info(
            "Claude usage (%s): input=%s output=%s cache_read=%s cache_write=%s -> %s",
            model, in_tok, out_tok, cache_read, cache_write, cost_str,
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
        # Opus 4.8 runs WITHOUT thinking unless we ask for it explicitly (unlike
        # fable-5, where thinking is always on). With thinking off, opus tends to leak
        # reasoning into the visible response, which corrupts the strict-JSON output
        # contract this scanner parses. So request adaptive thinking, then bound its
        # depth via output_config.effort so reasoning doesn't consume the whole token
        # budget and starve the JSON. Effort omitted when CLAUDE_EFFORT is blank.
        kwargs["thinking"] = {"type": "adaptive"}
        if getattr(config, "CLAUDE_EFFORT", ""):
            kwargs["output_config"] = {"effort": config.CLAUDE_EFFORT}
        message = await self._client.messages.create(**kwargs)
        self._log_usage(model, getattr(message, "usage", None))

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
        summary, cards, breakout_cards = _validate_output(parsed, payload)
        logger.info(
            "Claude (%s) produced %s core trade card(s) and %s breakout card(s)",
            model, len(cards), len(breakout_cards),
        )
        return {
            "market_summary": summary,
            "trade_cards": cards,
            "breakout_cards": breakout_cards,
            "error": None,
        }

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
        return {"market_summary": "", "trade_cards": [], "breakout_cards": [], "error": message}

    async def analyze(self, payload: dict) -> dict:
        """
        Run the confluence analysis. Returns:
          {"market_summary": str, "trade_cards": [...], "breakout_cards": [...],
           "error": Optional[str]}
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
                        "breakout_cards": [],
                        "error": str(exc),
                    }
        # Unreachable, but keeps type-checkers content.
        return {"market_summary": "", "trade_cards": [], "breakout_cards": [], "error": "unknown"}
