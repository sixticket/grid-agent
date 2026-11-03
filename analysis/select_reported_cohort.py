"""Select the paper's 96 included trajectories from external immutable snapshots."""
import argparse
from collections import Counter
from pathlib import Path
try:
    from .common import BASE_SNAPSHOT, WEATHER_SNAPSHOT, REPORTED_SNAPSHOT, evidence_root, output_root, identity, validate_cohort, read_json, write_json, sha, paper_data
except ImportError:
    from common import BASE_SNAPSHOT, WEATHER_SNAPSHOT, REPORTED_SNAPSHOT, evidence_root, output_root, identity, validate_cohort, read_json, write_json, sha, paper_data

def select(base, weather):
    runs = [r for r in base['runs'] if r['deployment'] == 'claude_opus'] + weather['runs']
    validate_cohort(runs)
    return paper_data(dict(schema_version=1, snapshot_revision='reported96_20261006',
        extraction_cutoff_utc=weather['extraction_cutoff_utc'], selection_policy=weather['selection_policy'],
        models={'sol_6_1': 'gpt-6.1-sol', 'claude_opus': 'claude-opus-5-5', 'opus_5_5': 'claude-opus-5-5'},
        runs=runs, summary={**{k:v for k,v in base['summary'].items() if k.startswith('claude_opus')}, **weather['summary']},
        scope='96 year-matched trajectories; 48 per deployment; 32 per year; 48 per architecture'))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root', type=Path, default=evidence_root())
    parser.add_argument('--base', type=Path, help='Override baseline results snapshot')
    parser.add_argument('--weather', type=Path, help='Override hosted80 results snapshot')
    parser.add_argument('--out', type=Path, default=output_root())
    args = parser.parse_args()
    base_path = args.base or args.evidence_root / BASE_SNAPSHOT
    weather_path = args.weather or args.evidence_root / WEATHER_SNAPSHOT
    base, weather = read_json(base_path), read_json(weather_path)
    data = select(base, weather)
    data['source_snapshots'] = {'base2023': {'sha256': sha(base_path)}, 'completed_weather80': {'sha256': sha(weather_path)}}
    folder = args.out / REPORTED_SNAPSHOT.parent
    write_json(folder / 'results.json', data)
    controls = dict(data, runs=[r for r in data['runs'] if r['year'] == 2023], summary={k:v for k,v in data['summary'].items() if k.endswith('_2023')})
    write_json(folder / 'base2023_results.json', controls)
    write_json(folder / 'source_manifest.json', dict(reported=96, sources=data['source_snapshots'],
        included_case_ids=[identity(r) for r in data['runs']],
        excluded_earlier_case_ids=[identity(r) for r in base['runs'] if r['deployment'] == 'gpt_sol'],
        original_experiment_records_unchanged=True, active_results_sha256=sha(folder/'results.json'),
        base2023_results_sha256=sha(folder/'base2023_results.json'),
        case_identity_rule='Model / demand year / region / architecture; earlier raw source_case_id omits model.',
        source_case_references=[dict(case_id=identity(r), source_case_id=r['case_id'], deployment=r['deployment'], model=r['model']) for r in data['runs']]))
    print('Selected 96 year-matched trajectories into ' + str(folder))

if __name__ == '__main__':
    main()
