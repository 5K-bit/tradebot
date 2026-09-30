"""
preflight.py — check a real MT5 terminal before letting the bot near it.

Run this on the Windows machine where MT5 is installed, with the terminal
running and logged into your account:

    python3 preflight.py

It sends NO orders. Order validity is probed with MT5's order_check(), which
asks the broker whether a request would be accepted without executing it.

It answers the questions that differ from broker to broker and that a test
suite against a fake terminal cannot:

  - is this account demo or real?
  - what are your symbols actually called? (EURUSD, EURUSD.raw, EURUSDm, ...)
  - which order filling modes does the broker accept?
  - does it accept pending stop orders with an expiry?
  - what is the server's UTC offset, for the daily reset?
  - what do spreads actually look like right now?
  - is there enough history for the indicators?

It finishes by printing the config values to set.
"""
import sys
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 is not installed. On Windows:  pip install MetaTrader5")
    print("The package is Windows-only and needs the MT5 terminal on this machine.")
    sys.exit(1)

import os

WANTED = ["EURUSD", "USDJPY"]

OK, WARN, BAD = "  [ok]  ", "  [warn]", "  [BAD] "
findings: list = []


def note(level, message):
    findings.append((level, message))
    print(f"{level} {message}")


def header(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def connect():
    header("1. CONNECTION")
    kwargs = {}
    if os.environ.get("MT5_PATH"):
        kwargs["path"] = os.environ["MT5_PATH"]
    if not mt5.initialize(**kwargs):
        note(BAD, f"initialize() failed: {mt5.last_error()}")
        print("\n  Is the MT5 terminal running and logged in on this machine?")
        return False

    login = os.environ.get("MT5_LOGIN")
    if login:
        ok = mt5.login(int(login), password=os.environ.get("MT5_PASSWORD", ""),
                       server=os.environ.get("MT5_SERVER", ""))
        if not ok:
            note(BAD, f"login() failed: {mt5.last_error()}")
            return False
        note(OK, f"logged in as {login}")
    else:
        note(WARN, "MT5_LOGIN not set — using whatever account the terminal has open")

    term = mt5.terminal_info()
    acct = mt5.account_info()
    if acct is None:
        note(BAD, f"account_info() returned None: {mt5.last_error()}")
        return False

    modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
    kind = modes.get(acct.trade_mode, f"unknown({acct.trade_mode})")
    if kind == "REAL":
        note(WARN, f"this is a REAL-MONEY account ({acct.login}). For practice, "
                   f"log the terminal into a demo account instead.")
    else:
        note(OK, f"{kind} account {acct.login} — {acct.balance} {acct.currency}")

    if term is not None:
        if not getattr(term, "trade_allowed", True):
            note(BAD, "algorithmic trading is DISABLED in the terminal. "
                      "Enable the 'Algo Trading' button, or "
                      "Tools > Options > Expert Advisors > Allow algorithmic trading.")
        else:
            note(OK, "algorithmic trading is enabled in the terminal")
    if not getattr(acct, "trade_allowed", True):
        note(BAD, "this account does not allow trading (investor password?)")
    if not getattr(acct, "trade_expert", True):
        note(BAD, "expert/automated trading is disabled for this account")
    return True


def resolve_symbols():
    header("2. SYMBOL NAMES")
    everything = mt5.symbols_get()
    if not everything:
        note(BAD, f"symbols_get() returned nothing: {mt5.last_error()}")
        return {}
    names = [s.name for s in everything]
    print(f"  broker exposes {len(names)} symbols")

    resolved = {}
    for want in WANTED:
        if want in names:
            resolved[want] = want
            note(OK, f"{want} exists exactly")
            continue
        # Brokers suffix heavily: EURUSD.raw, EURUSDm, EURUSD_i, EURUSD.pro
        matches = [n for n in names if n.upper().startswith(want)]
        if matches:
            pick = sorted(matches, key=len)[0]
            resolved[want] = pick
            note(WARN, f"{want} not found; closest is {pick!r} "
                       f"(all: {matches[:6]}) — put this in config.yaml symbols")
        else:
            note(BAD, f"no symbol resembling {want} on this account")
    return resolved


FILLING_NAMES = {}


def describe_filling(symbol):
    """Which filling modes the broker allows for this symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return []
    allowed = []
    # symbol_info.filling_mode is a bitmask: 1 = FOK, 2 = IOC.
    mask = getattr(info, "filling_mode", 0)
    if mask & 1:
        allowed.append(("FOK", mt5.ORDER_FILLING_FOK))
    if mask & 2:
        allowed.append(("IOC", mt5.ORDER_FILLING_IOC))
    if not allowed:
        allowed.append(("RETURN", mt5.ORDER_FILLING_RETURN))
    return allowed


def inspect_symbols(resolved):
    header("3. SYMBOL DETAILS")
    details = {}
    for want, actual in resolved.items():
        if not mt5.symbol_select(actual, True):
            note(BAD, f"symbol_select({actual}) failed: {mt5.last_error()}")
            continue
        info = mt5.symbol_info(actual)
        tick = mt5.symbol_info_tick(actual)
        if info is None or tick is None:
            note(BAD, f"no info/tick for {actual}")
            continue

        pip = 0.01 if actual.upper().startswith(("USDJPY",)) or "JPY" in actual.upper() else 0.0001
        spread_price = tick.ask - tick.bid
        spread_pips = spread_price / pip if pip else float("nan")
        fills = describe_filling(actual)

        print(f"\n  {actual}")
        print(f"    digits={info.digits}  point={info.point}")
        print(f"    volume: min={info.volume_min} step={info.volume_step} max={info.volume_max}")
        print(f"    spread now: {spread_price:.5f} ({spread_pips:.2f} pips)")
        print(f"    filling modes accepted: {[n for n, _ in fills] or 'none reported'}")
        print(f"    stops level: {getattr(info, 'trade_stops_level', 'n/a')} points")

        if getattr(info, "trade_mode", 4) == 0:
            note(BAD, f"{actual} is disabled for trading on this account")
        details[actual] = {"pip": pip, "spread_pips": spread_pips, "fills": fills,
                           "volume_min": info.volume_min, "digits": info.digits}
    return details


def broker_offset(resolved):
    header("4. BROKER SERVER TIME")
    if not resolved:
        return 0
    symbol = list(resolved.values())[0]
    tick = mt5.symbol_info_tick(symbol)
    stamp = getattr(tick, "time", None) if tick is not None else None
    if not stamp:
        note(WARN, "no tick timestamp available — re-run during market hours to "
                   "derive broker.utc_offset_hours, or read it off the terminal's "
                   "Market Watch clock")
        return 0
    server = datetime.fromtimestamp(stamp, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    offset_hours = round((server - now).total_seconds() / 3600)
    print(f"  server clock : {server:%Y-%m-%d %H:%M:%S} (from last tick)")
    print(f"  your UTC now : {now:%Y-%m-%d %H:%M:%S}")
    if abs(offset_hours) > 14:
        note(WARN, f"derived offset {offset_hours}h looks wrong — the market may be "
                   f"closed, so the last tick is stale. Re-run during market hours.")
    else:
        note(OK, f"broker.utc_offset_hours: {offset_hours}")
    return offset_hours


def check_history(resolved):
    header("5. HISTORY DEPTH")
    need = 400
    for actual in resolved.values():
        for label, tf in (("H1", mt5.TIMEFRAME_H1), ("M15", mt5.TIMEFRAME_M15),
                          ("M5", mt5.TIMEFRAME_M5)):
            rates = mt5.copy_rates_from_pos(actual, tf, 0, need)
            got = 0 if rates is None else len(rates)
            if got >= need:
                note(OK, f"{actual} {label}: {got} bars")
            elif got >= 250:
                note(WARN, f"{actual} {label}: only {got} bars — enough to start, "
                           f"but scroll the chart back in the terminal to load more")
            else:
                note(BAD, f"{actual} {label}: only {got} bars — open that chart in "
                          f"the terminal and scroll back to download history")


def probe_orders(resolved, details):
    header("6. ORDER ACCEPTANCE  (order_check only — nothing is sent)")
    for actual, meta in details.items():
        tick = mt5.symbol_info_tick(actual)
        if tick is None:
            continue
        pip = meta["pip"]
        volume = meta["volume_min"]

        for name, mode in meta["fills"]:
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": actual, "volume": volume,
                "type": mt5.ORDER_TYPE_BUY, "price": tick.ask,
                "sl": round(tick.ask - 20 * pip, meta["digits"]),
                "tp": round(tick.ask + 40 * pip, meta["digits"]),
                "deviation": 20, "magic": 20260917, "comment": "lathe-preflight",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": mode,
            }
            result = mt5.order_check(request)
            code = getattr(result, "retcode", None)
            if code == 0:
                note(OK, f"{actual}: market order with {name} filling would be accepted")
            else:
                note(WARN, f"{actual}: market order with {name} filling rejected "
                           f"(retcode {code}: {getattr(result, 'comment', '')})")

        # The bot enters with a stop order that expires — the one most likely
        # to be refused, and the one nothing else would reveal until live.
        entry = round(tick.ask + 20 * pip, meta["digits"])
        pending = {
            "action": mt5.TRADE_ACTION_PENDING, "symbol": actual, "volume": volume,
            "type": mt5.ORDER_TYPE_BUY_STOP, "price": entry,
            "sl": round(entry - 20 * pip, meta["digits"]),
            "tp": round(entry + 40 * pip, meta["digits"]),
            "magic": 20260917, "comment": "lathe-preflight",
            "type_time": mt5.ORDER_TIME_SPECIFIED,
            "expiration": int(datetime.now(timezone.utc).timestamp() + 3600),
            "type_filling": meta["fills"][0][1] if meta["fills"] else mt5.ORDER_FILLING_RETURN,
        }
        result = mt5.order_check(pending)
        code = getattr(result, "retcode", None)
        if code == 0:
            note(OK, f"{actual}: pending BUY_STOP with expiry would be accepted")
        else:
            note(BAD, f"{actual}: pending BUY_STOP with expiry REJECTED "
                      f"(retcode {code}: {getattr(result, 'comment', '')}). "
                      f"The bot enters with these — try ORDER_TIME_GTC instead.")


def summary(resolved, details, offset):
    header("7. WHAT TO PUT IN config.yaml")
    bad = [m for lvl, m in findings if lvl == BAD]
    warn = [m for lvl, m in findings if lvl == WARN]

    print("symbols:")
    for actual in resolved.values():
        print(f"  - {actual}")
    print(f"\nbroker:\n  utc_offset_hours: {offset}")
    if details:
        worst = max(d["spread_pips"] for d in details.values())
        suggested = max(1.0, round(worst * 2, 1))
        print(f"\nspread:\n  max_spread_pips: {suggested}"
              f"   # 2x the widest spread seen just now ({worst:.2f})")
        print("  # Re-run this during 22:00-06:00 New York — spreads widen overnight,")
        print("  # and that is the only window this bot trades in.")

    print(f"\n{'-' * 66}")
    print(f"{len(bad)} blocking, {len(warn)} to look at.")
    if bad:
        print("\nBlocking:")
        for m in bad:
            print(f"  - {m}")
    print("\nWhile safety.default_mode is PAPER, no order is sent whatever this says.")
    return 1 if bad else 0


def main():
    if not connect():
        return 2
    resolved, details, offset = {}, {}, 0
    try:
        # Each section is isolated: a broker that refuses one call should not
        # cost you the rest of the report.
        for label, step in (("symbol names", lambda: resolve_symbols()),):
            try:
                resolved = step()
            except Exception as e:
                note(BAD, f"{label} check failed: {type(e).__name__}: {e}")

        for label, step in (("symbol details", lambda: inspect_symbols(resolved)),):
            try:
                details = step()
            except Exception as e:
                note(BAD, f"{label} check failed: {type(e).__name__}: {e}")

        for label, step in (("server time", lambda: broker_offset(resolved)),):
            try:
                offset = step()
            except Exception as e:
                note(WARN, f"{label} check failed: {type(e).__name__}: {e}")

        for label, step in (("history depth", lambda: check_history(resolved)),
                            ("order acceptance", lambda: probe_orders(resolved, details))):
            try:
                step()
            except Exception as e:
                note(BAD, f"{label} check failed: {type(e).__name__}: {e}")

        return summary(resolved, details, offset)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
