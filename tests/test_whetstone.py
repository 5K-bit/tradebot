import copy
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whetstone import core
from whetstone.__main__ import demo_rows, main
from whetstone.analysis import call_claude, prompt, proposals
from whetstone.validation import make_plan, check_evidence

START, END = '2026-01-05T00:00:00+00:00', '2026-01-12T00:00:00+00:00'


@pytest.fixture
def source(tmp_path):
    p = tmp_path/'trades.jsonl'
    p.write_text(''.join(json.dumps(r)+'\n' for r in demo_rows()))
    return p


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))


def summary(source):
    return core.build_summary(source, START, END)


def response():
    return {'proposals':[{'hypothesis':'A narrower spread ceiling may improve net expectancy.',
                         'rationale':'Test the observed differences; they do not establish causality.',
                         'falsification':'Reject if unseen net expectancy does not increase.',
                         'change':{'rule':'spread.max_spread_pips','from':2.,'to':1.5},
                         'evidence':{'cohort_index':0,'dimension':'symbol','group':'EURUSD'}}]}


def test_metrics_hand_calculated():
    rows = list(demo_rows())[:4]
    for r,p in zip(rows,[10,-4,-8,5]): r['net_pnl'] = p
    m = core.metrics(rows)
    assert m['net_pnl'] == 3
    assert m['expectancy'] == .75
    assert m['win_rate'] == .5
    assert m['closed_trade_drawdown'] == 12
    assert m['sample_status'] == 'insufficient'
    assert m['win_rate_interval_95'][0] < .5 < m['win_rate_interval_95'][1]


def test_drawdown_simultaneous_closes_not_ticket_order():
    rows = list(demo_rows())[:2]
    rows[0]['net_pnl'],rows[1]['net_pnl'] = -100, 100
    rows[1]['closed_at'] = rows[0]['closed_at']
    assert core.metrics(rows)['closed_trade_drawdown'] == 0


def test_unresolved_malformed_nonfinite_and_conflicts_excluded(source):
    rows = list(demo_rows())[:5]
    rows[0]['outcome_status'] = 'unresolved'; rows[0]['net_pnl'] = None
    rows[1]['net_pnl'] = float('nan')
    rows.append(dict(rows[2],net_pnl=999))
    rows.append(copy.deepcopy(rows[3]))
    write_rows(source, rows)
    with source.open('a') as f: f.write('{broken\n')
    good,bad,_ = core.load_snapshot(source)
    assert len(good) == 2
    assert bad['unresolved'] == bad['malformed_record'] == bad['conflicting_trade_id'] == 1
    assert bad['identical_duplicate'] == 1


def test_mode_account_currency_version_never_pooled(source):
    rows = list(demo_rows())[:5]
    rows[1]['execution_mode']='paper'; rows[2]['account_type']='real'
    rows[3]['account_currency']='EUR'; rows[4]['strategy_version']='other'
    write_rows(source,rows)
    assert len(summary(source)['cohorts']) == 5


def test_no_fill_spread_fallback_and_sample_counts(source):
    s = summary(source)
    c, = s['cohorts']
    assert c['overall']['trades'] == 140
    assert c['groups']['symbol']['EURUSD']['trades'] == 70
    assert set(c['groups']['spread_at_entry']['buckets']) == {'EURUSD:unknown','USDJPY:unknown'}
    assert c['groups']['spread_at_signal']['thresholds']['EURUSD']['unit'] == 'price'


def test_half_open_date_window(source):
    rows = list(demo_rows())[:2]
    rows[0]['opened_at'] = START; rows[0]['closed_at']=START
    rows[1]['closed_at']=END
    write_rows(source,rows)
    assert summary(source)['included_trades'] == 1


def test_empty_report_and_minimum_threshold(source):
    source.write_text('')
    s=summary(source)
    assert 'No eligible' in core.markdown(s)
    with pytest.raises(core.DataError): core.build_summary(source,START,END,29)


def test_net_pnl_mismatch_excluded(source):
    rows=list(demo_rows())[:1]; rows[0]['net_pnl']=100
    write_rows(source,rows)
    assert summary(source)['excluded']['net_pnl_mismatch'] == 1


def test_prompt_contains_aggregates_not_raw_ids(source):
    text=prompt(summary(source))
    assert 'synthetic-signal-1' not in text and 'synthetic-139' not in text
    assert 'trade_id' not in text and str(source) not in text
    assert 'minimum_sample' in text


def test_proposals_enforce_sample_and_single_change(source):
    s=summary(source); bundle=proposals(response(),s)
    assert bundle['automatic_changes_allowed'] is False
    assert bundle['proposals'][0]['evidence_trades'] == 70
    r=response(); r['proposals'][0]['change']['to']={'a':1,'b':2}
    with pytest.raises(core.DataError): proposals(r,s)
    r=response(); r['proposals'][0]['evidence'].update(dimension='session',group='2026-01-05')
    with pytest.raises(core.DataError): proposals(r,s)


def test_tampered_summary_rejected(source):
    s=summary(source); s['included_trades']=999
    with pytest.raises(core.DataError): prompt(s)


def test_claude_request_mocked_and_no_key_echo(source,monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-secret')
    s=summary(source)
    def transport(req,timeout):
        assert req.full_url == 'https://api.anthropic.com/v1/messages'
        assert req.headers['Authorization'] == 'Bearer test-secret'
        body=json.loads(req.data)
        assert body['model']=='explicit-test-model'
        assert 'synthetic-signal-1' not in body['messages'][0]['content']
        return io.BytesIO(json.dumps({'stop_reason':'end_turn','content':[{'type':'text','text':json.dumps(response())}]}).encode())
    result=call_claude(s,'explicit-test-model',transport)
    assert result['model']=='explicit-test-model'
    assert 'test-secret' not in json.dumps(result)


def test_claude_missing_key_and_truncation_fail_closed(source,monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    with pytest.raises(core.DataError,match='missing'): call_claude(summary(source),'test')
    monkeypatch.setenv('ANTHROPIC_API_KEY','test')
    with pytest.raises(core.DataError,match='incomplete'):
        call_claude(summary(source),'test',lambda *a,**k:io.BytesIO(b'{"stop_reason":"max_tokens"}'))


def make_test_plan(source):
    rows=list(demo_rows())
    for r in rows: r['data_origin']='journal'
    write_rows(source,rows)
    bundle=proposals(response(),summary(source)); p=bundle['proposals'][0]
    return make_plan(bundle,p['proposal_id'],'2027-01-01T00:00:00+00:00','2027-02-01T00:00:00+00:00',
                     '2090-01-01T00:00:00+00:00','2090-02-01T00:00:00+00:00',1000)


def evidence(plan,stage):
    export={'plan_id':plan['plan_id'],'candidate_version':'candidate-2','window':plan[stage],
            'change':plan['change'],'dataset_id':stage,'engine_version':'test-replay-v1',
            'data_sha256':'a'*64}
    for arm in ('baseline','candidate') if stage=='holdout' else ('candidate',):
        rows=list(demo_rows())[:40]
        year=2027 if stage=='holdout' else 2090
        for i,r in enumerate(rows):
            r['trade_id']=f'{stage}-{arm}-{i}'
            r['data_origin']='journal'
            r['opened_at']=r['opened_at'].replace('2026',str(year))
            r['closed_at']=r['closed_at'].replace('2026',str(year))
            r['strategy_version']=plan['baseline_version'] if arm=='baseline' else 'candidate-2'
            r['gross_pnl']=10 if arm=='baseline' else 12
            r['net_pnl']=r['gross_pnl']-1
        export[arm]=rows
    return export


def test_validation_pass_still_requires_human(source):
    p=make_test_plan(source)
    result=check_evidence(p,evidence(p,'holdout'),evidence(p,'forward'),datetime(2091,1,1,tzinfo=timezone.utc))
    assert result['issues']==[]
    assert result['status']=='evidence_checks_passed_pending_human_review'
    assert result['human_approval_required'] and not result['automatic_changes_allowed']


@pytest.mark.parametrize('fault',['paper','version','window','sample','net','duplicate','synthetic','future','change'])
def test_gate_blocks_bad_evidence(source,fault):
    p=make_test_plan(source); h=evidence(p,'holdout'); f=evidence(p,'forward')
    now=datetime(2091,1,1,tzinfo=timezone.utc)
    if fault=='paper': f['candidate'][0]['execution_mode']='paper'
    if fault=='version': f['candidate_version']='changed'
    if fault=='window': f['candidate'][0]['opened_at']=START
    if fault=='sample': f['candidate']=f['candidate'][:29]
    if fault=='net': f['candidate'][0]['net_pnl']=999
    if fault=='duplicate': f['candidate'][0]['trade_id']=h['candidate'][0]['trade_id']
    if fault=='synthetic': f['candidate'][0]['data_origin']='synthetic'
    if fault=='future': now=datetime(2028,1,1,tzinfo=timezone.utc)
    if fault=='change': f['change']={'rule':'different'}
    assert check_evidence(p,h,f,now)['status']=='blocked'


def test_registration_rejects_leakage_and_synthetic(source):
    bundle=proposals(response(),summary(source)); pid=bundle['proposals'][0]['proposal_id']
    with pytest.raises(core.DataError,match='synthetic'):
        make_plan(bundle,pid,'2027-01-01T00:00:00+00:00','2027-02-01T00:00:00+00:00',
                  '2090-01-01T00:00:00+00:00','2090-02-01T00:00:00+00:00',1000)
    with pytest.raises(core.DataError,match='disjoint'):
        make_plan(bundle,pid,START,END,'2090-01-01T00:00:00+00:00','2090-02-01T00:00:00+00:00',1000)


def test_cli_review_preserves_input_and_outputs_report(source,tmp_path):
    before=source.read_bytes(); out=tmp_path/'report'
    assert main(['review','--input',str(source),'--week','2026-01-05','--out',str(out)])==0
    assert source.read_bytes()==before
    assert (out/'report.md').exists() and (out/'claude_prompt.txt').exists()
    assert 'synthetic' in (out/'report.md').read_text()


def test_cli_refuses_input_overwrite(tmp_path):
    source=tmp_path/'summary.json'; source.write_text('{}')
    assert main(['review','--input',str(source),'--week','2026-01-05','--out',str(tmp_path)])==1
    assert source.read_text()=='{}'


def test_cli_demo_runs_without_mt5_or_network(tmp_path):
    assert main(['demo','--out',str(tmp_path/'demo')])==0
    assert core.read_json(tmp_path/'demo/summary.json')['included_trades']==140
