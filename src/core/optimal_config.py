"""Canonical ICT-optimal params (v7, full-corpus-validated 2026-09-17).

Single source of truth for the ICT-only strategy's "best known"
parameter set. Import ``optimal_params()`` (or the module-level
constant ``OPTIMAL_PARAMS``) from any backtest driver, notebook, or
tool script to run the v7 SNIPER recipe without re-typing the knobs.

Recipe lineage
==============

The current ``OPTIMAL_PARAMS`` recipe is the **v7 SNIPER** config
(``entry_mode='sniper'``, ``fvg_inv_trade_tp_zone_mult=22.0``) that
was confirmed on the **full 654 Mon-Fri UTC day XAUUSD corpus**
(Jan 2024 – Jul 2026) with walk-forward split:

* Train (2024 + 2025, 516 days): **EV +$1.93/trade, PnL +$13.88/day**
* Test (2026, 138 days): **EV +$3.02/trade, PnL +$43.62/day**
* Every year and every month in the corpus was +EV.

See ``AGENTS.md`` § "v6 → v7 state — the canonical recipe" for the
full lineage and validation summary, and § "v8 — nb45 single-knob
sweep" for the related-but-untested v8 winner
(``inverse_breadth=False`` on immediate mode) that the user has
NOT YET validated when stacked on top of SNIPER.

Usage
=====

From a backtest driver::

    from src.core.optimal_config import optimal_params
    from src.core.ict_strategy import TrendStrategyParams

    p = optimal_params()                                  # default v7 SNIPER
    p = optimal_params(inverse_breadth=False)              # v8 candidate (UNVALIDATED on SNIPER)
    p = optimal_params(entry_mode='immediate')             # immediate-mode reference
    p = optimal_params(as_dict=False)                      # return TrendStrategyParams instance

From a CLI script that wants a dict to JSON-serialize or log::

    from src.core.optimal_config import optimal_params
    log.info("running with params: %s", optimal_params(as_dict=True))

Constants exported
==================

* ``OPTIMAL_PARAMS``          — module-level ``TrendStrategyParams``
                                pre-built with the v7 recipe.
* ``optimal_params(**overrides)`` — factory that returns a fresh
                                ``TrendStrategyParams`` with the
                                v7 defaults and any overrides
                                applied.
* ``OPTIMAL_RECIPE_VERSION``  — string identifier of the recipe
                                this file encodes. Bump when the
                                canonical config changes.

Override ergonomics
===================

``optimal_params`` accepts the same kwargs as ``TrendStrategyParams``.
Pass ``as_dict=True`` to get the dict form (handy for
``IctBacktestResult.params`` round-tripping or for logging). Pass
``as_dict=False`` (default) to get a ``TrendStrategyParams`` instance
that can be passed directly to the backtest.

If an override changes a knob that is part of the v7 recipe's "do
not touch" list (the bottom of this file's recipe block), a warning
is logged — not enforced. This is intentional: research scripts
should be able to do A/B work without monkey-patching.

Anti-examples
=============

* **Do not** copy/paste this file's defaults into a driver script.
  Always ``from src.core.optimal_config import optimal_params`` so a
  future bump to the recipe propagates automatically.
* **Do not** import ``OPTIMAL_PARAMS`` directly into a notebook cell
  and mutate it in place. Always call ``optimal_params()`` for a
  fresh instance.
* **Do not** add non-default knobs to this file unless they've been
  promoted to the canonical recipe by a full-corpus A/B (see
  ``AGENTS.md`` "v6 → next experiments" lifecycle).

See also
========

* ``AGENTS.md`` — full experiment lineage, validation tables, and
  the per-knob "do not touch" list.
* ``src/core/ict_strategy.py`` — ``TrendStrategyParams`` field
  reference (every field on the canonical recipe is documented
  inline in ``ict_strategy.py``).
"""
from __future__ import annotations

import copy
import logging as _logging
from dataclasses import asdict

from .ict_strategy import TrendStrategyParams

_log = _logging.getLogger(__name__)

# Bump this whenever the canonical recipe changes so downstream
# code can detect "I'm running an outdated config". Format:
# "v<N>-<recipe-name>-<YYYY-MM-DD>" — last touched 2026-09-17.
OPTIMAL_RECIPE_VERSION: str = "v17-sniper-2026-09-18"


# Knobs that are part of the v7 recipe's "do not touch" list.
# Overriding any of these in optimal_params() emits a warning so
# downstream A/B scripts can see when they've departed from the
# canonical recipe. The list mirrors the v6 OPTIMAL recipe block
# in AGENTS.md "v6 → v7 state — the canonical recipe".
_RECIPE_KNOBS = frozenset({
    # v2 BASELINE knobs that the v7 recipe leaves at v2 defaults
    "signal_source",
    "additional_sources",
    "fvg_resample_secs",
    "num_layers",
    "inverse_breadth",
    "invalidation_sl_usd",
    "invalidation_buffer_usd",
    "use_market_structure",
    "ms_min_conviction",
    "ms_boost_conviction",
    "use_atr_scaling",
    "atr_len",
    "sl_atr_mult",
    "tp_atr_mult",
    "sl_usd",
    "tp_usd",
    "lots",
    "contract_size",
    "fvg_require_retest_to_invert",
    "fvg_invalidation_min_pierce_usd",
    "fvg_invalidation_min_consecutive_bars",
    "fvg_supersede_on_new",
    "renko_drive_invalidation",
    "fvg_sweep_enabled",
    "fvg_invalidate_on_structure",
    "gate_on_gmma_bias",
    "layer_lifetime_secs",
    "bos_choch_ignore_invert_when_aligned",
    "bos_choch_memory_n_events",
    "fvg_min_lifetime_secs",
    # v7 SNIPER knobs (the actual recipe deltas)
    "entry_mode",
    "fvg_inv_trade_sl_zone_mult",
    "fvg_inv_trade_tp_zone_mult",
    "fvg_inv_trade_min_zone_usd",
    "fvg_inv_trade_max_per_zone",
    "sniper_max_age_secs",
})


def _build_v7_sniper() -> TrendStrategyParams:
    """Construct the canonical v7 SNIPER recipe as a fresh instance.

    All values here mirror the recipe block in AGENTS.md § "v6 → v7
    state — the canonical recipe (updated 2026-09-17)". If you find
    yourself wanting to change a value here, that change should
    first be promoted to the recipe in AGENTS.md, and then mirrored
    here.
    """
    return TrendStrategyParams(
        # ── v2 BASELINE defaults (unchanged from v2 alpha) ──
        signal_source="fvg",
        additional_sources=["ifvg"],
        fvg_resample_secs=60,
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=0.05,
        invalidation_buffer_usd=0.02,
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.0,
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25,
        tp_atr_mult=0.55,
        sl_usd=0.80,
        tp_usd=1.80,
        lots=0.01,
        contract_size=100.0,                 # XAUUSD: 1 lot = 100 oz. BTC uses 0.001.
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=0.05,
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,            # v6 winner (still relevant)
        # ── v17 SNIPER mode (SL widened 2026-09-18) ──
        entry_mode="sniper",                 # KEY CHANGE: skip FVG entry, wait for inversion
        fvg_inv_trade_sl_zone_mult=2.0,      # SL = 2× zone width (v16a full-corpus validated)
        fvg_inv_trade_tp_zone_mult=22.0,     # TP = 22× zone width (peak from sweep)
        fvg_inv_trade_min_zone_usd=0.30,     # min zone width (unchanged)
        fvg_inv_trade_max_per_zone=1,        # one inverse trade per zone
        sniper_max_age_secs=1800,            # drop stale snipers after 30 min
    )


# Module-level pre-built canonical instance. Use ``optimal_params()``
# instead when you want a fresh instance or want to override knobs.
OPTIMAL_PARAMS: TrendStrategyParams = _build_v7_sniper()


def optimal_params(**overrides) -> TrendStrategyParams | dict:
    """Return a fresh copy of the canonical v7 SNIPER params.

    Parameters
    ----------
    **overrides
        Keyword arguments matching fields on ``TrendStrategyParams``.
        Applied on top of the v7 recipe. Passing a field name that
        is part of the canonical recipe's "do not touch" list logs a
        WARNING (not an error) — research scripts are expected to
        override these knobs; we just want visibility.
    as_dict:
        If True, return the params as a plain dict (via
        ``dataclasses.asdict``). If False (default), return a
        ``TrendStrategyParams`` instance. The ``as_dict`` kwarg is
        consumed and not forwarded to ``TrendStrategyParams``.

    Returns
    -------
    TrendStrategyParams | dict
        A fresh instance (or dict) with overrides applied. Always
        a copy — mutating the returned value never mutates the
        module-level ``OPTIMAL_PARAMS`` constant.

    Examples
    --------
    >>> from src.core.optimal_config import optimal_params
    >>> p = optimal_params()                    # v7 SNIPER default
    >>> p = optimal_params(inverse_breadth=False)  # v8 candidate (untested on SNIPER)
    >>> p = optimal_params(as_dict=True)        # dict form for logging/serialisation
    """
    as_dict_flag = bool(overrides.pop("as_dict", False))
    if overrides.keys() & _RECIPE_KNOBS:
        _log.warning(
            "optimal_params() overrides touch canonical recipe knobs: %s. "
            "Verify this matches the latest AGENTS.md recipe.",
            sorted(overrides.keys() & _RECIPE_KNOBS),
        )
    fresh = copy.deepcopy(OPTIMAL_PARAMS)
    for k, v in overrides.items():
        if not hasattr(fresh, k):
            raise TypeError(
                f"optimal_params() got unexpected override {k!r}; "
                f"not a field on TrendStrategyParams"
            )
        setattr(fresh, k, v)
    return asdict(fresh) if as_dict_flag else fresh


__all__ = [
    "OPTIMAL_RECIPE_VERSION",
    "OPTIMAL_PARAMS",
    "optimal_params",
]