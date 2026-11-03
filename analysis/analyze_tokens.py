#!/usr/bin/env python3
"""Reconstruct token/role/search totals from received-response journals, offline."""
import argparse
from collections import Counter
import json
from pathlib import Path
from common import (evidence_root, output_root, add_archive_arguments, resolver_from_args,
                    validate_cohort, identity, sha, write_json, REPORTED_SNAPSHOT)

TOKENS = ('input_tokens','uncached_input_tokens','cache_read_tokens','cache_write_tokens','output_tokens','reasoning_tokens')


def normalized_tokens(row):
    counts = {key:row.get(key,0) for key in TOKENS}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError('Token counts must be nonnegative integers')
    if counts['input_tokens'] != sum(counts[key] for key in ('uncached_input_tokens','cache_read_tokens','cache_write_tokens')):
        raise ValueError('Input/cache token decomposition differs')
    if counts['reasoning_tokens'] > counts['output_tokens']:
        raise ValueError('Reasoning is already a subset of output')
    return counts


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence',type=Path,default=evidence_root()/REPORTED_SNAPSHOT)
    p.add_argument('--rounds',type=Path,default=output_root()/'round_analysis/round_analysis.json')
    p.add_argument('--out',type=Path,default=output_root()/'search_tokens')
    add_archive_arguments(p)
    args=p.parse_args(argv);resolve=resolver_from_args(args)
    runs=json.loads(args.evidence.read_text())['runs'];validate_cohort(runs)
    round_data=json.loads(args.rounds.read_text());traces={r['case_id']:r for r in round_data['cases']}
    sources={str(path):sha(path) for path in (args.evidence,args.rounds)}
    cases=[];responses=[];roles=[];seen=set()
    for run in runs:
        cid=identity(run);trace=traces[cid]
        log_path=resolve(run['source_output'])/'log.jsonl'
        ledger_path=resolve(run['api_usage_summary']['ledger_path'])
        sources.update({str(path):sha(path) for path in (log_path,ledger_path)})
        ledger=[json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        logs=[json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        mapping={}
        for number,event in enumerate(logs,1):
            payload=event['payload']
            if event['event'] in ('llm','assistant'):
                response=payload['response'] if event['event']=='llm' else payload
                mapping[response['id']]=(payload['agent'] if event['event']=='llm' else 'single_agent',number)
        ids=[row['response_id'] for row in ledger]
        if len(ids)!=len(set(ids)) or set(ids)!=set(mapping):
            raise ValueError('Response ledger and logged roles differ: '+cid)
        current=[]
        for order,row in enumerate(ledger,1):
            key=(row['provider'],row['response_id'])
            if key in seen:raise ValueError('Copied response counted twice: '+row['response_id'])
            seen.add(key)
            role,line=mapping[row['response_id']]
            record=dict(case_id=cid,model=run['model'],architecture=run['agent'],region=run['region'],year=run['year'],
                        response_order=order,response_id=row['response_id'],role=role,log_line=line,**normalized_tokens(row))
            record['total_tokens']=record['input_tokens']+record['output_tokens']
            current.append(record);responses.append(record)
        case={key:trace[key] for key in ('evaluation_count','unique_design_count','selected_evaluation','first_accepted_evaluation','selected_native_round','selected_native_turn_logged','system_simulation_calls','selected_cost_billion')}
        case.update(case_id=cid,model=run['model'],architecture=run['agent'],region=run['region'],year=run['year'],api_calls=len(current),
                    verification_calls=len(trace['probes']),failed_evaluations=sum(not e['passes'] for e in trace['evaluations']),
                    cheaper_failed_events=sum(e['cheaper_than_prior_accepted_and_failed'] for e in trace['evaluations']),
                    source_ledger=str(ledger_path),source_log=str(log_path),**{key:sum(r[key] for r in current) for key in TOKENS})
        for key in ('input_tokens','output_tokens'):
            if case[key]!=run['api_usage_summary'][key]:raise ValueError('Archived token total differs: '+cid)
        cases.append(case)
        for role in sorted({r['role'] for r in current}):
            rr=[r for r in current if r['role']==role]
            roles.append(dict(case_id=cid,model=run['model'],architecture=run['agent'],role=role,api_calls=len(rr),**{key:sum(r[key] for r in rr) for key in TOKENS}))
    totals={key:sum(c[key] for c in cases) for key in TOKENS}
    if len(cases)!=96 or len(responses)!=2257 or totals['input_tokens']!=276386026 or totals['output_tokens']!=3834189:
        raise ValueError('Reconstructed counts differ from the frozen 96-case manuscript')
    if any(sha(Path(path))!=digest for path,digest in sources.items()):raise ValueError('Source changed during token analysis')
    write_json(args.out/'analysis.json',dict(cases=cases,responses=responses,roles=roles,totals=totals,source_sha256=sources,
               definitions={'input':'Uncached input + cache read + cache write; repeated context counted per request.',
                            'output':'Includes reasoning subset; reasoning is never added twice.',
                            'recovery':'Copied histories appear once; duplicate provider/response identities are rejected.',
                            'money':'API monetary fields are omitted from paper outputs.'}))
    print(json.dumps({'status':'passed','cases':len(cases),'responses':len(responses),**totals}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
