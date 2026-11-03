#!/usr/bin/env python3
"""Hash or verify an external baseline input archive without network or solver calls."""
from pathlib import Path
import argparse, hashlib, json, os

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))) / 'experiment')
    p.add_argument('--write', action='store_true', help='write a new manifest for the supplied archive')
    args = p.parse_args(argv)
    root = args.root.expanduser().resolve()
    manifest = root / 'input_manifest.json'
    def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
    if args.write:
        paths = []
        for region in ['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']:
            folder = root / region
            paths.extend(path for path in folder.iterdir() if path.is_file() and path.suffix in ['.csv','.json'])
            for sub in ['raw','profiles']:
                paths.extend(path for path in (folder / sub).rglob('*') if path.is_file())
        paths.extend(path for path in (root / 'shared').rglob('*') if path.is_file())
        manifest.write_text(json.dumps({'schema_version':1, 'files':[{'path':str(path.relative_to(root)), 'bytes':path.stat().st_size, 'sha256':digest(path)} for path in sorted(set(paths))]}, indent=2)+'\n')
    data = json.loads(manifest.read_text())
    bad = []
    for row in data['files']:
        path = (root / row['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != row['sha256']:
            bad.append(row['path'])
    if bad:
        raise ValueError('Missing, modified or out-of-root inputs: ' + ', '.join(bad))
    print(json.dumps({'status':'passed', 'files':len(data['files'])}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
