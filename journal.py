"""Whetstone Phase 1: durable intents and conservative broker reconciliation.

JSONL is an atomic materialized view (one row per trade_id), not an append-only
stream. State is authoritative; projections can be rebuilt after a crash.
Only a dedicated, non-reversing position can resolve automatically.
"""
import copy
import hashlib
import json
import math
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

PNL = ('gross_pnl', 'commission', 'swap', 'fees', 'net_pnl')
BEGIN = '<!-- WHETSTONE CLOSED TRADES BEGIN -->'
END = '<!-- WHETSTONE CLOSED TRADES END -->'


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds')


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.journal-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def strategy_version():
    root = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                      stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                        cwd=root, timeout=5).strip()
        if not dirty:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    # A dirty worktree must not masquerade as a release commit.
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')) + [root / 'config.yaml']:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return 'source-sha256:' + digest.hexdigest()


def vars_or_dict(value):
    return value._asdict() if hasattr(value, '_asdict') else vars(value)


def identity(api):
    a = api.account_info()
    if a is None:
        raise RuntimeError('journal: broker account unavailable')
    mode = getattr(a, 'trade_mode', None)
    kind = {0: 'demo', 2: 'real'}.get(mode)
    if kind is None:
        raise RuntimeError('journal: unsupported/unknown account type')
    return {'login': str(a.login), 'server': getattr(a, 'server', ''),
            'currency': a.currency, 'account_type': kind}


class TradeJournal:
    def __init__(self, state, path, vault):
        self.state, self.path, self.vault = state, Path(path), Path(vault)
        if getattr(state, 'load_error', None):
            raise RuntimeError('journal: state could not be loaded; restore it before running')
        if state.get('journal_trades') is None and self.path.exists() and self.path.stat().st_size:
            raise RuntimeError('journal: existing outcomes without matching state; restore state')
        self.trades = copy.deepcopy(state.get('journal_trades', {}))
        self.version = strategy_version()

    def save(self):
        self.state.set(journal_trades=self.trades)

    def begin(self, signal, account, mode, context, original_stop, now):
        if any(t['account'] != account for t in self.trades.values()):
            raise RuntimeError('journal: account changed; use a separate state file')
        trade_id, signal_id = str(uuid.uuid4()), str(uuid.uuid4())
        record = dict(event='trade_closed', schema_version=1, trade_id=trade_id,
                      signal_id=signal_id, broker_ids={'orders': [], 'deals': [], 'position': ''},
                      execution_mode=mode, account_type=account['account_type'],
                      strategy_version=self.version, account_currency=account['currency'],
                      symbol=signal['symbol'],
                      side=signal['signal'].lower(), opened_at=None, closed_at=None,
                      entry_avg=None, exit_avg=None, lots=None, fills=[], exits=[],
                      stop_changes=[], context=context, outcome_status='unresolved',
                      missing_fields=[], **{k: None for k in PNL})
        self.trades[trade_id] = {'record': record, 'account': account, 'status': 'intent',
                                 'created_at': now.isoformat(), 'signal': dict(signal),
                                 'original_stop': original_stop,
                                 'comment': 'lh:' + trade_id.replace('-', '')[:26]}
        self.save()  # before sending any order
        return trade_id, signal_id

    def submitted(self, trade_id, result):
        t = self.trades[trade_id]
        r = t['record']
        if r['execution_mode'] == 'paper':
            t['status'] = 'paper_signal'
            # Synthetic tickets are not broker IDs and never become closures.
        else:
            r['broker_ids']['orders'] = [str(result.order)] if result.order else []
            r['broker_ids']['deals'] = [str(result.deal)] if result.deal else []
            t['status'] = 'pending'
        self.save()

    def stop_changed(self, position_id, new_stop, reason, now):
        for t in self.trades.values():
            if t['record']['broker_ids']['position'] == str(position_id):
                t['record']['stop_changes'].append({'ts': now.isoformat(),
                                                   'price': new_stop, 'reason': reason})
                self.save()
                return

    def metadata(self, position):
        pid = str(getattr(position, 'identifier', position.ticket))
        for t in self.trades.values():
            r = t['record']
            if r['broker_ids']['position'] == pid and t['status'] == 'open':
                return {'entry': r['entry_avg'], 'original_stop': t['original_stop']}
        return None

    def blocked(self):
        return any(t['status'] in ('intent', 'unresolved') for t in self.trades.values()
                   if t['record']['execution_mode'] == 'live')

    def _unresolved(self, t, missing):
        r = t['record']
        missing = list(missing) + list(PNL)
        missing += [key for key in ('opened_at', 'closed_at', 'entry_avg', 'exit_avg', 'lots')
                    if r[key] is None]
        r.update(outcome_status='unresolved', missing_fields=sorted(set(missing)))
        r.update({k: None for k in PNL})
        t['status'] = 'unresolved'

    def reconcile(self, api, now):
        account = identity(api)
        positions, orders = api.positions_get(), api.orders_get()
        # None is an API error, not an empty account.
        if positions is None or orders is None:
            raise RuntimeError('journal: broker position/order snapshot unavailable')
        for t in self.trades.values():
            r = t['record']
            if t['account'] != account:
                raise RuntimeError('journal: account changed; use a separate state file')
            if r['execution_mode'] == 'paper' or t['status'] in ('closed', 'cancelled'):
                continue
            self._reconcile_one(t, api, positions, orders, now)
        self.save()
        self.project()

    def _reconcile_one(self, t, api, positions, orders, now):
        r, ids = t['record'], t['record']['broker_ids']
        # Recover the send/ack crash window via the persisted unique comment.
        if not ids['orders']:
            history = api.history_orders_get(datetime.fromisoformat(t['created_at']), now)
            if history is None:
                self._unresolved(t, ['order_history', 'broker_ids.orders'])
                return
            matches = {str(o.ticket): o for o in list(orders) + list(history)
                       if getattr(o, 'comment', '') == t['comment']}
            if len(matches) != 1:
                self._unresolved(t, ['submission_acknowledgement'])
                return
            ids['orders'] = list(matches)
        entry_order = ids['orders'][0]
        working = [o for o in orders if str(o.ticket) == entry_order]
        historical = api.history_orders_get(ticket=int(entry_order))
        if historical is None or (not historical and not working):
            self._unresolved(t, ['order_history'])
            return
        order = (working or list(historical))[0]
        pid = getattr(order, 'position_id', 0)
        if pid:
            ids['position'] = str(pid)
        if not ids['position']:
            if working:
                t['status'] = 'pending'
                return
            # A cancelled/rejected/expired order with no fills is not a trade.
            if (getattr(order, 'state', None) in (2, 5, 6)
                    and getattr(order, 'volume_initial', None) is not None
                    and order.volume_initial == getattr(order, 'volume_current', None)):
                t['status'] = 'cancelled'
                return
            self._unresolved(t, ['broker_ids.position', 'fills'])
            return
        deals = api.history_deals_get(position=int(ids['position']))
        if deals is None or not deals:
            self._unresolved(t, ['deal_history', 'fills', 'exits', *PNL])
            return
        # Query includes manual closing deals too: do not filter by magic here.
        unique = {}
        missing = []
        for d in deals:
            ticket = getattr(d, 'ticket', None)
            if not ticket:
                missing.append('deal.ticket')
                continue
            if str(ticket) in unique and vars_or_dict(unique[str(ticket)]) != vars_or_dict(d):
                missing.append('conflicting_duplicate_deal')
            unique[str(ticket)] = d
        deals = sorted(unique.values(), key=lambda d: (getattr(d, 'time_msc', 0), d.ticket))
        ids['deals'] = list(unique)
        ids['orders'] = list(dict.fromkeys([entry_order] +
                             [str(d.order) for d in deals if getattr(d, 'order', 0)]))
        fills, exits = [], []
        for d in deals:
            if str(getattr(d, 'position_id', '')) != ids['position']:
                missing.append('position_identity')
            if getattr(d, 'symbol', None) != r['symbol']:
                missing.append('symbol_identity')
            # INOUT (netting reversals) and non-trade balance adjustments need
            # explicit attribution; never silently assign them to this trade.
            if getattr(d, 'type', None) not in (0, 1) or getattr(d, 'entry', None) not in (0, 1, 3):
                missing.append('unsupported_deal_attribution')
                continue
            if d.entry == 0 and str(getattr(d, 'order', '')) != entry_order:
                missing.append('shared_position')
            expected_type = 0 if r['side'] == 'buy' else 1
            if d.type != (expected_type if d.entry == 0 else 1 - expected_type):
                missing.append('deal_side')
            required = ('order', 'price', 'volume', 'time_msc', 'profit', 'commission', 'swap', 'fee')
            absent = [key for key in required if not isinstance(getattr(d, key, None), (int, float))
                      or not math.isfinite(getattr(d, key))]
            missing.extend('deal.' + key for key in absent)
            if absent:
                continue
            if d.volume <= 0 or d.price <= 0 or d.time_msc <= 0:
                missing.append('deal_geometry')
                continue
            item = {'ts': utc(d.time_msc / 1000), 'price': d.price, 'lots': d.volume,
                    'deal_id': str(d.ticket), 'order_id': str(d.order)}
            if d.entry == 0:
                fills.append(item)
            else:
                # MT5 reports SL for both original and trailing stops. Preserve
                # that fact; stop_changes permits analysis without guessing.
                item['reason'] = {0: 'manual', 1: 'manual', 2: 'manual',
                                  4: 'sl', 5: 'tp', 6: 'stop_out'}.get(getattr(d, 'reason', None), 'unknown')
                exits.append(item)
        r['fills'], r['exits'] = fills, exits
        vin, vout = sum(f['lots'] for f in fills), sum(f['lots'] for f in exits)
        r['lots'] = vin or None
        r['entry_avg'] = sum(f['price'] * f['lots'] for f in fills) / vin if vin else None
        r['exit_avg'] = sum(f['price'] * f['lots'] for f in exits) / vout if vout else None
        r['opened_at'] = min((f['ts'] for f in fills), default=None)
        active = any(str(getattr(p, 'identifier', p.ticket)) == ids['position'] for p in positions)
        if active or working:
            if missing:
                self._unresolved(t, missing)
                return
            t['status'] = 'open' if active else 'pending'
            # Do not publish partial exits as completed trades.
            r['closed_at'] = None
            return
        r['closed_at'] = max((f['ts'] for f in exits), default=None)
        if not vin or not math.isclose(vin, vout, abs_tol=1e-8):
            missing.append('balanced_entry_exit_volume')
        initial, remaining = getattr(order, 'volume_initial', None), getattr(order, 'volume_current', None)
        if initial is None or remaining is None:
            missing.append('order_fill_volume')
        elif not math.isclose(vin, initial - remaining, abs_tol=1e-8):
            missing.append('complete_entry_history')
        if missing:
            self._unresolved(t, missing)
            return
        values = {key: sum(getattr(d, attr) for d in deals) for key, attr in
                  [('gross_pnl', 'profit'), ('commission', 'commission'), ('swap', 'swap'), ('fees', 'fee')]}
        # Broker values are signed amounts (charges negative, credits positive).
        values['net_pnl'] = sum(values.values())
        context_missing = ['context.' + key for key, value in r['context'].items() if value is None]
        r.update(values, outcome_status='resolved', missing_fields=context_missing)
        t['status'] = 'closed'

    def project(self):
        records = [t['record'] for t in self.trades.values() if t['status'] in ('closed', 'unresolved')]
        atomic_text(self.path, ''.join(json.dumps(r, allow_nan=False, sort_keys=True) + '\n' for r in records))
        previous = self.vault.read_text(encoding='utf-8') if self.vault.exists() else ''
        if BEGIN in previous and END in previous:
            before, rest = previous.split(BEGIN, 1)
            _, after = rest.split(END, 1)
            previous = before.rstrip() + after
        lines = [BEGIN, '## Whetstone trade outcomes', '']
        for r in records:
            result = str(r['net_pnl']) if r['outcome_status'] == 'resolved' else 'UNRESOLVED: ' + ', '.join(r['missing_fields'])
            lines.append(f"- {r['trade_id']} | {r['symbol']} {r['side']} | "
                         f"{r['execution_mode']}/{r['account_type']} | net P&L: {result}")
        lines.append(END)
        atomic_text(self.vault, previous.rstrip() + '\n\n' + '\n'.join(lines) + '\n')
