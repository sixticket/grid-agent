#!/usr/bin/env python3
"""Verify downloaded research assets or extracted original files using SHA-256."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify(root, records, field):
    root = root.resolve()
    failed = []
    for item in records:
        name = item[field]
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            failed.append((name, 'missing or outside root'))
        elif path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            failed.append((name, 'size or SHA-256 mismatch'))
    return failed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--extracted', action='store_true', help='Check RESEARCH_FILES.json after extraction')
    args = p.parse_args()
    filename = 'RESEARCH_FILES.json' if args.extracted else 'RESEARCH_ASSETS.json'
    data = json.loads((args.root / filename).read_text())
    records = data['files' if args.extracted else 'assets']
    failures = verify(args.root, records, 'path' if args.extracted else 'name')
    for name, reason in failures:
        print(f'{name}: {reason}', file=sys.stderr)
    print(f'{len(records)} checked; {len(failures)} failures')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
