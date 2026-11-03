"""Small, fixed one-rule search; training chooses, later screening evaluates."""
import copy
from datetime import datetime, timezone

from .core import DataError, digest, number, stamp
from .replay import run_replay

ALLOWED_RULES = {'spread.max_spread_pips', 'scoring.execution_threshold'}


def candidates(cfg,rule,values):
    if rule not in ALLOWED_RULES:
        raise DataError('v1 searches only spread.max_spread_pips or scoring.execution_threshold')
    if not isinstance(values,list) or not 1 <= len(values) <= 8 or any(not number(v) for v in values):
        raise DataError('provide 1-8 finite numeric candidates')
    section,key = rule.split('.')
    current = cfg.get(section,{}).get(key)
    if not number(current):
        raise DataError('the baseline configuration must explicitly declare the chosen rule')
    values = sorted(set(values))
    for value in values:
        if rule=='spread.max_spread_pips' and not 0 < value <= current:
            raise DataError('spread candidates may only tighten the existing ceiling')
        if rule=='scoring.execution_threshold' and not max(80,current) <= value <= 100:
            raise DataError('score candidates may only raise the existing threshold, up to 100')
    result = []
    for value in values:
        if value == current:
            continue
        changed = copy.deepcopy(cfg)
        changed[section][key] = value
        result.append(({'rule':rule,'from':current,'to':value},changed))
    if not result:
        raise DataError('provide at least one value different from baseline')
    return result


def eligible(result,minimum):
    return (result['complete_closed_outcomes'] and not result.get('data_quality_issues')
            and result['metrics']['trades'] >= minimum)


def research(path,cfg,manifest,rule,values,train_start,train_end,screen_start,screen_end,minimum=30,runner=None):
    dates = list(map(stamp,[train_start,train_end,screen_start,screen_end]))
    if not dates[0] < dates[1] <= dates[2] < dates[3]:
        raise DataError('training and screening windows must be ordered and disjoint')
    if minimum < 30:
        raise DataError('minimum sample cannot be below 30')
    variants = candidates(cfg,rule,values)
    run = runner or run_replay
    baseline = run(path,cfg,manifest,train_start,train_end)
    trials = []
    for change,changed in variants:
        result = run(path,changed,manifest,train_start,train_end)
        trials.append({'change':change,'configuration':changed,'training':result})
    # All candidates and the baseline see training only before a choice is made.
    ranked = [t for t in trials if eligible(baseline,minimum) and eligible(t['training'],minimum)
              and t['training']['metrics']['expectancy'] > max(0,baseline['metrics']['expectancy'])
              and t['training']['metrics']['closed_trade_drawdown'] <= baseline['metrics']['closed_trade_drawdown']
              and t['training']['max_equity_drawdown'] <= baseline['max_equity_drawdown']]
    ranked.sort(key=lambda t:(-t['training']['metrics']['expectancy'],
                             t['training']['metrics']['closed_trade_drawdown'],t['change']['to']))
    selection = ranked[0] if ranked else None
    screening = None
    passed = False
    if selection:
        base_screen = run(path,cfg,manifest,screen_start,screen_end)
        candidate_screen = run(path,selection['configuration'],manifest,screen_start,screen_end)
        screening = {'baseline':base_screen,'candidate':candidate_screen}
        passed = (eligible(base_screen,minimum) and eligible(candidate_screen,minimum)
                  and candidate_screen['metrics']['expectancy'] > max(0,base_screen['metrics']['expectancy'])
                  and candidate_screen['metrics']['closed_trade_drawdown'] <= base_screen['metrics']['closed_trade_drawdown']
                  and candidate_screen['max_equity_drawdown'] <= base_screen['max_equity_drawdown'])
    report = {'schema_version':1,'rule':rule,'minimum_sample':minimum,
              'training_baseline':baseline,'trials':trials,
              'selected_change':selection['change'] if selection else None,'screening':screening,
              'status':'screen_passed_hypothesis_only' if passed else 'screen_failed' if selection else 'no_candidate',
              'automatic_changes_allowed':False,
              'limitations':['Candidate selection uses training only. The later screen is not a final untouched validation set.',
                             'Repeated experiments on the same screening period can overfit it.',
                             'One split does not establish stability across market regimes; further registered walk-forward tests are required.',
                             'No risk limits are optimized. No live configuration is written.']}
    report['experiment_id'] = digest(report)
    proposal_list = []
    if passed and manifest['data_origin'] != 'synthetic':
        c = selection['change']; selected = screening['candidate']
        proposal = {'hypothesis':f"Changing {c['rule']} from {c['from']} to {c['to']} improves net expectancy without increasing closed-trade drawdown.",
                    'rationale':'Selected using training replay; passed one later chronological screen. This is a hypothesis, not proof.',
                    'falsification':'Reject if registered unseen-data expectancy does not improve, drawdown worsens, or demo-forward criteria fail.',
                    'change':c,'evidence':{'experiment_id':report['experiment_id'],
                                         'training_run':selection['training']['run_id'],'screening_run':selected['run_id']},
                    'summary_id':report['experiment_id'],
                    'cohort':{'execution_mode':'paper','account_type':'demo','currency':manifest['account_currency'],
                              'strategy_version':baseline['strategy_version'],'data_origin':'historical_replay'},
                    'evidence_trades':selected['metrics']['trades'],
                    # Every inspected period is now discovery, including screening.
                    'discovery_window':{'start':dates[0].isoformat(),'end':dates[3].isoformat()},
                    'status':'hypothesis_only'}
        proposal['proposal_id'] = digest(proposal)
        proposal_list.append(proposal)
    bundle = {'schema_version':1,'summary_id':report['experiment_id'],
              'created_at':datetime.now(timezone.utc).isoformat(),'proposals':proposal_list,
              'automatic_changes_allowed':False}
    return report,bundle


def research_markdown(report):
    rows = [('baseline',report['training_baseline'])] + [(str(t['change']['to']),t['training']) for t in report['trials']]
    lines = ['# Whetstone numerical research', '', 'Rule: `'+report['rule']+'`.', '',
             'Status: **'+report['status']+'**. No strategy changes applied.', '',
             '| Training candidate | Closed trades | Mean net/trade | Closed-trade drawdown | Open exposure |',
             '|---|---:|---:|---:|---|']
    for name,r in rows:
        m = r['metrics']
        lines.append(f"| {name} | {m['trades']} | {m['expectancy']} | {m['closed_trade_drawdown']:.2f} | {bool(r['open_exposure'])} |")
    lines += ['', 'Selected from training: `'+str(report['selected_change'])+'`.', '']
    if report['screening']:
        for arm,result in report['screening'].items():
            lines += [f"Screening {arm}: {result['metrics']['trades']} trades, mean net/trade {result['metrics']['expectancy']}, drawdown {result['metrics']['closed_trade_drawdown']:.2f}.", '']
    lines += ['## Interpretation', ''] + ['- '+s for s in report['limitations']]
    return '\n'.join(lines)+'\n'


def replay_plan(path,cfg,manifest,plan,candidate_version):
    """Run a registered holdout once, with no candidate selection on that data."""
    from .replay import ENGINE_VERSION, source_fingerprint
    payload = dict(plan); expected = payload.pop('plan_id',None)
    if expected != digest(payload):
        raise DataError('plan integrity check failed')
    actual_baseline = 'source-'+source_fingerprint()[:16]+'-config-'+digest(cfg)
    if plan['baseline_version'] != actual_baseline:
        raise DataError('registered baseline differs from current source/config; register a new plan')
    if not isinstance(candidate_version,str) or not candidate_version or candidate_version==plan['baseline_version']:
        raise DataError('provide the distinct candidate release tag/SHA intended for demo testing')
    change = plan['change']
    variants = candidates(cfg,change['rule'],[change['to']])
    actual_change,changed = variants[0]
    if actual_change != change:
        raise DataError('registered rule baseline differs from configuration')
    start,end = plan['holdout']['start'],plan['holdout']['end']
    base = run_replay(path,cfg,manifest,start,end)
    candidate = run_replay(path,changed,manifest,start,end,version=candidate_version)
    for result in (base,candidate):
        if not result['complete_closed_outcomes'] or result['data_quality_issues']:
            raise DataError('incomplete holdout: open exposure or missing symbol data')
    return {'plan_id':plan['plan_id'],'window':plan['holdout'],'change':change,
            'candidate_version':candidate_version,'engine_version':ENGINE_VERSION,
            'data_sha256':base['data_sha256'],'dataset_id':base['data_sha256'],
            'baseline':base['trades'],'candidate':candidate['trades'],
            'replay_runs':{'baseline':base,'candidate':candidate},
            'version_binding':'Candidate release name supplied by operator; verify code/config hashes against the release.'}
