import copy
import csv
import json
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import scenarios
from setups import SetupCandidate, TREND_PULLBACK
from whetstone.core import DataError, validate_record
from whetstone.replay import Bars, Replay, Tick, ticks, run_replay
from whetstone.research import candidates, research

NOW = datetime(2026,1,15,3,30,tzinfo=timezone.utc)


@pytest.fixture
def assumptions():
    cost = {'contract_size':100000,'volume_min':.01,'volume_step':.01,'volume_max':100,
            'commission_per_lot_side':3.5,'fee_per_lot_side':.2,'slippage_price':.00001}
    return {'schema_version':1,'data_origin':'synthetic','account_currency':'USD',
            'starting_balance':10000.,'broker_utc_offset_hours':0,'swap_policy':'explicit_events',
            'swaps':[],'symbols':{'EURUSD':cost,'USDJPY':dict(cost,slippage_price=.001)}}


@pytest.fixture
def engine(config,assumptions):
    return Replay(config,assumptions,NOW.isoformat(),(NOW+timedelta(hours=3)).isoformat(),'test-version')


def quote(seconds=0,bid=1.0998,ask=1.0999,symbol='EURUSD',rate=1.):
    return Tick(NOW+timedelta(seconds=seconds),symbol,bid,ask,rate)


def candidate(side='buy'):
    return SetupCandidate(TREND_PULLBACK,side,1.1000,1.099 if side=='buy' else 1.101,
                          1.102 if side=='buy' else 1.098,1.099,20,'location',20,'trigger')


def submit(engine,side='buy'):
    t=quote(); engine.last_tick[t.symbol]=t
    engine.submit(t,candidate(side),'2026-01-14',.001,'TREND_UP')


def test_tick_validation_and_ordering(tmp_path):
    path=tmp_path/'ticks.csv'
    path.write_text('timestamp,symbol,bid,ask,quote_to_account_rate\n2026-01-15T03:30:00Z,EURUSD,1.1,1.1001,1\n')
    assert len(list(ticks(path)))==1
    with path.open('a') as f:f.write('2026-01-15T03:29:00Z,EURUSD,1.1,1.1001,1\n')
    with pytest.raises(DataError,match='out-of-order'):list(ticks(path))


@pytest.mark.parametrize('bad',['1.2,1.1,1','1.1,1.2,NaN','-1,1.2,1','1.1,1.2,0'])
def test_tick_bad_quotes_rejected(tmp_path,bad):
    path=tmp_path/'ticks.csv'
    path.write_text('timestamp,symbol,bid,ask,quote_to_account_rate\n2026-01-15T03:30:00Z,EURUSD,'+bad+'\n')
    with pytest.raises(DataError):list(ticks(path))


def test_bars_exclude_forming_and_first_partial():
    b=Bars(300)
    b.push(quote(30)); b.push(quote(301,bid=1.11,ask=1.111))
    assert len(b.closed)==0
    b.push(quote(350,bid=1.12,ask=1.121)); b.push(quote(601,bid=2.,ask=2.01))
    a=b.array()
    assert len(a)==1 and a['high'][0]==1.12 and a['close'][0]==1.12
    assert 2. not in a['high']


def test_long_fills_at_ask_plus_slippage_and_costs(engine):
    submit(engine)
    assert engine.pending['lots']==1.
    t=quote(1,bid=1.1000,ask=1.1001); engine.last_tick[t.symbol]=t; engine.execute(t)
    assert engine.position['entry']==pytest.approx(1.10011)
    assert engine.balance==pytest.approx(9996.3)
    engine.execute(quote(2,bid=1.1021,ask=1.1022))
    r,=engine.records
    assert r['exit_avg']==1.102
    assert r['gross_pnl']==pytest.approx(189.)
    assert r['commission']==-7 and r['fees']==-.4
    assert engine.balance==pytest.approx(10000+r['net_pnl'])
    assert r['context']['spread_at_entry']==pytest.approx(.0001)
    assert r['simulated'] and r['data_origin']=='synthetic'
    validate_record(r)


def test_short_fills_bid_and_stop_uses_ask(engine):
    submit(engine,'sell')
    engine.execute(quote(1,bid=1.0999,ask=1.1000))
    assert engine.position['entry']==pytest.approx(1.09989)
    engine.execute(quote(2,bid=1.10095,ask=1.10105))
    r,=engine.records
    assert r['exit_avg']==pytest.approx(1.10106)
    assert r['net_pnl']<0


def test_gap_stop_uses_observed_quote_not_stop_price(engine):
    submit(engine); engine.execute(quote(1,bid=1.1,ask=1.1001))
    engine.execute(quote(2,bid=1.097,ask=1.0971))
    assert engine.records[0]['exit_avg']==pytest.approx(1.09699)


def test_no_same_tick_fill_of_new_signal(engine,monkeypatch):
    monkeypatch.setattr(engine,'signal',lambda t,s:engine.submit(t,candidate(),s,.001,'TREND_UP'))
    engine.feed(quote(0,bid=1.1001,ask=1.1002))
    assert engine.position is None and engine.pending is not None
    engine.feed(quote(1,bid=1.1002,ask=1.1003))
    assert engine.position is not None


def test_pending_expiry_never_becomes_trade(engine):
    submit(engine)
    engine.execute(quote(901,bid=1.11,ask=1.111))
    assert engine.pending is None and engine.records==[]
    assert engine.events['expired']==1


def test_swaps_signed_and_once(engine):
    engine.swaps=[{'timestamp':quote(2).time.isoformat(),'symbol':'EURUSD','long_per_lot':-2.,'short_per_lot':1.}]
    submit(engine); engine.execute(quote(1,bid=1.1,ask=1.1001))
    engine.charge_swaps(quote(3).time); engine.charge_swaps(quote(4).time)
    engine.execute(quote(5,bid=1.1021,ask=1.1022))
    assert engine.records[0]['swap']==-2.
    assert engine.balance==pytest.approx(10000+engine.records[0]['net_pnl'])


def test_open_position_retained_at_boundary(engine):
    submit(engine); t=quote(1,bid=1.1,ask=1.1001)
    engine.last_tick[t.symbol]=t; engine.execute(t)
    r=engine.finish()
    assert not r['complete_closed_outcomes'] and r['trades']==[]
    assert r['open_exposure']['unrealized_pnl']<0


def test_historical_risk_clock(engine):
    engine.risk.clock=datetime(2021,2,3,1,tzinfo=timezone.utc)
    assert engine.risk._trading_day()=='2021-02-03'


def test_management_uses_lathe_trailing_rule(engine):
    submit(engine); engine.execute(quote(1,bid=1.1,ask=1.1001))
    engine.manage(quote(16,bid=1.1019,ask=1.1020))
    assert engine.position['stop']>engine.position['entry']
    assert engine.position['stop_changes']


def test_real_lathe_setup_modules_produce_pending_signal(engine):
    arrays={'regime':scenarios.h1_uptrend(),'setup':scenarios.m15_pullback_to_ema(),'entry':scenarios.m5_full_long_trigger()}
    for role,a in arrays.items():
        b=engine.bars['EURUSD'][role]
        times=int(NOW.timestamp())-b.seconds*np.arange(len(a),0,-1)
        b.closed=deque([(int(t),float(r['open']),float(r['high']),float(r['low']),float(r['close'])) for t,r in zip(times,a)],maxlen=400)
    last=float(arrays['entry']['close'][-1])
    t=quote(bid=last-.000001,ask=last+.000001)
    engine.last_tick['EURUSD']=t
    engine.signal(t,'2026-01-14')
    assert engine.pending is not None,engine.events
    assert engine.pending['context']['setup']==TREND_PULLBACK


def test_replay_reproducible_and_input_unchanged(tmp_path,config,assumptions):
    path=tmp_path/'ticks.csv'
    path.write_text('timestamp,symbol,bid,ask,quote_to_account_rate\n2026-01-15T03:30:00Z,EURUSD,1.1,1.1001,1\n')
    before=path.read_bytes()
    a=run_replay(path,config,assumptions,NOW.isoformat(),quote(60).time.isoformat())
    b=run_replay(path,config,assumptions,NOW.isoformat(),quote(60).time.isoformat())
    assert a==b and path.read_bytes()==before
    assert a['metrics']['trades']==0 and a['events']['warmup']==1
    assert a['engine_source_sha256'] and a['strategy_source_sha256']


def test_no_mt5_or_network_imports_in_replay():
    cmd=[sys.executable,'-c',"import sys; import whetstone.replay; assert 'MetaTrader5' not in sys.modules; assert 'mt5_connector' not in sys.modules"]
    subprocess.run(cmd,check=True,capture_output=True)


def test_missing_cost_assumption_refused(config,assumptions):
    del assumptions['symbols']['EURUSD']['fee_per_lot_side']
    with pytest.raises(DataError,match='fee_per_lot_side'):
        Replay(config,assumptions,NOW.isoformat(),quote(60).time.isoformat(),'v')


def test_candidates_one_rule_only_and_do_not_mutate(config):
    config['spread']={'max_spread_pips':2.}
    before=copy.deepcopy(config)
    variants=candidates(config,'spread.max_spread_pips',[1.,1.5,2.])
    assert len(variants)==2 and config==before
    with pytest.raises(DataError):candidates(config,'risk.risk_per_trade_pct',[.02])
    with pytest.raises(DataError):candidates(config,'spread.max_spread_pips',[3.])


def test_training_selects_before_screen_and_no_reselection(config,assumptions):
    config['spread']={'max_spread_pips':2.}; calls=[]
    def runner(path,cfg,manifest,start,end):
        value=cfg['spread']['max_spread_pips']; calls.append((value,start))
        training=start=='2026-01-01T00:00:00Z'
        expectancy={2.:1.,1.:3.,1.5:2.}[value] if training else {2.:1.,1.:0.,1.5:10.}[value]
        return {'run_id':str(value)+start,'strategy_version':'v'+str(value),'window':{'start':start,'end':end},
                'max_equity_drawdown':10.,'complete_closed_outcomes':True,'metrics':{'trades':40,'expectancy':expectancy,'closed_trade_drawdown':10.},
                'open_exposure':None}
    r,p=research('unused',config,assumptions,'spread.max_spread_pips',[1.,1.5],
                 '2026-01-01T00:00:00Z','2026-02-01T00:00:00Z','2026-02-01T00:00:00Z','2026-03-01T00:00:00Z',runner=runner)
    assert r['selected_change']['to']==1. and r['status']=='screen_failed'
    assert calls==[(2.,'2026-01-01T00:00:00Z'),(1.,'2026-01-01T00:00:00Z'),(1.5,'2026-01-01T00:00:00Z'),
                   (2.,'2026-02-01T00:00:00Z'),(1.,'2026-02-01T00:00:00Z')]
    assert p['proposals']==[]


def test_research_rejects_window_overlap(config,assumptions):
    with pytest.raises(DataError,match='disjoint'):
        research('unused',config,assumptions,'spread.max_spread_pips',[1],
                 '2026-01-01T00:00:00Z','2026-03-01T00:00:00Z','2026-02-01T00:00:00Z','2026-04-01T00:00:00Z')


def test_future_ticks_do_not_change_earlier_results(tmp_path,config,assumptions):
    path=tmp_path/'ticks.csv'
    prefix='timestamp,symbol,bid,ask,quote_to_account_rate\n2026-01-15T03:30:00Z,EURUSD,1.1,1.1001,1\n'
    path.write_text(prefix+'2026-02-01T00:00:00Z,EURUSD,1.5,1.6,1\n')
    a=run_replay(path,config,assumptions,NOW.isoformat(),quote(60).time.isoformat())
    path.write_text(prefix+'2026-02-01T00:00:00Z,EURUSD,8,9,1\n')
    b=run_replay(path,config,assumptions,NOW.isoformat(),quote(60).time.isoformat())
    assert a['events']==b['events'] and a['trades']==b['trades'] and a['metrics']==b['metrics']


def test_unknown_symbol_and_offset_mismatch_refused(engine,config,assumptions):
    with pytest.raises(DataError,match='symbol'):engine.feed(quote(symbol='GBPUSD'))
    assumptions['broker_utc_offset_hours']=3
    with pytest.raises(DataError,match='offset'):Replay(config,assumptions,NOW.isoformat(),quote(60).time.isoformat(),'v')


def test_equity_drawdown_includes_open_losses(engine):
    submit(engine)
    engine.feed(quote(1,bid=1.1000,ask=1.1001))
    engine.feed(quote(2,bid=1.0995,ask=1.0996))
    assert engine.finish()['max_equity_drawdown']>50
    assert engine.finish()['metrics']['closed_trade_drawdown']==0


def test_data_quality_disqualifies_missing_pair(engine):
    from whetstone.research import eligible
    engine.feed(quote())
    result=engine.finish()
    assert 'no evaluation-window quotes for USDJPY' in result['data_quality_issues']
    result['metrics']['trades']=40
    assert not eligible(result,30)


def test_no_model_command_or_network_adapter_remains():
    from whetstone.__main__ import parser
    import whetstone.analysis as a
    assert not hasattr(a,'call_claude')
    with pytest.raises(SystemExit):parser().parse_args(['analyze'])


def test_registered_replay_refuses_changed_source_or_config(config,assumptions):
    from whetstone.research import replay_plan
    from whetstone.core import digest
    plan={'baseline_version':'wrong','change':{'rule':'spread.max_spread_pips','from':2.,'to':1.}}
    plan['plan_id']=digest(plan)
    with pytest.raises(DataError,match='baseline differs'):
        replay_plan('unused',config,assumptions,plan,'candidate-tag')


def test_registered_replay_exports_only_the_fixed_change(config,assumptions,monkeypatch):
    import whetstone.research as module
    from whetstone.replay import source_fingerprint
    from whetstone.core import digest
    config['spread']={'max_spread_pips':2.}
    base='source-'+source_fingerprint()[:16]+'-config-'+digest(config)
    plan={'baseline_version':base,'change':{'rule':'spread.max_spread_pips','from':2.,'to':1.},
          'holdout':{'start':'2027-01-01T00:00:00Z','end':'2027-02-01T00:00:00Z'}}
    plan['plan_id']=digest(plan); seen=[]
    def run(path,cfg,manifest,start,end,version=None):
        seen.append((cfg['spread']['max_spread_pips'],version))
        return {'complete_closed_outcomes':True,'data_quality_issues':[],'trades':[], 'data_sha256':'a'*64}
    monkeypatch.setattr(module,'run_replay',run)
    result=module.replay_plan('unused',config,assumptions,plan,'candidate-tag')
    assert seen==[(2.,None),(1.,'candidate-tag')]
    assert result['candidate_version']=='candidate-tag' and result['plan_id']==plan['plan_id']
