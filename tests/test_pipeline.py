"""
The full decision pipeline:

    session -> regime -> setup -> RR -> score -> protection -> size -> order

setups.py and scoring.py ship fail-closed, so the first test here asserts the
bot genuinely cannot trade as delivered. The rest install a synthetic setup and
score to exercise everything downstream — which doubles as a worked example of
how a real setup drops in once its rules are defined.
"""
from datetime import datetime

import pytest

import scoring
import setups
import fake_mt5
from conftest import (H1, IN_SESSION_UTC, M5, M15,
                      OUT_OF_SESSION_UTC, read_log)
from setups import SetupCandidate

NOW = datetime.fromisoformat(IN_SESSION_UTC)
OUT = datetime.fromisoformat(OUT_OF_SESSION_UTC)


def install_setup(monkeypatch, *, direction="buy", entry=None, stop=None,
                  target=None, setup=setups.TREND_PULLBACK, score=90.0):
    """Give the pipeline a setup and a score so it can reach an order."""
    def detector(m15, m5, regime, cfg):
        px = float(m15["close"][-1]) if entry is None else entry
        sl = px - 0.0020 if stop is None else stop
        tp = px + 0.0050 if target is None else target
        return SetupCandidate(setup, direction, px, sl, tp, sl, "synthetic"), "ok"

    monkeypatch.setitem(setups._DETECTORS, setup, detector)
    if score is not None:
        monkeypatch.setattr(scoring, "score", lambda c, r, ctx: (score, "synthetic score"))
    return detector


def evaluate(lathe, conn, symbol="EURUSD", now=NOW, session="2026-01-14"):
    return lathe.evaluate(symbol, session, conn.account_info(), now)


# --- as shipped -------------------------------------------------------------
def test_ships_fail_closed_and_cannot_trade(lathe, conn, trending_market, vault):
    """No setup has rules, so nothing can be opened. This is the delivered state."""
    evaluate(lathe, conn)
    assert trending_market.orders == []
    log = read_log(vault)
    assert "NO-TRADE" in log
    assert "no rules defined" in log


def test_no_setups_are_configured():
    assert setups.configured_setups() == ()
    assert setups.any_configured() is False


def test_score_refuses_without_a_formula():
    value, reason = scoring.score(None, None, {})
    assert value is None
    assert not scoring.passes(value)
    assert "not defined" in reason


# --- with a synthetic setup installed ---------------------------------------
def test_opens_a_trade_when_every_gate_passes(monkeypatch, lathe, conn,
                                              trending_market, vault):
    install_setup(monkeypatch)
    evaluate(lathe, conn)

    opens = [o for o in trending_market.orders if "position" not in o]
    assert len(opens) == 1
    order = opens[0]
    assert order["sl"] > 0 and order["tp"] > 0
    log = read_log(vault)
    assert "OPENED EURUSD" in log
    assert "rr=" in log and "risk_pct=" in log and "ticket=" in log


def test_one_decision_per_setup_bar(monkeypatch, lathe, conn, trending_market):
    """Repeated polls inside one M15 bar must not re-decide."""
    install_setup(monkeypatch)
    for _ in range(10):
        evaluate(lathe, conn)
    assert len([o for o in trending_market.orders if "position" not in o]) == 1


def test_bar_gating_holds_even_when_no_trade_is_taken(monkeypatch, lathe, conn,
                                                      trending_market, vault):
    """
    Ten polls inside one M15 bar must produce exactly one decision record.

    This uses a setup that gets rejected on RR, so no position exists — the
    concurrent-trade cap therefore cannot be what limits repeat evaluations,
    and only the bar cursor can.
    """
    px = float(trending_market.closed_prices[-1])
    install_setup(monkeypatch, entry=px, stop=px - 0.0020, target=px + 0.0020)
    for _ in range(10):
        evaluate(lathe, conn)
    assert read_log(vault).count("REJECTED") == 1
    assert trending_market.orders == []


def test_a_new_setup_bar_is_evaluated_again(monkeypatch, lathe, conn,
                                            trending_market, vault):
    px = float(trending_market.closed_prices[-1])
    install_setup(monkeypatch, entry=px, stop=px - 0.0020, target=px + 0.0020)
    evaluate(lathe, conn)

    # Close one more bar: append a price AND advance the clock, which is what
    # happens in a real terminal. Advancing only the series would leave the
    # newest bar pinned to the same timestamp.
    for tf in (H1, M15, M5):
        closed, _ = trending_market.series[tf]
        trending_market.set_series(tf, list(closed) + [px + 0.0005], px + 0.0006)
    trending_market.now_ts += fake_mt5.BAR_SECONDS

    evaluate(lathe, conn)
    assert read_log(vault).count("REJECTED") == 2


def test_outside_the_session_nothing_is_evaluated(monkeypatch, lathe, conn,
                                                  trending_market, vault):
    install_setup(monkeypatch)
    lathe.evaluate("EURUSD", None, conn.account_info(), OUT)
    assert trending_market.orders == []
    assert "outside the trading session" in read_log(vault)


def test_undefined_regime_blocks_the_trade(monkeypatch, lathe, conn, market, vault):
    from conftest import feed_all_timeframes
    feed_all_timeframes(market, [1.1000 + (i % 3) * 0.0001 for i in range(160)])
    install_setup(monkeypatch)
    # Force the dead band: nothing is trending enough, nothing quiet enough.
    lathe.regime_cfg.adx_trend_min = 99.0
    lathe.regime_cfg.adx_range_max = 0.5
    evaluate(lathe, conn)
    assert market.orders == []
    log = read_log(vault)
    # Must be blocked BY THE REGIME CHECK, naming the dead band — not merely
    # blocked later by "no setup maps to this regime", which would still say
    # NO-TRADE while the regime gate itself was gone.
    assert "neither trending nor ranging" in log


def test_wide_spread_blocks_the_trade(monkeypatch, lathe, conn, trending_market, vault):
    install_setup(monkeypatch)
    trending_market.spread = 0.0010          # 10 pips, limit is 2
    evaluate(lathe, conn)
    assert trending_market.orders == []
    assert "spread" in read_log(vault)


def test_rr_below_minimum_is_rejected(monkeypatch, lathe, conn, trending_market, vault):
    px = float(trending_market.closed_prices[-1])
    # risk 0.0020, reward 0.0020 -> RR 1.0, below the 2.0 a trend setup needs
    install_setup(monkeypatch, entry=px, stop=px - 0.0020, target=px + 0.0020)
    evaluate(lathe, conn)
    assert trending_market.orders == []
    log = read_log(vault)
    assert "REJECTED" in log and "RR 1.00 below minimum 2.0" in log


def test_score_below_threshold_is_rejected(monkeypatch, lathe, conn,
                                           trending_market, vault):
    install_setup(monkeypatch, score=79.0)
    evaluate(lathe, conn)
    assert trending_market.orders == []
    assert "signal score 79 < 80" in read_log(vault)


def test_score_at_the_threshold_is_accepted(monkeypatch, lathe, conn, trending_market):
    install_setup(monkeypatch, score=80.0)
    evaluate(lathe, conn)
    assert len([o for o in trending_market.orders if "position" not in o]) == 1


def test_unscoreable_trade_is_rejected(monkeypatch, lathe, conn, trending_market, vault):
    """A None score must reject, never be treated as passing."""
    install_setup(monkeypatch, score=None)
    monkeypatch.setattr(scoring, "score", lambda c, r, ctx: (None, "no formula"))
    evaluate(lathe, conn)
    assert trending_market.orders == []
    assert "score n/a" in read_log(vault)


def test_one_concurrent_trade(monkeypatch, lathe, conn, trending_market, vault):
    install_setup(monkeypatch)
    evaluate(lathe, conn)
    assert len(trending_market.positions) == 1
    lathe.bar_cursor.clear()                 # pretend a new setup bar closed
    evaluate(lathe, conn, symbol="USDJPY")
    assert "concurrent" in read_log(vault)
    assert len(trending_market.positions) == 1


def test_session_trade_limit(monkeypatch, lathe, conn, trending_market, vault):
    install_setup(monkeypatch)
    for _ in range(3):
        lathe.protection.record_trade_opened("2026-01-14")
    evaluate(lathe, conn)
    assert trending_market.orders == []
    assert "session trade limit reached (3/3)" in read_log(vault)


def test_position_is_sized_from_risk_not_guessed(monkeypatch, lathe, conn,
                                                 trending_market):
    px = float(trending_market.closed_prices[-1])
    install_setup(monkeypatch, entry=px, stop=px - 0.0020, target=px + 0.0050)
    evaluate(lathe, conn)
    order = [o for o in trending_market.orders if "position" not in o][0]
    # 1% of 10,000 = $100 over a 20-pip stop at $10/pip/lot -> 0.50 lots
    assert order["volume"] == pytest.approx(0.50)


def test_trade_metadata_is_recorded_for_management(monkeypatch, lathe, conn,
                                                   trending_market):
    install_setup(monkeypatch)
    evaluate(lathe, conn)
    ticket = str(trending_market.positions[0].ticket)
    assert ticket in lathe.trade_meta
    meta = lathe.trade_meta[ticket]
    assert meta["symbol"] == "EURUSD"
    assert meta["session_key"] == "2026-01-14"
    assert meta["original_stop"] > 0


def test_foreign_positions_are_invisible_to_the_pipeline(monkeypatch, lathe, conn,
                                                         trending_market):
    """A manual trade must not consume the single concurrent-trade slot."""
    trending_market.add_foreign_position(symbol="EURUSD", ticket=777, magic=0)
    install_setup(monkeypatch)
    evaluate(lathe, conn)
    assert len([o for o in trending_market.orders if "position" not in o]) == 1
    assert any(p.ticket == 777 for p in trending_market.positions)


def test_errors_on_one_symbol_do_not_stop_the_other(monkeypatch, lathe, conn,
                                                    trending_market, vault):
    install_setup(monkeypatch)

    original = lathe.conn.get_candles
    def explode(symbol, tf, count=200):
        if symbol == "EURUSD":
            raise RuntimeError("boom")
        return original(symbol, tf, count)
    monkeypatch.setattr(lathe.conn, "get_candles", explode)

    with pytest.raises(RuntimeError):
        evaluate(lathe, conn, symbol="EURUSD")
    evaluate(lathe, conn, symbol="USDJPY")
    assert len([o for o in trending_market.orders if "position" not in o]) == 1
