#!/usr/bin/env python3
"""Check a code-only release without importing it or writing any files.

Usage: python tools/check_release.py [--root DIRECTORY] [--manifest FILE]
An optional JSON manifest has a ``files`` list of objects with release-relative
``path`` and ``sha256`` fields. The manifest itself need not list itself.
The scan is a release hygiene check, not a guarantee that every secret is found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


BANNED_DIRECTORIES = frozenset({".venv", "venv", "node_modules", "__pycache__"})
BANNED_SUFFIXES = frozenset({
    ".csv", ".tsv", ".parquet", ".feather", ".nc", ".netcdf",
    ".pdf", ".xlsx", ".xls", ".xlsb", ".xlsm", ".ods",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z",
    ".gguf", ".safetensors", ".pt", ".pth", ".pkl", ".pickle",
    ".h5", ".hdf5", ".npy", ".npz", ".sqlite", ".sqlite3", ".db",
    ".lic", ".jsonl", ".log", ".pyc", ".pyo", ".so", ".dylib",
    ".dll", ".exe",
})
BANNED_NAMES = frozenset({
    ".ds_store", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "credentials.toml",
})

# Patterns intentionally require token-like values, so empty environment
# assignments, localhost URLs and short illustrative placeholders are allowed.
TOKEN_PATTERN = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|ghp_[A-Za-z0-9]{16,})\b"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
)
USER_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9][A-Za-z0-9._-]*(?:/|\b)"
)
INLINE_LINK = re.compile(r"\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", re.MULTILINE)


def finding(path: str, rule: str, line: int | None = None) -> dict:
    item = {"path": path, "rule": rule}
    if line is not None:
        item["line"] = line
    return item


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def check_links(path: Path, content: str, root: Path) -> list[dict]:
    """Validate README file targets; remote URLs and fragment-only links skip."""
    issues = []
    relative = path.relative_to(root).as_posix()
    # Code examples are prose, not rendered links. Preserve line positions.
    lines = content.splitlines(keepends=True)
    fenced = False
    fence_character = ""
    fence_length = 0
    for index, line in enumerate(lines):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fenced:
                fenced, fence_character, fence_length = True, token[0], len(token)
            elif token[0] == fence_character and len(token) >= fence_length:
                fenced = False
            lines[index] = "\n" if line.endswith("\n") else ""
        elif fenced:
            lines[index] = "\n" if line.endswith("\n") else ""
    markdown = "".join(lines)
    seen = set()
    for pattern in (INLINE_LINK, REFERENCE_LINK):
        for match in pattern.finditer(markdown):
            target = match.group(1).strip("<>")
            line = markdown.count("\n", 0, match.start()) + 1
            if (line, target) in seen:
                continue
            seen.add((line, target))
            if not target or target.startswith(("#", "//")):
                continue
            try:
                parsed = urlsplit(target)
            except ValueError:
                issues.append(finding(relative, "invalid README link", line))
                continue
            if parsed.scheme:
                continue
            local = unquote(parsed.path)
            if not local:
                continue
            resolved = (path.parent / local).resolve()
            if not inside(resolved, root):
                issues.append(finding(relative, "README local link escapes release", line))
            elif not resolved.exists():
                issues.append(finding(relative, "README local link target does not exist", line))
    return issues


def check_manifest(root: Path, manifest: Path) -> list[dict]:
    issues = []
    manifest = manifest if manifest.is_absolute() else root / manifest
    label = manifest.relative_to(root).as_posix() if inside(manifest, root) else "<manifest>"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        rows = document["files"]
        if not isinstance(rows, list):
            raise ValueError("files is not a list")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [finding(label, "cannot read manifest files list")]
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append(finding(label, "invalid manifest file entry"))
            continue
        name, expected = row.get("path"), row.get("sha256")
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected)):
            issues.append(finding(label, "manifest requires relative path and SHA-256"))
            continue
        source = root / name
        resolved = source.resolve()
        if not inside(resolved, root) or source.is_symlink():
            issues.append(finding(label, "manifest path escapes release or is a symlink"))
            continue
        relative = resolved.relative_to(root).as_posix()
        if relative in seen:
            issues.append(finding(relative, "duplicate manifest entry"))
            continue
        seen.add(relative)
        try:
            actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            issues.append(finding(relative, "manifest file is missing or unreadable"))
            continue
        if actual != expected.lower():
            issues.append(finding(relative, "manifest SHA-256 mismatch"))
    return issues


def check_release(root: Path, max_bytes: int = 2 * 1024 * 1024,
                  manifest: Path | None = None) -> dict:
    root = Path(root)
    if root.is_symlink():
        return {"files_checked": 0, "python_files_checked": 0,
                "issues": [finding(".", "release root is a symlink")]}
    root = root.resolve()
    issues = []
    files_checked = python_files_checked = 0
    if not root.is_dir():
        return {"files_checked": 0, "python_files_checked": 0,
                "issues": [finding(".", "release root is not a directory")]}

    def walk_error(_error):
        issues.append(finding(".", "cannot read a release directory"))

    for folder, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(folder)
        keep = []
        for name in sorted(directories):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(finding(relative, "symlink is not allowed"))
            elif name == ".git":
                # The Git object database is not part of the published tree.
                continue
            elif name.lower() in BANNED_DIRECTORIES:
                issues.append(finding(relative, "environment or generated directory is not allowed"))
            else:
                keep.append(name)
        directories[:] = keep
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(finding(relative, "symlink is not allowed"))
                continue
            files_checked += 1
            lower = name.lower()
            manuscript_asset = relative.startswith("manuscript/") and path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".pptx"}
            if ((lower == ".env" or lower.startswith(".env.")) and lower != ".env.example"):
                issues.append(finding(relative, "environment credential file is not allowed"))
            if lower in BANNED_NAMES or lower == ".git" or (path.suffix.lower() in BANNED_SUFFIXES and not manuscript_asset):
                issues.append(finding(relative, "data, log, binary or credential artifact is not allowed"))
            try:
                if path.stat().st_size > (20 * 1024 * 1024 if manuscript_asset else max_bytes):
                    issues.append(finding(relative, "file exceeds maximum release size"))
                    continue
                data = path.read_bytes()
            except OSError:
                issues.append(finding(relative, "file is unreadable"))
                continue
            if manuscript_asset:
                continue  # Published manuscript assets are hashed; binary data is expected.
            if b"\x00" in data:
                issues.append(finding(relative, "binary content is not allowed in code-only release"))
                continue
            content = data.decode("utf-8", errors="replace")
            for pattern, rule in (
                (TOKEN_PATTERN, "possible API or GitHub credential"),
                (PRIVATE_KEY_PATTERN, "private key material"),
                (USER_PATH_PATTERN, "user-specific absolute path"),
            ):
                lines = {content.count("\n", 0, match.start()) + 1 for match in pattern.finditer(content)}
                issues.extend(finding(relative, rule, line) for line in sorted(lines))
            if path.suffix.lower() == ".py":
                python_files_checked += 1
                try:
                    compile(data, relative, "exec", dont_inherit=True)
                except (SyntaxError, ValueError, UnicodeError) as error:
                    issues.append(finding(relative, "Python syntax or encoding error", getattr(error, "lineno", None)))
            if lower.startswith("readme") and path.suffix.lower() in {".md", ".markdown"}:
                issues.extend(check_links(path, content, root))
    if manifest is not None:
        issues.extend(check_manifest(root, Path(manifest)))
    return {"files_checked": files_checked, "python_files_checked": python_files_checked, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, help="optional JSON manifest to verify")
    parser.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    result = check_release(args.root, args.max_bytes, args.manifest)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in result["issues"]:
            location = item["path"] + (f':{item["line"]}' if "line" in item else "")
            print(f'{location}: {item["rule"]}')
        status = "PASS" if not result["issues"] else "FAIL"
        print(f'{status}: {result["files_checked"]} files, '
              f'{result["python_files_checked"]} Python sources, {len(result["issues"])} findings')
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
