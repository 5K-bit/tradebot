"""Deterministic bid/ask tick replay using Lathe's pure strategy modules.

Never imports trader.py, MetaTrader5, or the live connector. This is a research
execution model, not a claim of broker parity: fills are full-volume, stops use
observed ticks plus declared adverse slippage, and latency/liquidity are absent.
"""
import copy
import csv
import hashlib
import math
import statistics
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import config_schema as schema
import indicators
import protection
import regime
import scoring
import sessions
import setups
import trade_management
from risk_manager import RiskManager
from .core import DataError, digest, metrics, number, stamp

ENGINE_VERSION = 'whetstone-tick-replay-1'
DTYPE = [('time','i8'),('open','f8'),('high','f8'),('low','f8'),('close','f8')]


@dataclass(frozen=True)
class Tick:
    time: datetime
    symbol: str
    bid: float
    ask: float
    quote_to_account_rate: float


def ticks(path):
    """CSV: aware ISO time, symbol, bid, ask, quote-to-account conversion rate."""
    previous = None
    with Path(path).open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        required = {'timestamp','symbol','bid','ask','quote_to_account_rate'}
        if not required.issubset(reader.fieldnames or []):
            raise DataError('tick CSV requires ' + ', '.join(sorted(required)))
        for line, row in enumerate(reader, 2):
            try:
                t = Tick(stamp(row['timestamp']), row['symbol'], float(row['bid']),
                         float(row['ask']), float(row['quote_to_account_rate']))
                if not t.symbol or any(not number(v) or v <= 0 for v in (t.bid,t.ask,t.quote_to_account_rate)):
                    raise ValueError()
                if t.ask < t.bid or previous is not None and t.time < previous:
                    raise ValueError()
            except (ValueError, TypeError, KeyError) as e:
                raise DataError(f'invalid or out-of-order tick at CSV line {line}') from e
            previous = t.time
            yield t


def source_fingerprint():
    root = Path(__file__).resolve().parent.parent
    return digest({name:file_sha(root/name) for name in
                   ('config_schema.py','indicators.py','regime.py','setups.py','scoring.py',
                    'sessions.py','risk_manager.py','protection.py','trade_management.py')})


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


class Bars:
    def __init__(self, seconds):
        self.seconds = seconds
        self.closed = deque(maxlen=400)
        self.current = None
        self.discard_first = True  # input may start halfway through a candle

    def push(self, tick):
        bucket = int(tick.time.timestamp()) // self.seconds * self.seconds
        if self.current is None or self.current[0] != bucket:
            if self.current is not None:
                if not self.discard_first:
                    self.closed.append(tuple(self.current))
                self.discard_first = False
            self.current = [bucket,tick.bid,tick.bid,tick.bid,tick.bid]
        else:
            self.current[2] = max(self.current[2],tick.bid)
            self.current[3] = min(self.current[3],tick.bid)
            self.current[4] = tick.bid

    def array(self):
        return np.array(list(self.closed), dtype=DTYPE)


class HistoricalRisk(RiskManager):
    def _trading_day(self):
        return (self.clock + timedelta(hours=self.config.broker_utc_offset_hours)).date().isoformat()


def check_manifest(cfg, manifest):
    if manifest.get('schema_version') != 1 or manifest.get('data_origin') not in ('historical_ticks','synthetic'):
        raise DataError('manifest requires schema_version 1 and declared data_origin')
    if not isinstance(manifest.get('account_currency'), str) or not manifest['account_currency']:
        raise DataError('manifest account_currency required')
    if not number(manifest.get('starting_balance')) or manifest['starting_balance'] <= 0:
        raise DataError('positive starting_balance required')
    offset = manifest.get('broker_utc_offset_hours')
    if not number(offset) or not -14 <= offset <= 14:
        raise DataError('explicit historical broker UTC offset required; split datasets at offset changes')
    if manifest.get('swap_policy') != 'explicit_events' or not isinstance(manifest.get('swaps'), list):
        raise DataError('declare swap_policy explicit_events and a swaps list, even if empty')
    for symbol in cfg['symbols']:
        costs = manifest.get('symbols',{}).get(symbol,{})
        for key in ('contract_size','volume_min','volume_step','volume_max'):
            if not number(costs.get(key)) or costs[key] <= 0:
                raise DataError(f'{symbol}: positive {key} required')
        if costs['volume_min'] > costs['volume_max']:
            raise DataError('volume_min exceeds volume_max')
        for key in ('commission_per_lot_side','fee_per_lot_side','slippage_price'):
            if not number(costs.get(key)) or costs[key] < 0:
                raise DataError(f'{symbol}: explicit nonnegative {key} required')
    for event in manifest['swaps']:
        stamp(event.get('timestamp'))
        if event.get('symbol') not in cfg['symbols'] or any(not number(event.get(k)) for k in ('long_per_lot','short_per_lot')):
            raise DataError('invalid signed swap event')


class Replay:
    def __init__(self,cfg,manifest,start,end,version):
        self.cfg,self.manifest = copy.deepcopy(cfg),copy.deepcopy(manifest)
        self.start,self.end = stamp(start),stamp(end)
        if self.start >= self.end:
            raise DataError('replay start must precede end')
        self.norm = schema.normalise(cfg)
        problems = schema.check_safety(cfg,self.norm)
        if problems:
            raise DataError('; '.join(problems))
        check_manifest(cfg,manifest)
        configured_offset = schema.broker_offset(cfg)
        if configured_offset is not None and configured_offset != manifest['broker_utc_offset_hours']:
            raise DataError('historical broker offset disagrees with config')
        pc = schema.protection_config(cfg)
        if pc.max_concurrent_trades != 1:
            raise DataError('v1 replay supports one concurrent position/order only')
        # Historical calendar windows must be explicit; malformed ones cannot
        # be silently skipped as a clean-news observation.
        for w in pc.news_blackout_windows:
            stamp(w['start'])
            if 'end' in w: stamp(w['end'])
        self.guard = protection.Protection(pc)
        self.risk = HistoricalRisk(schema.risk_config(cfg,self.norm,manifest['broker_utc_offset_hours']))
        self.session = sessions.from_config(cfg)
        self.rc,self.sc = schema.regime_config(cfg),schema.setup_config(cfg)
        self.management = schema.management_config(cfg)
        self.regime_map = schema.regime_map(cfg) or {
            regime.TREND_UP:(setups.TREND_PULLBACK,setups.BREAKOUT_RETEST),
            regime.TREND_DOWN:(setups.TREND_PULLBACK,setups.BREAKOUT_RETEST),
            regime.RANGE:(setups.RANGE_REVERSION,)}
        self.enabled = schema.enabled_setups(cfg)
        if not self.enabled:
            raise DataError('no enabled setups')
        self.version = version
        self.balance = float(manifest['starting_balance'])
        self.equity_peak = self.balance
        self.max_equity_drawdown = 0.
        self.observed_symbols = set()
        self.bars = {s:{role:Bars(schema.TIMEFRAME_SECONDS[tf]) for role,tf in
                     [('regime',self.norm.tf_regime),('setup',self.norm.tf_setup),('entry',self.norm.tf_entry)]}
                     for s in self.norm.symbols}
        self.last_poll,self.cursor,self.last_tick = {},{},{}
        self.atr_history = {s:deque(maxlen=50) for s in self.norm.symbols}
        self.pending = self.position = None
        self.records,self.events = [],Counter()
        self.swaps = sorted(manifest['swaps'],key=lambda e:stamp(e['timestamp']))
        self.swap_index = 0
        self.sequence = 0
        self.observed_start = self.observed_end = None

    def floating(self,tick=None):
        if not self.position:
            return 0.
        p = self.position
        t = tick if tick is not None and tick.symbol == p['symbol'] else self.last_tick[p['symbol']]
        quote = t.bid if p['side']=='buy' else t.ask
        return self.gross(p,quote,t.quote_to_account_rate)

    def gross(self,p,exit_price,conversion):
        sign = 1 if p['side']=='buy' else -1
        return sign*(exit_price-p['entry'])*p['lots']*self.manifest['symbols'][p['symbol']]['contract_size']*conversion

    def charge_swaps(self,now):
        while self.swap_index < len(self.swaps) and stamp(self.swaps[self.swap_index]['timestamp']) <= now:
            e = self.swaps[self.swap_index]; self.swap_index += 1
            if self.position and e['symbol']==self.position['symbol'] and stamp(e['timestamp']) > self.position['opened_at']:
                rate = e['long_per_lot'] if self.position['side']=='buy' else e['short_per_lot']
                amount = rate*self.position['lots']
                self.position['swap'] += amount
                self.balance += amount

    def close(self,tick,price,reason):
        p = self.position; costs = self.manifest['symbols'][p['symbol']]
        gross = self.gross(p,price,tick.quote_to_account_rate)
        commission = -2*costs['commission_per_lot_side']*p['lots']
        fees = -2*costs['fee_per_lot_side']*p['lots']
        # Entry costs and swaps were already charged to balance.
        self.balance += gross + commission/2 + fees/2
        net = gross+commission+fees+p['swap']
        r = {'event':'trade_closed','schema_version':1,'trade_id':p['trade_id'],
             'signal_id':p['trade_id']+'-signal','broker_ids':{'orders':[],'deals':[],'position':''},
             'execution_mode':'paper','account_type':'demo','account_currency':self.manifest['account_currency'],
             'strategy_version':self.version,'data_origin':'synthetic' if self.manifest['data_origin']=='synthetic' else 'historical_replay',
             'symbol':p['symbol'],'side':p['side'],'opened_at':p['opened_at'].isoformat(),
             'closed_at':tick.time.isoformat(),'entry_avg':p['entry'],'exit_avg':price,'lots':p['lots'],
             'fills':[{'ts':p['opened_at'].isoformat(),'price':p['entry'],'lots':p['lots']}],
             'exits':[{'ts':tick.time.isoformat(),'price':price,'lots':p['lots'],'reason':reason}],
             'stop_changes':p['stop_changes'],'gross_pnl':gross,'commission':commission,
             'swap':p['swap'],'fees':fees,'net_pnl':net,'context':p['context'],
             'outcome_status':'resolved','missing_fields':[], 'simulated':True}
        self.records.append(r)
        self.guard.record_trade_closed(net,p['context']['session'],now=tick.time)
        self.position = None
        self.events['closed'] += 1

    def execute(self,tick):
        if self.pending and tick.time > self.pending['expires']:
            self.pending = None; self.events['expired'] += 1
        if self.pending and tick.symbol==self.pending['symbol']:
            p = self.pending; buy = p['side']=='buy'
            if (buy and tick.ask >= p['requested_entry']) or (not buy and tick.bid <= p['requested_entry']):
                costs = self.manifest['symbols'][tick.symbol]
                p['entry'] = (max(tick.ask,p['requested_entry'])+costs['slippage_price'] if buy
                              else min(tick.bid,p['requested_entry'])-costs['slippage_price'])
                p['opened_at'] = tick.time
                p['context']['spread_at_entry'] = tick.ask-tick.bid
                p['swap'],p['stop_changes'] = 0.,[]
                self.balance -= (costs['commission_per_lot_side']+costs['fee_per_lot_side'])*p['lots']
                self.position,self.pending = p,None
                self.events['filled'] += 1
        if self.position and tick.symbol==self.position['symbol']:
            p = self.position; buy = p['side']=='buy'
            quote = tick.bid if buy else tick.ask
            slip = self.manifest['symbols'][tick.symbol]['slippage_price']
            if (buy and quote <= p['stop']) or (not buy and quote >= p['stop']):
                self.close(tick,quote-slip if buy else quote+slip,'sl')
            elif (buy and quote >= p['target']) or (not buy and quote <= p['target']):
                self.close(tick,p['target'],'tp')

    def manage(self,tick):
        if self.position and tick.symbol==self.position['symbol']:
            p = self.position
            quote = tick.bid if p['side']=='buy' else tick.ask
            stop,reason = trade_management.next_stop(p['side'],p['entry'],p['original_stop'],p['stop'],quote,self.management)
            if reason:
                p['stop'] = stop
                p['stop_changes'].append({'ts':tick.time.isoformat(),'price':stop,'reason':reason})

    def signal(self,tick,session_key):
        arrays = {role:b.array() for role,b in self.bars[tick.symbol].items()}
        minimums = {'regime':max(60,self.rc.ema_slow,2*self.rc.adx_period+1),
                    'setup':max(60,self.sc.ema_slow),
                    'entry':max(self.sc.atr_period+1,2*self.sc.swing_lookback+self.sc.trigger_bars+1)}
        if any(len(a) < minimums[role] for role,a in arrays.items()):
            self.events['warmup'] += 1; return
        m15,h1,m5 = arrays['setup'],arrays['regime'],arrays['entry']
        bar = int(m15['time'][-1])
        if self.cursor.get(tick.symbol)==bar:
            return
        self.cursor[tick.symbol] = bar
        self.events['evaluations'] += 1
        pip,_ = schema.pip_for(self.cfg,tick.symbol)
        spread = tick.ask-tick.bid
        self.guard.observe_spread(spread)
        gate = self.guard.check(session_key=session_key,open_positions=int(bool(self.position or self.pending)),
                                spread_pips=spread/pip,last_candle_time=bar,now=tick.time,connected=True)
        if not gate:
            self.events['gate_rejected'] += 1; return
        current = regime.classify(h1,self.rc)
        if not current.is_clear:
            self.events['regime_rejected'] += 1; return
        atr15 = float(indicators.atr(m15,self.rc.atr_period)[-1])
        atr5 = float(indicators.atr(m5,self.rc.atr_period)[-1])
        if not number(atr15) or not number(atr5):
            self.events['invalid_indicators'] += 1; return
        for name in self.regime_map.get(current.state,()):
            if name not in self.enabled:
                continue
            candidate,_ = setups.detect(name,m15,m5,current,self.sc)
            if candidate is None:
                continue
            history = self.atr_history[tick.symbol]; history.append(atr5)
            points,reason = regime.regime_score(current,self.rc)
            score = scoring.build(regime_points=points,regime_reason=reason,candidate=candidate,
                     setup_class=candidate.setup_class,spread=spread,median_spread=self.guard.median_spread(),
                     max_spread=self.guard.cfg.max_spread_pips*pip,atr_m5=atr5,median_atr_m5=statistics.median(history),
                     news_clear=True,data_healthy=True,min_score=schema.min_score(self.cfg))
            if not self.guard.check_spread(spread,atr5) or not self.guard.check_stop(candidate.risk_distance,spread,atr15,pip):
                self.events['execution_rejected'] += 1; return
            if not score.executable:
                self.events['score_rejected'] += 1; return
            self.submit(tick,candidate,session_key,atr15,current.state)
            return
        self.events['no_setup'] += 1

    def submit(self,tick,candidate,session_key,atr15,market_regime):
        costs = self.manifest['symbols'][tick.symbol]
        pip,_ = schema.pip_for(self.cfg,tick.symbol)
        try:
            lots = self.risk.position_size(self.balance+self.floating(tick),candidate.entry_price,candidate.stop_price,
                   pip*costs['contract_size']*tick.quote_to_account_rate,pip,
                   costs['volume_min'],costs['volume_step'],costs['volume_max'])
        except ValueError:
            self.events['size_rejected'] += 1; return
        self.sequence += 1
        self.pending = {'trade_id':digest([self.version,self.start.isoformat(),self.sequence,tick.symbol,tick.time.isoformat()]),
                        'symbol':tick.symbol,'side':candidate.direction,'lots':lots,
                        'requested_entry':candidate.entry_price,'original_stop':candidate.stop_price,
                        'stop':candidate.stop_price,'target':candidate.target_price,
                        'expires':tick.time+timedelta(seconds=candidate.expiry_bars*self.norm.entry_seconds),
                        'context':{'session':session_key,'setup':candidate.setup,'regime':market_regime,
                                   'atr':atr15,'atr_timeframe':self.norm.tf_setup,
                                   'spread_at_signal':tick.ask-tick.bid,'spread_at_entry':None,'spread_unit':'price'}}
        self.guard.record_trade_opened(session_key)
        self.events['submitted'] += 1

    def feed(self,tick):
        if tick.symbol not in self.bars:
            raise DataError('tick symbol absent from strategy: '+tick.symbol)
        if self.manifest['symbols'][tick.symbol]['slippage_price'] >= tick.bid:
            raise DataError('slippage assumption would produce a nonpositive price')
        for b in self.bars[tick.symbol].values():
            b.push(tick)
        self.last_tick[tick.symbol] = tick
        if not self.start <= tick.time < self.end:
            return
        self.observed_symbols.add(tick.symbol)
        self.observed_start = self.observed_start or tick.time
        self.observed_end = tick.time
        self.charge_swaps(tick.time)
        self.execute(tick)  # broker-side stops/fills run on every recorded quote
        self.risk.clock = tick.time
        equity = self.balance+self.floating(tick)
        self.equity_peak = max(self.equity_peak,equity)
        self.max_equity_drawdown = max(self.max_equity_drawdown,self.equity_peak-equity)
        self.guard.observe_equity(equity)
        halted = self.risk.check_kill_switch(equity)
        previous = self.last_poll.get(tick.symbol)
        if previous is not None and (tick.time-previous).total_seconds() < self.norm.poll_seconds:
            return
        self.last_poll[tick.symbol] = tick.time
        self.manage(tick)
        key = self.session.session_key(tick.time)
        if not halted and key is not None:
            self.signal(tick,key)

    def finish(self):
        # No forced sale at a future price, no silently discarded open loss.
        open_exposure = None
        if self.position:
            open_exposure = {'trade_id':self.position['trade_id'],'symbol':self.position['symbol'],
                             'unrealized_pnl':self.floating(),'opened_at':self.position['opened_at'].isoformat()}
        return {'engine_version':ENGINE_VERSION,'strategy_version':self.version,
                'window':{'start':self.start.isoformat(),'end':self.end.isoformat()},
                'configuration_sha256':digest(self.cfg),'assumptions_sha256':digest(self.manifest),
                'starting_balance':self.manifest['starting_balance'],'ending_balance':self.balance,
                'max_equity_drawdown':self.max_equity_drawdown,
                'data_quality_issues':['no evaluation-window quotes for '+s for s in self.norm.symbols if s not in self.observed_symbols],
                'open_exposure':open_exposure,'pending_at_boundary':bool(self.pending),
                'complete_closed_outcomes':open_exposure is None,
                'observed_start':self.observed_start.isoformat() if self.observed_start else None,
                'observed_end':self.observed_end.isoformat() if self.observed_end else None,
                'events':dict(self.events),'metrics':metrics(self.records),'trades':self.records,
                'assumptions':self.manifest,
                'limitations':['Full fills only; liquidity, rejects, partial fills, and network latency are not simulated.',
                               'Stops use the next recorded bid/ask plus declared adverse slippage; targets fill at their limit.',
                               'Gaps remain gaps; missing ticks can change fills and signals.',
                               'Indicators use bid bars aggregated on UTC boundaries, with the first partial bar discarded.',
                               'Only declared historical news blackout windows and swap events are enforced.',
                               'One concurrent trade; management polls on available ticks, not a terminal wall-clock timer.',
                               'An open position at the boundary disqualifies the run from candidate selection.']}


def run_replay(path,cfg,manifest,start,end,version=None):
    source_sha = source_fingerprint()
    version = version or 'source-'+source_sha[:16]+'-config-'+digest(cfg)
    engine = Replay(cfg,manifest,start,end,version)
    for tick in ticks(path):
        if tick.time >= engine.end:
            break
        engine.feed(tick)
    result = engine.finish()
    result['strategy_source_sha256'] = source_sha
    result['engine_source_sha256'] = file_sha(__file__)
    result['data_sha256'] = file_sha(path)
    result['run_id'] = digest({k:result[k] for k in ('engine_version','engine_source_sha256','strategy_version','configuration_sha256','window','data_sha256','assumptions_sha256')})
    return result
