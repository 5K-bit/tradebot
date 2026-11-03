"""Broker-shaped history fixtures: no tests can contact a real terminal."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as Obj

import pytest
import fake_mt5
from journal import TradeJournal, identity
from state import JsonState
from trader import Lathe

NOW = datetime(2026, 1, 15, 3, 30, tzinfo=timezone.utc)


class Broker:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.history = [Obj(ticket=101, position_id=9001, state=4,
                            volume_initial=1.0, volume_current=0.0)]
        self.deals = [deal(1, 0, .4, 1.10), deal(2, 0, .6, 1.12),
                      deal(3, 1, .3, 1.13, 12), deal(4, 1, .7, 1.14, 18)]

    def account_info(self):
        return Obj(login=7, server='demo', currency='USD', trade_mode=0)

    def positions_get(self):
        return self.positions

    def orders_get(self):
        return self.orders

    def history_orders_get(self, *args, **kwargs):
        return self.history

    def history_deals_get(self, **kwargs):
        return self.deals


def deal(ticket, entry, lots, price, profit=0, **overrides):
    values = dict(ticket=ticket, order=101 if entry == 0 else 200 + ticket,
                  position_id=9001, symbol='EURUSD', type=0 if entry == 0 else 1,
                  entry=entry, time_msc=int(NOW.timestamp()*1000) + ticket*1000,
                  volume=lots, price=price, profit=profit, commission=-.5,
                  swap=-.1, fee=-.05, reason=5)
    values.update(overrides)
    return Obj(**values)


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(JsonState(str(tmp_path/'state.json')), tmp_path/'trade_closed.jsonl', tmp_path/'trades.md')


def start(journal, broker, mode='live'):
    tid, sid = journal.begin({'symbol':'EURUSD', 'signal':'BUY'}, identity(broker), mode,
                             {'session':'2026-01-14'}, 1.09, NOW)
    journal.submitted(tid, Obj(order=101, deal=0))
    return tid, sid


def rows(journal):
    return [json.loads(l) for l in journal.path.read_text().splitlines()]


def test_partial_entries_exits_weighted_prices_and_signed_costs(journal):
    b = Broker()
    tid, sid = start(journal, b)
    journal.reconcile(b, NOW)
    r, = rows(journal)
    assert r['trade_id'] == tid and r['signal_id'] == sid
    assert len(r['fills']) == len(r['exits']) == 2
    assert r['entry_avg'] == pytest.approx(1.112)
    assert r['exit_avg'] == pytest.approx(1.137)
    assert r['lots'] == 1
    assert r['gross_pnl'] == 30
    assert r['commission'] == -2
    assert r['swap'] == pytest.approx(-.4)
    assert r['fees'] == pytest.approx(-.2)
    assert r['net_pnl'] == pytest.approx(27.4)
    assert r['outcome_status'] == 'resolved'
    assert r['broker_ids']['position'] == '9001'
    assert set(r['broker_ids']['orders']) == {'101', '203', '204'}


def test_restart_mid_trade_and_partial_exit_is_not_closure(journal):
    b = Broker(); tid, sid = start(journal, b)
    b.positions = [Obj(ticket=9999, identifier=9001)]
    b.deals = b.deals[:3]
    journal.reconcile(b, NOW)
    assert rows(journal) == []
    restored = TradeJournal(JsonState(str(journal.state.path)), journal.path, journal.vault)
    assert restored.trades[tid]['record']['signal_id'] == sid
    assert restored.metadata(b.positions[0])['entry'] == pytest.approx(1.112)
    b.positions = []; b.deals.append(deal(4, 1, .7, 1.14, 18))
    restored.reconcile(b, NOW)
    assert rows(restored)[0]['trade_id'] == tid


@pytest.mark.parametrize('missing', [None, []])
def test_unavailable_history_null_then_resolves_without_duplicate(journal, missing):
    b = Broker(); start(journal, b); saved = b.deals; b.deals = missing
    journal.reconcile(b, NOW)
    r, = rows(journal)
    assert r['outcome_status'] == 'unresolved'
    assert all(r[k] is None for k in ('gross_pnl', 'net_pnl', 'commission', 'swap', 'fees'))
    assert 'deal_history' in r['missing_fields']
    assert journal.blocked()
    b.deals = saved
    journal.reconcile(b, NOW)
    journal.reconcile(b, NOW)
    assert len(rows(journal)) == 1
    assert rows(journal)[0]['outcome_status'] == 'resolved'
    assert journal.vault.read_text().count(r['trade_id']) == 1
    assert not journal.blocked()


def test_pending_and_cancelled_orders_are_not_closed_trades(journal):
    b = Broker(); start(journal, b)
    o = Obj(ticket=101, position_id=0, state=1, volume_initial=1., volume_current=1.)
    b.orders = [o]; b.history = []
    journal.reconcile(b, NOW)
    assert rows(journal) == []
    b.orders = []; o.state = 2; b.history = [o]
    journal.reconcile(b, NOW)
    assert rows(journal) == []
    assert not journal.blocked()


def test_paper_and_live_modes_and_no_fake_paper_closures(journal):
    b = Broker()
    paper, _ = start(journal, b, 'paper')
    live, _ = start(journal, b, 'live')
    journal.reconcile(b, NOW)
    assert journal.trades[paper]['record']['execution_mode'] == 'paper'
    assert journal.trades[paper]['record']['broker_ids']['orders'] == []
    assert rows(journal)[0]['trade_id'] == live
    assert rows(journal)[0]['execution_mode'] == 'live'
    assert rows(journal)[0]['account_type'] == 'demo'


def test_position_api_error_is_not_closure(journal):
    b = Broker(); start(journal, b); b.positions = None
    with pytest.raises(RuntimeError, match='snapshot'):
        journal.reconcile(b, NOW)
    assert not journal.path.exists()


@pytest.mark.parametrize('problem', ['fee', 'imbalance', 'shared', 'reversal', 'incomplete_entry'])
def test_incomplete_or_ambiguous_history_cannot_resolve(journal, problem):
    b = Broker(); start(journal, b)
    if problem == 'fee': del b.deals[0].fee
    if problem == 'imbalance': b.deals.pop()
    if problem == 'shared': b.deals[0].order = 888
    if problem == 'reversal': b.deals[0].entry = 2
    if problem == 'incomplete_entry': b.history[0].volume_initial = 2
    journal.reconcile(b, NOW)
    r, = rows(journal)
    assert r['outcome_status'] == 'unresolved'
    assert r['net_pnl'] is None
    assert r['missing_fields']


def test_duplicate_deals_are_not_double_counted(journal):
    b = Broker(); start(journal, b); b.deals += [b.deals[-1]]
    journal.reconcile(b, NOW)
    assert rows(journal)[0]['gross_pnl'] == 30


def test_intent_persisted_and_send_ack_crash_recovered(journal):
    b = Broker()
    tid, sid = journal.begin({'symbol':'EURUSD', 'signal':'BUY'}, identity(b), 'live', {}, 1.09, NOW)
    persisted = json.loads(journal.state.path.read_text())['journal_trades'][tid]
    assert persisted['record']['signal_id'] == sid
    assert persisted['status'] == 'intent'
    b.history[0].comment = persisted['comment']
    restored = TradeJournal(JsonState(str(journal.state.path)), journal.path, journal.vault)
    restored.reconcile(b, NOW)
    assert rows(restored)[0]['outcome_status'] == 'resolved'


def test_unknown_submission_blocks_without_resending(journal):
    b = Broker()
    journal.begin({'symbol':'EURUSD','signal':'BUY'}, identity(b), 'live', {}, 1.09, NOW)
    b.history = []
    journal.reconcile(b, NOW)
    assert journal.blocked()
    assert rows(journal)[0]['net_pnl'] is None


def test_stop_changes_survive_restart(journal):
    b = Broker(); tid, _ = start(journal, b)
    b.positions = [Obj(ticket=9999, identifier=9001)]
    journal.reconcile(b, NOW)
    journal.stop_changed(9001, 1.115, 'trailing', NOW)
    restored = TradeJournal(JsonState(str(journal.state.path)), journal.path, journal.vault)
    assert restored.trades[tid]['record']['stop_changes'][0]['reason'] == 'trailing'


def test_projection_rebuilt_after_state_commit_crash(journal, monkeypatch):
    b = Broker(); start(journal, b)
    monkeypatch.setattr(journal, 'project', lambda: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError): journal.reconcile(b, NOW)
    restored = TradeJournal(JsonState(str(journal.state.path)), journal.path, journal.vault)
    restored.reconcile(b, NOW)
    assert len(rows(restored)) == 1


def test_account_switch_refused(journal):
    b = Broker(); tid, _ = start(journal, b)
    journal.trades[tid]['account']['login'] = 'another-account'
    with pytest.raises(RuntimeError, match='account changed'): journal.reconcile(b, NOW)


def test_genuine_zero_profit_is_valid(journal):
    b = Broker(); start(journal, b)
    for d in b.deals: d.profit = d.commission = d.swap = d.fee = 0.
    journal.reconcile(b, NOW)
    assert rows(journal)[0]['net_pnl'] == 0.
    assert rows(journal)[0]['outcome_status'] == 'resolved'


def test_loss_protection_replay_is_idempotent(state):
    from protection import Protection, ProtectionConfig
    p = Protection(ProtectionConfig(), state)
    p.record_trade_closed(-10, 'session', event_id='trade')
    restarted = Protection(ProtectionConfig(), JsonState(str(state.path)))
    restarted.record_trade_closed(-10, 'session', event_id='trade')
    assert restarted._consecutive_losses == 1


def test_real_pipeline_paper_gap_and_live_signal_ids(config, conn, state, trending_setup):
    config['safety'] = {'default_mode':'PAPER', 'live_trading_enabled':False}
    bot = Lathe(config, conn, state)
    bot.evaluate('EURUSD', '2026-01-14', conn.account_info(), NOW)
    t, = bot.journal.trades.values()
    assert t['record']['execution_mode'] == 'paper'
    assert t['status'] == 'paper_signal'
    assert conn.pending_orders() == conn.open_positions() == []
    bot.reconcile_closed_trades('2026-01-14', NOW + timedelta(minutes=2))
    assert rows(bot.journal) == []


def test_closed_trade_integration_updates_protection_once(lathe, monkeypatch):
    b = Broker()
    for name in ('account_info','positions_get','orders_get','history_orders_get','history_deals_get'):
        monkeypatch.setattr(fake_mt5, name, getattr(b, name))
    start(lathe.journal, b)
    b.deals[-1].profit = -100
    lathe.reconcile_closed_trades('2026-01-14', NOW)
    lathe.reconcile_closed_trades('2026-01-14', NOW + timedelta(minutes=2))
    assert lathe.protection._consecutive_losses == 1


def test_corrupt_state_refuses_trading(tmp_path):
    path = tmp_path/'state.json'
    path.write_text('{broken')
    with pytest.raises(RuntimeError, match='state could not be loaded'):
        TradeJournal(JsonState(str(path)), tmp_path/'closed.jsonl', tmp_path/'trades.md')


def test_existing_journal_cannot_be_erased_by_missing_state(journal):
    b = Broker(); start(journal, b); journal.reconcile(b, NOW)
    journal.state.path.unlink()
    with pytest.raises(RuntimeError, match='without matching state'):
        TradeJournal(JsonState(str(journal.state.path)), journal.path, journal.vault)


def test_unknown_cost_and_entry_spread_are_explicit(journal):
    b = Broker(); tid, _ = start(journal, b)
    journal.trades[tid]['record']['context']['spread_at_entry'] = None
    journal.reconcile(b, NOW)
    assert 'context.spread_at_entry' in rows(journal)[0]['missing_fields']


def test_wrong_side_and_conflicting_duplicate_never_resolve(journal):
    b = Broker(); start(journal, b)
    b.deals[0].type = 1
    b.deals += [deal(4, 1, .8, 1.14, 18)]
    journal.reconcile(b, NOW)
    assert rows(journal)[0]['outcome_status'] == 'unresolved'
    assert 'conflicting_duplicate_deal' in rows(journal)[0]['missing_fields']


def test_closed_account_cannot_be_reused_for_another_account(journal):
    b = Broker(); start(journal, b); journal.reconcile(b, NOW)
    b.account_info = lambda: Obj(login=8, server='demo', currency='USD', trade_mode=0)
    with pytest.raises(RuntimeError, match='account changed'):
        journal.reconcile(b, NOW)


@pytest.mark.parametrize('retcode', [10008, 10010])
def test_pending_acknowledgements_are_not_rejected(conn, trending_setup, monkeypatch, retcode):
    send = fake_mt5.order_send
    def wrapped(request):
        result = send(request)
        result.retcode = retcode
        return result
    monkeypatch.setattr(fake_mt5, 'order_send', wrapped)
    result = conn.pending_stop_order('EURUSD', .1, 'buy', 1.1, 1.09, 1.12)
    assert result.retcode == retcode


def test_submission_counter_recovers_once_after_restart(lathe, monkeypatch):
    b = Broker()
    for name in ('account_info','positions_get','orders_get','history_orders_get','history_deals_get'):
        monkeypatch.setattr(fake_mt5, name, getattr(b, name))
    start(lathe.journal, b)
    lathe.reconcile_closed_trades('2026-01-14', NOW)
    lathe.reconcile_closed_trades('2026-01-14', NOW + timedelta(minutes=2))
    assert lathe.protection.trades_this_session('2026-01-14') == 1


def test_candle_cursor_saved_before_order_submission(lathe, conn, trending_setup, monkeypatch):
    def crash(*args, **kwargs):
        persisted = json.loads(lathe.state.path.read_text())
        assert persisted['last_bar_time']['EURUSD'] == lathe.bar_cursor['EURUSD']
        assert persisted['journal_trades']
        raise RuntimeError('lost acknowledgement')
    monkeypatch.setattr(conn, 'pending_stop_order', crash)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        lathe.evaluate('EURUSD', '2026-01-14', conn.account_info(), NOW)
    assert lathe.journal.blocked()
