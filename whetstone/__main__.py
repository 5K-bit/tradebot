"""Run with python -m whetstone --help. No command modifies Lathe."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analysis import call_claude, prompt, proposals
from .core import DataError, build_summary, markdown, read_json, write_json
from .validation import check_evidence, make_plan


def destination(directory, inputs=()):
    out = Path(directory).resolve()
    names = ('summary.json','report.md','claude_prompt.txt','proposals.json',
             'validation_plan.json','validation_result.json','synthetic_trades.jsonl')
    targets = [(out/name).resolve() for name in names]
    if any(p.parent != out for p in targets):
        raise DataError('output symlinks may not escape the output directory')
    if any(Path(p).resolve() in targets for p in inputs if p):
        raise DataError('output would overwrite an input file')
    out.mkdir(parents=True, exist_ok=True)
    return out


def review(input_path, start, end, minimum, out):
    summary = build_summary(input_path, start, end, minimum)
    write_json(out/'summary.json', summary)
    (out/'report.md').write_text(markdown(summary), encoding='utf-8')
    (out/'claude_prompt.txt').write_text(prompt(summary), encoding='utf-8')
    return summary


def demo_rows():
    """Clearly marked synthetic fixtures, never evidence of Lathe performance."""
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(140):
        opened = start + timedelta(hours=i)
        gross = 12. if i % 3 else -15.
        yield {'event':'trade_closed','schema_version':1,'trade_id':f'synthetic-{i}',
               'signal_id':f'synthetic-signal-{i}', 'data_origin':'synthetic',
               'execution_mode':'live','account_type':'demo', 'account_currency':'USD',
               'strategy_version':'synthetic-example-v1','symbol':'EURUSD' if i % 2 else 'USDJPY',
               'side':'buy','outcome_status':'resolved','missing_fields':['context.spread_at_entry'],
               'opened_at':opened.isoformat(),'closed_at':(opened+timedelta(minutes=30)).isoformat(),
               'gross_pnl':gross,'commission':-.8,'swap':-.1,'fees':-.1,'net_pnl':gross-1.,
               'context':{'session':str(opened.date()),'setup':'trend_pullback',
                          'spread_at_entry':None,'spread_at_signal':(.0001 if i % 2 else .01)*(1+i%3),
                          'spread_unit':'price'}}


def parser():
    p = argparse.ArgumentParser(prog='whetstone', description='Read-only review. Never places trades or changes Lathe.')
    commands = p.add_subparsers(dest='command', required=True)
    r = commands.add_parser('review', help='write statistics, Markdown report, and Claude prompt')
    r.add_argument('--input', required=True)
    r.add_argument('--week', help='UTC Monday YYYY-MM-DD; one week, end excluded')
    r.add_argument('--start', help='timezone-aware ISO8601')
    r.add_argument('--end', help='timezone-aware ISO8601, excluded')
    r.add_argument('--minimum-sample', type=int, default=30)
    r.add_argument('--out', required=True)
    a = commands.add_parser('analyze', help='send aggregate stats to Claude; requires ANTHROPIC_API_KEY')
    a.add_argument('--summary', required=True); a.add_argument('--model', required=True)
    a.add_argument('--out', required=True)
    i = commands.add_parser('import-proposals', help='validate a manually obtained Claude JSON response')
    i.add_argument('--summary', required=True); i.add_argument('--response', required=True)
    i.add_argument('--out', required=True)
    plan = commands.add_parser('plan', help='register one proposal and fixed evidence windows/criteria')
    plan.add_argument('--proposals', required=True); plan.add_argument('--proposal-id', required=True)
    for name in ('holdout-start','holdout-end','forward-start','forward-end'):
        plan.add_argument('--'+name, required=True)
    plan.add_argument('--max-drawdown', type=float, required=True, help='money in the cohort account currency')
    plan.add_argument('--minimum-trades', type=int, default=30)
    plan.add_argument('--out', required=True)
    v = commands.add_parser('validate', help='check exported backtest/demo evidence; never approve deployment')
    v.add_argument('--plan', required=True); v.add_argument('--holdout', required=True)
    v.add_argument('--forward', required=True); v.add_argument('--out', required=True)
    demo = commands.add_parser('demo', help='generate a synthetic example and report, no broker/API')
    demo.add_argument('--out', required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        sources = [getattr(args,k,None) for k in ('input','summary','response','proposals','plan','holdout','forward')]
        out = destination(args.out, sources)
        if args.command == 'review':
            if args.week:
                if args.start or args.end:
                    raise DataError('use either --week or --start/--end')
                start = datetime.strptime(args.week, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                if start.weekday() != 0:
                    raise DataError('--week must be a Monday')
                args.start, args.end = start.isoformat(), (start+timedelta(days=7)).isoformat()
            if not args.start or not args.end:
                raise DataError('supply --week or both --start and --end')
            summary = review(args.input,args.start,args.end,args.minimum_sample,out)
            print(f"Whetstone reviewed {summary['included_trades']} eligible trades. Report: {out/'report.md'}")
        elif args.command == 'demo':
            source = out/'synthetic_trades.jsonl'
            source.write_text(''.join(json.dumps(r)+'\n' for r in demo_rows()), encoding='utf-8')
            review(source,'2026-01-05T00:00:00+00:00','2026-01-12T00:00:00+00:00',30,out)
            print(f'SYNTHETIC DEMONSTRATION ONLY. Report: {out/"report.md"}')
        elif args.command == 'analyze':
            write_json(out/'proposals.json',call_claude(read_json(args.summary),args.model))
            print(f'Unapproved hypotheses: {out/"proposals.json"}')
        elif args.command == 'import-proposals':
            write_json(out/'proposals.json',proposals(read_json(args.response),read_json(args.summary)))
            print(f'Unapproved hypotheses: {out/"proposals.json"}')
        elif args.command == 'plan':
            plan = make_plan(read_json(args.proposals),args.proposal_id,args.holdout_start,args.holdout_end,
                             args.forward_start,args.forward_end,args.max_drawdown,args.minimum_trades)
            write_json(out/'validation_plan.json',plan)
            print(f'Registered plan {plan["plan_id"]}. Preserve this file before collecting evidence.')
        elif args.command == 'validate':
            result = check_evidence(read_json(args.plan),read_json(args.holdout),read_json(args.forward))
            write_json(out/'validation_result.json',result)
            print(result['status'] + '; human approval required; no Lathe changes made')
            return 2 if result['issues'] else 0
        return 0
    except (DataError, OSError, ValueError, KeyError, TypeError) as e:
        print('Whetstone: ' + str(e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
