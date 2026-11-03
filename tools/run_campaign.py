#!/usr/bin/env python3
"""Plan, check or sequentially execute new cases; preserve every previous attempt."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import run_case


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root', type=Path, default=Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[1] / 'data'))))
    p.add_argument('--baseline-root', type=Path, help='Default DATA_ROOT/experiment; use corrected archived packs when reproducing the study')
    p.add_argument('--baseline-pattern', default='{region}', help='Directory pattern relative to baseline root, e.g. {region}_2023')
    p.add_argument('--shared', type=Path, help='Default DATA_ROOT/experiment/shared')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--deployments', nargs='+', choices=run_case.MODELS, default=list(run_case.MODELS))
    p.add_argument('--years', nargs='+', type=int, choices=(2023, 2030, 2035), default=[2023, 2030, 2035])
    p.add_argument('--regions', nargs='+', choices=run_case.REGIONS, default=list(run_case.REGIONS))
    p.add_argument('--agents', nargs='+', choices=('single', 'multi'), default=['single', 'multi'])
    p.add_argument('--co2', action='store_true', help='Select the ten fixed carbon cases (40 trajectories by default)')
    p.add_argument('--solver', choices=('gurobi', 'highs', 'auto'), default='gurobi')
    p.add_argument('--threads', type=int, default=18)
    p.add_argument('--check-inputs', action='store_true')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--retry-failed', action='store_true', help='Create a new attempt after failure; never overwrite or replay old attempts')
    return p


def commands(args):
    root = args.data_root.expanduser().resolve()
    base = (args.baseline_root or root / 'experiment').expanduser().resolve()
    shared = (args.shared or root / 'experiment/shared').expanduser().resolve()
    output = args.output.expanduser().resolve()
    carbon_cases = {(c["region"], c["year"]) for c in run_case.read_json(Path(__file__).resolve().parents[1] / "experiments/carbon/cases.json")["cases"]}
    jobs = []
    for deployment in dict.fromkeys(args.deployments):
        for year in dict.fromkeys(args.years):
            for region in dict.fromkeys(args.regions):
                if args.co2 and (region, year) not in carbon_cases:
                    continue
                pack = base / args.baseline_pattern.format(region=region, year=year) if year == 2023 else root / 'nasa_climate/packs/MPI-ESM1-2-HR/ssp245' / str(year) / region
                for agent in dict.fromkeys(args.agents):
                    case = output / deployment / str(year) / region / agent
                    argv = ['--deployment',deployment,'--year',str(year),'--region',region,'--agent',agent,'--pack',str(pack),'--shared',str(shared),'--data-root',str(root),'--output',str(case / 'attempt_001'),'--solver',args.solver,'--threads',str(args.threads)]
                    if args.co2:
                        argv.append('--co2')
                    jobs.append({'case_id':f'{deployment}/{year}/{region}/{agent}', 'case':case, 'argv':argv})
    if not jobs:
        raise ValueError('No published cases match the requested selection')
    return jobs


def main(argv=None):
    args = parser().parse_args(argv)
    jobs = commands(args)
    if not (args.check_inputs or args.execute):
        print(json.dumps({'status':'plan_only','cases':len(jobs),'jobs':[{'case_id':j['case_id'],'plan':run_case.make_plan(run_case.parser().parse_args(j['argv']))} for j in jobs]},indent=2))
        return 0
    # Complete input validation before creating outputs or starting a provider.
    for job in jobs:
        plan = run_case.make_plan(run_case.parser().parse_args(job['argv']))
        job['inventory'] = run_case.check_inputs(plan)
    if not args.execute:
        print(json.dumps({'status':'inputs_checked','cases':len(jobs)},indent=2))
        return 0
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True,exist_ok=True)
    lock = output / 'campaign.lock'
    with lock.open('x') as stream:
        stream.write(json.dumps({'pid':os.getpid(),'started_at':dt.datetime.now(dt.timezone.utc).isoformat()}))
    report = {'schema_version':1,'status':'running','cases':len(jobs),'outcomes':[]}
    try:
        for job in jobs:
            attempts = sorted(job['case'].glob('attempt_*'))
            if attempts:
                status_path = attempts[-1] / 'status.json'
                status = run_case.read_json(status_path) if status_path.exists() else {}
                config_path = attempts[-1] / 'run_config.json'
                if status.get('status') == 'completed' and status.get('accepted') is True:
                    config = run_case.read_json(config_path)
                    expected = run_case.make_plan(run_case.parser().parse_args(job['argv']))
                    if any(config.get(k) != expected.get(k) for k in ('deployment','model','region','year','agent','profile','pack','shared','data_root','settings','output_tokens','co2_case')) or any(config.get(k) != v for k,v in job['inventory'].items()):
                        raise ValueError('Existing completed case has different settings, code or inputs: '+job['case_id'])
                    report['outcomes'].append({'case_id':job['case_id'],'status':'preserved_completed','output':str(attempts[-1])})
                    continue
                if not args.retry_failed or status.get('status') == 'running':
                    raise ValueError('Existing incomplete attempt requires inspection and --retry-failed: '+job['case_id'])
            number = max([int(p.name.split('_')[-1]) for p in attempts] or [0]) + 1
            active_output = job['case'] / f'attempt_{number:03d}'
            argv = job['argv'][:]
            argv[argv.index('--output')+1] = str(active_output)
            result = subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('run_case.py')),*argv,'--execute'],check=False)
            status = run_case.read_json(active_output / 'status.json') if (active_output / 'status.json').exists() else {'status':'failed_before_execution'}
            report['outcomes'].append({'case_id':job['case_id'],'output':str(active_output),**status})
            run_case.save(output / 'campaign.json',report)
            if result.returncode or status.get('accepted') is not True:
                report['status'] = 'needs_attention'
                return result.returncode or 1
        report['status'] = 'completed'
        return 0
    finally:
        run_case.save(output / 'campaign.json',report)
        lock.unlink()


if __name__ == '__main__':
    raise SystemExit(main())
