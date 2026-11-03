#!/usr/bin/env python3
"""Regenerate the source-tree inventory after reviewed release edits."""
import datetime
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or '.git' in path.relative_to(root).parts or path.name == 'RELEASE_FILES.json':
        continue
    if path.is_symlink():
        raise ValueError('Release files must not be symlinks')
    data = path.read_bytes()
    files.append(dict(path=path.relative_to(root).as_posix(), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))
(root / 'RELEASE_FILES.json').write_text(json.dumps(dict(
    schema_version=2, prepared_date=datetime.date.today().isoformat(),
    scope='Every source-tree and working-manuscript file except RELEASE_FILES.json itself and Git internals; supporting research assets have separate manifests.',
    files=files), indent=2) + '\n')
print(f'Inventoried {len(files)} files')
