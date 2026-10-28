"""Pure analysis. No imports from Lathe, broker SDKs, or trading credentials."""
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


class DataError(ValueError):
    pass


def stamp(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError('timezone missing')
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as e:
        raise DataError('expected timezone-aware ISO8601 timestamp') from e


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def validate_record(r):
    if not isinstance(r, dict) or r.get('event') != 'trade_closed' or r.get('schema_version') != 1:
        raise DataError('unsupported_record')
    for key in ('trade_id', 'strategy_version', 'account_currency', 'symbol'):
        if not isinstance(r.get(key), str) or not r[key].strip():
            raise DataError('missing_' + key)
    if r.get('execution_mode') not in ('paper', 'live') or r.get('account_type') not in ('demo', 'real'):
        raise DataError('invalid_mode_or_account')
    if r.get('outcome_status') != 'resolved':
        raise DataError('unresolved')
    if stamp(r.get('closed_at')) < stamp(r.get('opened_at')):
        raise DataError('invalid_time_order')
    keys = ('gross_pnl', 'commission', 'swap', 'fees', 'net_pnl')
    if any(not number(r.get(k)) for k in keys):
        raise DataError('missing_or_invalid_pnl')
    if not math.isclose(sum(r[k] for k in keys[:-1]), r['net_pnl'], rel_tol=1e-9, abs_tol=1e-6):
        raise DataError('net_pnl_mismatch')
    if not isinstance(r.get('context'), dict):
        raise DataError('missing_context')
    return r


def load_snapshot(path):
    """Quarantine every conflicting occurrence of an ID, not just the last."""
    raw = Path(path).read_bytes()
    by_id, bad, conflicts = {}, Counter(), set()
    for line in raw.decode('utf-8').splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or not isinstance(r.get('trade_id'), str):
                raise ValueError()
            tid = r['trade_id']
            if tid in by_id:
                if by_id[tid] != r:
                    conflicts.add(tid)
                else:
                    bad['identical_duplicate'] += 1
            else:
                by_id[tid] = r
        except (ValueError, TypeError):
            bad['malformed_record'] += 1
    good = []
    for tid, r in by_id.items():
        if tid in conflicts:
            bad['conflicting_trade_id'] += 1
            continue
        try:
            good.append(validate_record(r))
        except DataError as e:
            bad[str(e)] += 1
    return good, dict(bad), hashlib.sha256(raw).hexdigest()


def metrics(rows, minimum=30):
    ordered = sorted(rows, key=lambda r: (stamp(r['closed_at']), r['trade_id']))
    pnl = [r['net_pnl'] for r in ordered]
    n = len(pnl)
    wins = sum(p > 0 for p in pnl)
    losses = sum(p < 0 for p in pnl)
    balance = peak = drawdown = 0.
    # Aggregate simultaneous closes; arbitrary ticket ordering should not
    # manufacture a drawdown when deals have the same timestamp.
    by_time = defaultdict(float)
    for r in ordered:
        by_time[stamp(r['closed_at'])] += r['net_pnl']
    for delta in by_time.values():
        balance += delta
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    interval = None
    if n:
        z = 1.959963984540054
        p = wins/n
        center = (p + z*z/(2*n))/(1 + z*z/n)
        margin = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/(1 + z*z/n)
        interval = [max(0., center-margin), min(1., center+margin)]
    return {'trades': n, 'wins': wins, 'losses': losses, 'breakeven': n-wins-losses,
            'net_pnl': sum(pnl), 'expectancy': sum(pnl)/n if n else None,
            'win_rate': wins/n if n else None, 'win_rate_interval_95': interval,
            'closed_trade_drawdown': drawdown,
            'sample_status': 'exploratory' if n >= minimum else 'insufficient',
            'minimum_sample': minimum}


def cohort_key(r):
    return (r['execution_mode'], r['account_type'], r['account_currency'],
            r['strategy_version'], r.get('data_origin', 'journal'))


def build_summary(path, start, end, minimum=30):
    if minimum < 30:
        raise DataError('minimum sample cannot be below 30')
    begin, finish = stamp(start), stamp(end)
    if begin >= finish:
        raise DataError('start must precede end')
    rows, excluded, source_sha = load_snapshot(path)
    selected = [r for r in rows if begin <= stamp(r['closed_at']) < finish]
    excluded['outside_window'] = len(rows)-len(selected)
    cohorts = defaultdict(list)
    for r in selected:
        cohorts[cohort_key(r)].append(r)
    result = []
    for key, items in sorted(cohorts.items()):
        labels = dict(zip(('execution_mode','account_type','currency','strategy_version','data_origin'), key))
        groups = {}
        for dimension in ('symbol','session','setup'):
            bins = defaultdict(list)
            for r in items:
                value = r['symbol'] if dimension == 'symbol' else r['context'].get(dimension)
                bins[str(value) if value not in (None, '') else 'unknown'].append(r)
            groups[dimension] = {label: metrics(group, minimum) for label, group in sorted(bins.items())}
        # Symbol-specific quantiles, in explicit price units. Missing fill
        # spread never falls back silently to signal spread.
        for field in ('spread_at_entry', 'spread_at_signal'):
            bins, thresholds = defaultdict(list), {}
            for symbol in sorted({r['symbol'] for r in items}):
                symbol_rows = [r for r in items if r['symbol'] == symbol]
                known = sorted(r['context'][field] for r in symbol_rows
                               if number(r['context'].get(field)) and r['context'][field] >= 0
                               and r['context'].get('spread_unit') == 'price')
                if known:
                    low, high = known[(len(known)-1)//3], known[2*(len(known)-1)//3]
                    thresholds[symbol] = {'low_max': low, 'medium_max': high, 'unit':'price'}
                for r in symbol_rows:
                    value = r['context'].get(field)
                    bucket = 'unknown'
                    if known and number(value) and value >= 0 and r['context'].get('spread_unit') == 'price':
                        bucket = 'low' if value <= low else 'medium' if value <= high else 'high'
                    bins[symbol + ':' + bucket].append(r)
            groups[field] = {'thresholds': thresholds,
                             'buckets': {label:metrics(group, minimum) for label, group in sorted(bins.items())}}
        result.append({'cohort':labels, 'overall':metrics(items, minimum), 'groups':groups})
    summary = {'schema_version':1, 'window':{'start':begin.isoformat(), 'end':finish.isoformat()},
               'source_sha256':source_sha, 'minimum_sample':minimum,
               'included_trades':len(selected), 'excluded':excluded, 'cohorts':result,
               'limitations':['Closed-trade P&L drawdown is not equity or intratrade drawdown.',
                              'Thirty trades is a review threshold, not statistical proof.',
                              'Subgroup comparisons are exploratory and may reflect multiple testing.',
                              'Wilson intervals assume independent trials; clustered trades weaken that assumption.',
                              'Observed performance does not establish a causal rule improvement.']}
    summary['summary_id'] = digest(summary)
    return summary


def safe_text(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def markdown(summary):
    lines = ['# Whetstone trade review', '',
             f"Window: {summary['window']['start']} to {summary['window']['end']} (end excluded).", '',
             f"Included trades: {summary['included_trades']}. Minimum review sample: {summary['minimum_sample']}.", '',
             '## Data quality', '', 'Excluded/duplicate counts: `' + json.dumps(summary['excluded'], sort_keys=True) + '`.', '']
    def table(entries):
        out = ['| Group | N | Win rate | Mean net/trade | Net P&L | Closed-trade drawdown | Sample |',
               '|---|---:|---:|---:|---:|---:|---|']
        for name, m in entries:
            rate = f"{m['win_rate']:.1%}" if m['win_rate'] is not None else 'n/a'
            mean = f"{m['expectancy']:.2f}" if m['expectancy'] is not None else 'n/a'
            out.append(f"| {safe_text(name)} | {m['trades']} | {rate} | {mean} | {m['net_pnl']:.2f} | {m['closed_trade_drawdown']:.2f} | {m['sample_status']} |")
        return out
    for c in summary['cohorts']:
        labels = c['cohort']
        lines += ['## ' + safe_text(' / '.join(labels.values())), '', *table([('Overall', c['overall'])]), '']
        interval = c['overall']['win_rate_interval_95']
        if interval:
            lines += [f"Win-rate 95% Wilson interval: {interval[0]:.1%} to {interval[1]:.1%}.", '']
        for dimension, groups in c['groups'].items():
            lines += ['### ' + dimension, '']
            if dimension.startswith('spread_'):
                lines += ['Buckets are relative to this symbol/window; thresholds: `' + json.dumps(groups['thresholds']) + '`.', '']
                groups = groups['buckets']
            lines += table(groups.items()) + ['']
    if not summary['cohorts']:
        lines += ['No eligible resolved trades in this window. No performance conclusion is available.', '']
    lines += ['## Interpretation limits', ''] + ['- ' + x for x in summary['limitations']]
    return '\n'.join(lines) + '\n'
