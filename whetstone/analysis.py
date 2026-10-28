"""Validate human-authored numerical research proposals. No model or network."""
from datetime import datetime, timezone
from .core import DataError, digest


def verify_summary(summary):
    payload = dict(summary)
    expected = payload.pop('summary_id', None)
    if expected != digest(payload):
        raise DataError('summary integrity check failed')


def proposals(response, summary):
    verify_summary(summary)
    if not isinstance(response, dict) or not isinstance(response.get('proposals'), list):
        raise DataError('expected a proposals array')
    if len(response['proposals']) > 5:
        raise DataError('at most five proposals per report')
    validated = []
    for item in response['proposals']:
        try:
            for field in ('hypothesis','rationale','falsification'):
                if not isinstance(item[field], str) or not item[field].strip():
                    raise ValueError()
            change = item['change']
            if set(change) != {'rule','from','to'} or not isinstance(change['rule'], str) or not change['rule'].strip():
                raise ValueError()
            if change['from'] == change['to']:
                raise ValueError()
            # Nested objects/lists could conceal several rule changes.
            if any(isinstance(change[k], (dict, list)) for k in ('from','to')):
                raise ValueError()
            evidence = item['evidence']
            index = evidence['cohort_index']
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ValueError()
            if evidence.get('group') == 'unknown' or evidence.get('group', '').endswith(':unknown'):
                raise ValueError()
            c = summary['cohorts'][index]
            group = c['groups'][evidence['dimension']]
            if evidence['dimension'].startswith('spread_'):
                group = group['buckets']
            n = group[evidence['group']]['trades']
            if n < summary['minimum_sample']:
                raise DataError('proposal evidence is below the sample threshold')
            canonical = {k:item[k] for k in ('hypothesis','rationale','falsification','change','evidence')}
            canonical.update(summary_id=summary['summary_id'], cohort=c['cohort'],
                             evidence_trades=n, discovery_window=summary['window'],
                             status='hypothesis_only')
            canonical['proposal_id'] = digest(canonical)
            validated.append(canonical)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise DataError('invalid proposal or insufficient evidence') from e
    return {'schema_version':1, 'summary_id':summary['summary_id'],
            'created_at':datetime.now(timezone.utc).isoformat(), 'proposals':validated,
            'automatic_changes_allowed':False}


