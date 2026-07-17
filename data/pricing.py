"""
Black-Scholes option pricing.

Used for two things the scanner previously could not do:

  * DELTA on each chain candidate -- the payload carried no greeks at all, so the
    model was inventing a "delta_target" out of nothing.
  * The MODELED OPTION reward/risk of a trade card.

Why the option R/R matters: the card's `rr_ratio` measures the *underlying* --
target distance over stop distance. But you are not buying the stock. Your risk is
the premium, your reward is non-linear in the underlying, and theta is invisible to
a price-distance ratio. A 2.4 reward/risk on the stock can still be a losing call
simply because the move took five weeks instead of two. This module prices the
actual contract at the target and at the stop so the card can state what happens to
the money, not just to the chart.

Dividends are ignored (no dividend data in the payload); this biases call values
slightly low on payers, which is conservative for a call buyer.
"""

from __future__ import annotations

import math
from typing import Optional

import config


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(spot: float, strike: float, years: float, iv: float) -> tuple[float, float]:
    vol_t = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (config.RISK_FREE_RATE + 0.5 * iv * iv) * years) / vol_t
    return d1, d1 - vol_t


def _valid(spot: float, strike: float, years: float, iv: float) -> bool:
    return spot > 0 and strike > 0 and years > 0 and iv > 0


def call_delta(spot: float, strike: float, dte: int, iv: float) -> Optional[float]:
    """N(d1). The probability-weighted sensitivity to a $1 move in the underlying."""
    years = (dte or 0) / 365.0
    if not _valid(spot, strike, years, iv or 0.0):
        return None
    try:
        d1, _ = _d1_d2(spot, strike, years, iv)
    except (ValueError, ZeroDivisionError):
        return None
    return round(norm_cdf(d1), 3)


def call_price(spot: float, strike: float, dte_days: float, iv: float) -> Optional[float]:
    """
    Black-Scholes call value. At/after expiry this collapses to intrinsic value,
    which is the correct limit and keeps the caller from having to special-case it.
    """
    if spot is None or strike is None or iv is None:
        return None
    years = (dte_days or 0) / 365.0
    if years <= 0 or iv <= 0:
        return round(max(0.0, (spot or 0.0) - strike), 2)
    if spot <= 0 or strike <= 0:
        return None
    try:
        d1, d2 = _d1_d2(spot, strike, years, iv)
    except (ValueError, ZeroDivisionError):
        return None
    value = spot * norm_cdf(d1) - strike * math.exp(
        -config.RISK_FREE_RATE * years
    ) * norm_cdf(d2)
    return round(max(0.0, value), 2)


def reprice_at_entry(
    *,
    spot_now: float,
    entry_spot: float,
    strike: float,
    dte: int,
    iv: float,
    ask_now: float,
) -> Optional[float]:
    """
    What this contract would cost if the underlying were at `entry_spot` instead of
    `spot_now` -- calibrated so the model reproduces the REAL ask at today's price.

    Why not just call call_price(entry_spot, ...): that returns a MODEL price, and
    comparing it to the quoted `ask` compares a model to a market. The gap between
    them is the bid/ask spread plus the volatility smile, and it is not small. A raw
    Black-Scholes "premium at the zone" set against a real ask would show a premium
    improvement that is partly just model error -- and that error would land directly
    in the reward/risk that decides whether a card ships.

    So price the DIFFERENCE and keep the level honest: take the residual
    (ask_now - model_now) as the market's standing correction for this contract and
    carry it, unchanged, to the zone. Crude -- the smile is not constant in spot --
    but it makes the two premiums commensurable, which is the entire requirement.
    Exact at the anchor: entry_spot == spot_now returns ask_now.

    Time is deliberately NOT advanced. A real pullback takes days, so you would
    actually buy a cheaper option with less life left; holding dte fixed OVERSTATES
    the zone premium, which lowers reward and raises risk. That is conservative in
    the direction that matters -- it cannot manufacture a passing card.
    """
    if any(v is None for v in (spot_now, entry_spot, strike, dte, iv, ask_now)):
        return None
    if ask_now <= 0 or iv <= 0 or dte <= 0 or spot_now <= 0 or entry_spot <= 0:
        return None

    model_now = call_price(spot_now, strike, dte, iv)
    model_at_entry = call_price(entry_spot, strike, dte, iv)
    if model_now is None or model_at_entry is None:
        return None

    residual = ask_now - model_now
    # A contract cannot be worth less than nothing, and a premium rounded to zero
    # would divide the reward/risk by ~0 and print a spectacular ratio.
    return round(max(0.01, model_at_entry + residual), 2)


def option_reward_risk(
    *,
    strike: float,
    ask: float,
    iv: float,
    dte: int,
    price_target: float,
    stop_level: float,
    entry_premium: Optional[float] = None,
) -> Optional[dict]:
    """
    Model what the CONTRACT is worth if the thesis plays out, and if it fails.

    The move is assumed to complete partway through the contract's life
    (config.OPTION_RR_TIME_FRACTION), not on the expiration date -- a swing thesis
    that needs every last day to work is not the trade you thought you were taking,
    and pricing at expiry would ignore the time value you still hold at the target.

    Risk is NOT the whole premium: you exit at the stop, where the option still has
    value. Reward is the modeled value at the target minus what you paid.

    Assumes IV is unchanged at both levels. Real IV usually FALLS after a breakout,
    so the reward side is, if anything, flattered -- state that, don't hide it.
    Returns None when the inputs can't support a model.

    `entry_premium` overrides what you PAY (and therefore what you risk) without
    touching the target/stop valuations: it models the same contract bought at a
    different entry -- see reprice_at_entry. Defaults to `ask`, which is the
    buy-it-now case and leaves every existing caller unchanged.
    """
    if not all(v is not None for v in (strike, ask, iv, dte, price_target, stop_level)):
        return None
    if ask <= 0 or iv <= 0 or dte <= 0:
        return None

    paid = ask if entry_premium is None else entry_premium
    if paid <= 0:
        return None

    remaining = dte * (1.0 - config.OPTION_RR_TIME_FRACTION)
    at_target = call_price(price_target, strike, remaining, iv)
    at_stop = call_price(stop_level, strike, remaining, iv)
    if at_target is None or at_stop is None:
        return None

    reward = at_target - paid
    risk = paid - at_stop
    if risk <= 0.01:
        # The model says the option is worth ~what you paid even at the stop. That is
        # not a real risk estimate (deep ITM, or a stop that isn't a stop) -- refuse
        # to publish a reward/risk that would look spectacular for the wrong reason.
        return None
    if reward <= 0:
        return {
            "option_rr": 0.0,
            "value_at_target": at_target,
            "value_at_stop": at_stop,
            "premium_at_risk": round(risk, 2),
        }
    return {
        "option_rr": round(reward / risk, 2),
        "value_at_target": at_target,
        "value_at_stop": at_stop,
        "premium_at_risk": round(risk, 2),
    }
