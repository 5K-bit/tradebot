"""Predeclare criteria and inspect supplied backtest/forward-test evidence.

This is an evidence gate, not a historical market simulator or deployment tool.
Export provenance remains subject to human verification.
"""
from datetime import datetime, timezone
import re

from .core import DataError, cohort_key, digest, metrics, number, stamp, validate_record


def make_plan(bundle, proposal_id, holdout_start, holdout_end, forward_start, forward_end,
              max_drawdown, minimum=30):
    if minimum < 30 or not number(max_drawdown) or max_drawdown <= 0:
        raise DataError('require minimum >= 30 and a positive monetary drawdown limit')
    found = [p for p in bundle['proposals'] if p['proposal_id'] == proposal_id]
    if len(found) != 1:
        raise DataError('proposal ID not unique/present')
    proposal = found[0]
    payload = dict(proposal); expected = payload.pop('proposal_id')
    if expected != digest(payload):
        raise DataError('proposal integrity check failed')
    dates = list(map(stamp, [holdout_start,holdout_end,forward_start,forward_end]))
    if not (stamp(proposal['discovery_window']['end']) <= dates[0] < dates[1] <= dates[2] < dates[3]):
        raise DataError('discovery, holdout, and forward windows must be ordered and disjoint')
    now = datetime.now(timezone.utc)
    if dates[2] <= now:
        raise DataError('forward testing must begin after plan registration')
    if proposal['cohort']['data_origin'] == 'synthetic':
        raise DataError('synthetic demonstration statistics cannot register a real validation plan')
    plan = {'schema_version':1, 'proposal':proposal, 'registered_at':now.isoformat(),
            'holdout':{'start':dates[0].isoformat(),'end':dates[1].isoformat()},
            'forward':{'start':dates[2].isoformat(),'end':dates[3].isoformat()},
            'criteria':{'minimum_trades_per_arm':minimum,
                        'minimum_expectancy_improvement':0.,
                        'max_closed_trade_drawdown':max_drawdown,
                        'positive_forward_expectancy':True},
            'baseline_version':proposal['cohort']['strategy_version'],
            'rollback_version':proposal['cohort']['strategy_version'],
            'change':proposal['change'], 'human_approval_required':True}
    plan['plan_id'] = digest(plan)
    return plan


def check_evidence(plan, holdout, forward, now=None):
    payload = dict(plan); expected = payload.pop('plan_id', None)
    if expected != digest(payload):
        raise DataError('plan integrity check failed')
    issues, calculated = [], {}
    seen = set()
    candidate_version = holdout.get('candidate_version')
    if not isinstance(candidate_version, str) or not candidate_version or candidate_version == plan['baseline_version']:
        issues.append('candidate must have a distinct version tag')
    if forward.get('candidate_version') != candidate_version:
        issues.append('candidate version changed between holdout and forward tests')
    target = plan['proposal']['cohort']
    now = now or datetime.now(timezone.utc)
    for stage, export in [('holdout',holdout),('forward',forward)]:
        window = plan[stage]
        if now < stamp(window['end']):
            issues.append(stage + ': registered window has not finished')
        if export.get('plan_id') != plan['plan_id'] or export.get('change') != plan['change']:
            issues.append(stage + ': plan or single-rule change mismatch')
        if export.get('window') != window:
            issues.append(stage + ': window mismatch')
        if not export.get('dataset_id') or not export.get('engine_version'):
            issues.append(stage + ': missing dataset/engine provenance')
        if not isinstance(export.get('data_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', export['data_sha256']):
            issues.append(stage + ': missing source data fingerprint')
        arms = ('baseline','candidate') if stage == 'holdout' else ('candidate',)
        stage_ids = set()
        for arm in arms:
            rows = export.get(arm)
            if not isinstance(rows, list):
                issues.append(stage + ': missing ' + arm + ' records'); continue
            valid, arm_ids = [], set()
            for r in rows:
                try:
                    validate_record(r)
                    tid = r['trade_id']
                    if tid in arm_ids or tid in seen:
                        raise DataError('duplicate or reused trade ID')
                    arm_ids.add(tid)
                    if r.get('data_origin') == 'synthetic':
                        raise DataError('synthetic evidence')
                    if not (stamp(window['start']) <= stamp(r['opened_at']) <= stamp(r['closed_at']) < stamp(window['end'])):
                        raise DataError('trade crosses registered window')
                    expected_version = plan['baseline_version'] if arm == 'baseline' else candidate_version
                    if r['strategy_version'] != expected_version or r['account_currency'] != target['currency']:
                        raise DataError('version/currency mismatch')
                    if stage == 'forward' and (r['execution_mode'] != 'live' or r['account_type'] != 'demo'):
                        raise DataError('forward evidence must contain actual demo executions')
                    valid.append(r)
                except (DataError, KeyError, TypeError) as e:
                    issues.append(stage + '/' + arm + ': ' + str(e))
            stage_ids.update(arm_ids)
            if len({cohort_key(r) for r in valid}) > 1:
                issues.append(stage + '/' + arm + ': mixed cohorts')
            m = metrics(valid, plan['criteria']['minimum_trades_per_arm'])
            calculated[stage + '_' + arm] = m
            if m['trades'] < plan['criteria']['minimum_trades_per_arm']:
                issues.append(stage + '/' + arm + ': insufficient trades')
            if m['closed_trade_drawdown'] > plan['criteria']['max_closed_trade_drawdown']:
                issues.append(stage + '/' + arm + ': drawdown limit exceeded')
        seen.update(stage_ids)
    baseline, candidate = calculated.get('holdout_baseline', {}), calculated.get('holdout_candidate', {})
    if baseline.get('expectancy') is None or candidate.get('expectancy') is None:
        issues.append('holdout expectancy unavailable')
    elif candidate['expectancy'] - baseline['expectancy'] <= plan['criteria']['minimum_expectancy_improvement']:
        issues.append('candidate did not improve holdout expectancy')
    if baseline.get('closed_trade_drawdown') is not None and candidate.get('closed_trade_drawdown') is not None:
        if candidate['closed_trade_drawdown'] > baseline['closed_trade_drawdown']:
            issues.append('candidate worsened holdout drawdown')
    if (calculated.get('forward_candidate', {}).get('expectancy') or 0) <= 0:
        issues.append('forward expectancy is not positive')
    return {'schema_version':1, 'plan_id':plan['plan_id'],
            'status':'blocked' if issues else 'evidence_checks_passed_pending_human_review',
            'issues':sorted(set(issues)), 'metrics':calculated,
            'evidence_sha256':{'holdout':digest(holdout),'forward':digest(forward)},
            'automatic_changes_allowed':False, 'human_approval_required':True,
            'limitations':['This checks supplied exports; it does not run a market backtest.',
                           'Dataset hashes and version tags are supplied provenance, not independently verified execution.',
                           'Human review must verify unseen holdout data, one-rule code diff, costs, and rollback.',
                           'Passing these thresholds is not statistical proof of an improvement.']}
