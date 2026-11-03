#!/usr/bin/env python3
"""Build or verify future-demand packs; NASA resources are added in a later step."""
import argparse
import json
from pathlib import Path
from scenarios import build, verify

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('build', help='build a new demand-only intermediate pack')
    make.add_argument('--spec', type=Path, required=True)
    make.add_argument('--output', type=Path, required=True)
    check = sub.add_parser('verify', help='validate a demand-only intermediate pack')
    check.add_argument('--pack', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'build':
        build(args.spec, args.output)
        pack = args.output
    else:
        pack = args.pack
    manifest = verify(pack)
    print(json.dumps({'status': 'passed', 'pack': str(pack.resolve()), 'scenario': manifest['scenario']}, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
