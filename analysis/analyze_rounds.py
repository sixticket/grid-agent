"""Read-only reconstruction of the paper's 96 trajectories; no simulator/API imports."""
import sys
sys.dont_write_bytecode = True
import hashlib, json, statistics
from collections import Counter
from pathlib import Path

import argparse
from common import evidence_root, output_root, add_archive_arguments, resolver_from_args, validate_cohort
PAPER = evidence_root()
OUT = output_root() / 'round_analysis'
import committed_evidence as ce

EPS = 1e-6  # billion USD/year: $1,000/year; suppress numerical ties only.
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def key(d): return json.dumps(d, sort_keys=True, separators=(',', ':'))
def dump(p, v): p.write_text(json.dumps(v, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
def stats(rows):
    ev = [e for r in rows for e in r['evaluations']]
    return dict(n=len(rows), evaluations=len(ev), accepted_evaluations=sum(e['passes'] for e in ev),
        rejected_evaluations=sum(not e['passes'] for e in ev),
        first_candidate_accepted=sum(r['first_accepted_evaluation']==1 for r in rows),
        selected_first=sum(r['selected_evaluation']==1 for r in rows),
        selected_later=sum(r['selected_evaluation']>1 for r in rows),
        selected_before_last=sum(r['selected_evaluation']<r['evaluation_count'] for r in rows),
        last_design_differs_from_selected=sum(r['last_design_differs_from_selected'] for r in rows),
        cheaper_failed_events=sum(e['cheaper_than_prior_accepted_and_failed'] for e in ev),
        cheaper_failed_cases=sum(any(e['cheaper_than_prior_accepted_and_failed'] for e in r['evaluations']) for r in rows),
        cheaper_failed_after_selected_cases=sum(any(e['cheaper_than_prior_accepted_and_failed'] and e['evaluation']>r['selected_evaluation'] for e in r['evaluations']) for r in rows),
        cheaper_failed_after_selected_events=sum(e['cheaper_than_prior_accepted_and_failed'] and e['evaluation']>r['selected_evaluation'] for r in rows for e in r['evaluations']),
        improved_after_first_accepted=sum(r['improvement_after_first_accepted_billion']>EPS for r in rows),
        median_evaluations=statistics.median(r['evaluation_count'] for r in rows),
        range_evaluations=[min(r['evaluation_count'] for r in rows),max(r['evaluation_count'] for r in rows)],
        median_selected_evaluation=statistics.median(r['selected_evaluation'] for r in rows),
        selection_histogram=dict(sorted(Counter(r['selected_evaluation'] for r in rows).items())),
        system_simulation_calls=sum(r['system_simulation_calls'] for r in rows),
        verification_events=sum(len(r['probes']) for r in rows),
        solved_verification_events=sum(p['solved'] for r in rows for p in r['probes']),
        failures_by_check=dict(Counter(k for e in ev for k in e['failed_checks'])),
        cheaper_failures_by_check=dict(Counter(k for e in ev if e['cheaper_than_prior_accepted_and_failed'] for k in e['failed_checks'])))

def main():
    global PAPER, OUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root',type=Path,default=evidence_root())
    parser.add_argument('--out',type=Path,default=output_root()/'round_analysis')
    add_archive_arguments(parser)
    args=parser.parse_args();PAPER=args.evidence_root;OUT=args.out
    resolver=resolver_from_args(args)
    OUT.mkdir(parents=True, exist_ok=True)
    src = PAPER/'evidence/reported96_20261006/results.json'
    manifest = PAPER/'evidence/reported96_20261006/source_manifest.json'
    runs = json.loads(src.read_text())['runs']
    validate_cohort(runs)
    included = set(json.loads(manifest.read_text())['included_case_ids'])
    hashes = {str(p):sha(p) for p in [src,manifest,Path(__file__).with_name('committed_evidence.py')]}
    rows=[]
    for r in runs:
        cid=f"{r['model']}/{r['year']}/{r['region']}/{r['agent']}"
        assert cid in included and r['demand_year']==r['weather_year']==r['year']
        base=resolver(r['source_output']); lp=base/'log.jsonl'; fp=base/'final.json'
        hashes.update({str(p):sha(p) for p in [lp,fp]})
        final=json.loads(fp.read_text()); log=lp.read_text()
        replay=ce.replay(log,r['agent'],final); cs=replay['candidates']
        assert [c['candidate_sha256'] for c in cs]==[c['candidate_sha256'] for c in r['candidates']], cid
        selected=next(i+1 for i,c in enumerate(cs) if c['candidate_sha256']==r['selected']['candidate_sha256'])
        first=next(i+1 for i,c in enumerate(cs) if c['acceptance']['passes'])
        assert cs[selected-1]['acceptance']['passes']
        assert cs[selected-1]['all_in_cost_billion']==min(c['all_in_cost_billion'] for c in cs if c['acceptance']['passes'])
        assert cs[selected-1]['design']==final['final_design']
        assert abs(cs[selected-1]['all_in_cost_billion']-(final['final_results']['total_cost_billion']+final['final_distribution_check']['distribution_capex_billion']))<1e-8
        incumbent=None; current=None; ev=[]
        for i,c in enumerate(cs,1):
            cost=c['all_in_cost_billion']; passed=c['acceptance']['passes']; prior=incumbent
            new=passed and (incumbent is None or cost<incumbent)
            if new: incumbent=cost
            s=c['source']; lower=prior is not None and cost is not None and cost<prior-EPS
            ev.append(dict(evaluation=i, source=s, log_line=c['log_line'], candidate_sha256=c['candidate_sha256'],
                native_round_1based=s['cycle']+1 if 'cycle' in s else None,
                native_turn_logged=s.get('turn'), simulation_call=s.get('simulation'),
                system_cost_billion=c['system_cost_billion'], distribution_cost_billion=c['distribution_cost_billion'],
                all_in_cost_billion=cost, lower_bound_billion=c['all_in_cost_lower_bound_billion'], mip_gap=c['mip_gap'],
                passes=passed, failed_checks=[k for k,v in c['acceptance']['checks'].items() if not v],
                prior_accepted_cost_billion=prior, best_accepted_cost_billion=incumbent,
                cheaper_than_prior_accepted_and_failed=lower and not passed,
                apparent_saving_billion=(prior-cost) if lower and not passed else None,
                selected=i==selected, improves_incumbent=new, distribution=c['distribution'],
                design_changes={k:v for k,v in c['design'].items() if current is not None and current.get(k)!=v}))
            current=c['design']
        probes=[]; current=None
        for line,raw in enumerate(log.splitlines(),1):
            event=json.loads(raw); p=event['payload']
            if event['event']=='simulation':current=p['design']
            if event['event']!='verification':continue
            trial=dict(current,**p['changes']); res=p['results']
            matched=[i+1 for i,c in enumerate(cs) if c['log_line']>line and c['design']==trial]
            probs=ce.physical_acceptance(res,{},final)['checks']; probs.pop('distribution')
            probes.append(dict(log_line=line, native_round_1based=p['cycle']+1, changes=p['changes'],
                solved=res.get('status') in ('optimal','feasible_time_limit'), status=res.get('status'),
                system_cost_billion=res.get('total_cost_billion'), lower_bound_billion=res.get('cost_lower_bound_billion'),
                mip_gap=res.get('mip_gap'), system_checks_pass=all(probs.values()),
                failed_system_checks=[k for k,v in probs.items() if not v],
                distribution_validation='not performed by verify_design', all_in_cost_billion=None,
                later_exact_design_evaluations=matched,
                later_exact_design_accepted=any(cs[i-1]['acceptance']['passes'] for i in matched)))
        assert len(probes)==replay['logged_verifications']
        sel=ev[selected-1]
        rows.append(dict(case_id=cid, model=r['model'], year=r['year'], region=r['region'], architecture=r['agent'],
            source_log=str(lp), source_final=str(fp), evaluation_count=len(cs),
            unique_design_count=len({key(c['design']) for c in cs}),
            system_simulation_calls=replay['logged_simulations'], native_multi_rounds=r.get('cycles_run'),
            selected_evaluation=selected, selected_native_round=sel['native_round_1based'],
            selected_native_turn_logged=sel['native_turn_logged'], selected_system_simulation=sel['simulation_call'],
            first_accepted_evaluation=first, selected_cost_billion=sel['all_in_cost_billion'],
            improvement_after_first_accepted_billion=ev[first-1]['all_in_cost_billion']-sel['all_in_cost_billion'],
            last_design_differs_from_selected=cs[-1]['design']!=cs[selected-1]['design'],
            selected_design_evaluations=[i+1 for i,c in enumerate(cs) if c['design']==cs[selected-1]['design']],
            stopped_by=r['stopped_by'], api_calls=r['metrics'].get('api_calls'),
            evaluations=ev, probes=probes))
    assert len(rows)==96 and {r['case_id'] for r in rows}==included
    summary=stats(rows)
    groups={f'{model}/{arch}':stats([r for r in rows if r['model']==model and r['architecture']==arch]) for model in ['gpt-6.1-sol','claude-opus-5-5'] for arch in ['single','multi']}
    assert all(sha(Path(p))==h for p,h in hashes.items()),'Input changed during analysis'
    output=dict(summary=summary, groups=groups, cases=rows,
        method=dict(scope='Exactly the manuscript reported96 cohort; target-year weather and demand match. Qwen, GPT-6 Sol and mismatched years excluded.',
            evaluation='Chronological committed candidate assessment after system simulation or feeder-only check. Includes repeated designs and rechecks. Not an independent experiment.',
            round='Multi: native cycle + 1. Single: retain logged tool turn and simulation count separately; a shared evaluation index is used for cross-architecture plots.',
            selected='First occurrence of the archived selected candidate, cross-checked against final design and all-in cost. Equal-cost rechecks can occur later.',
            selection_mechanism='The predefined BestAccepted reporting rule retains the lowest-cost fully accepted committed candidate. Agent deliberation controls search and stop; retention alone does not prove autonomous safety judgment.',
            rejection='Failure under recorded model acceptance criteria; distinct from missing verification. All-in includes recorded distribution cost, even for failed candidates; such costs are not feasible savings.',
            cheaper='Lower recorded achieved all-in cost than the best previously accepted candidate by more than 1e-6 billion USD/year; not a statistically significant/global-optimality claim.',
            probes='Uncommitted verify_design calls run system simulation only. No feeder check or full all-in cost, so missing verification is never counted as a physical failure. Exact later commitment matches only.',
            source_validation='All 382 candidate hashes exactly reproduced from original logs. Final selected design and cost checked for all 96. Recovery histories read once from final combined log; API journal not double-counted.',
            no_new_execution=True), input_sha256=hashes)
    dump(OUT/'round_analysis.json',output)
    dump(OUT/'validation.json',dict(cases_verified=96,candidate_hashes_verified=len([e for r in rows for e in r['evaluations']]),
        source_files_unchanged=len(hashes),source_sha256=hashes,analysis_script_sha256=sha(Path(__file__))))
    print(json.dumps(dict(summary=summary,groups=groups),indent=2))

if __name__=='__main__':main()
