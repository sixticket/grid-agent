"""Offline release checker tests using only temporary synthetic files."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "check_release.py"
SPEC = importlib.util.spec_from_file_location("check_release", SOURCE)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class ReleaseCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def rules(self, **kwargs):
        return [item["rule"] for item in checker.check_release(self.root, **kwargs)["issues"]]

    def test_clean_tree_empty_env_localhost_and_git_database_pass(self):
        self.write("run.py", "raise RuntimeError('must never execute')\n")
        self.write(".env.example", "OPENAI_API_KEY=\nLOCAL_ENDPOINT=http://localhost:8000\nLOCAL_TOKEN=local-only\n")
        self.write("README.md", "[Run](run.py) [Remote](https://example.org/missing) [Section](#usage)\n")
        self.write(".git/objects/not-published.log", "private Git internals")
        self.assertEqual(self.rules(), [])
        self.assertFalse((self.root / "__pycache__").exists())

    def test_symlinks_are_rejected_without_reading_targets(self):
        target = self.write("target.txt", "ordinary text")
        (self.root / "linked.txt").symlink_to(target)
        (self.root / "linked-dir").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.rules().count("symlink is not allowed"), 2)

    def test_dataset_secret_environment_and_generated_directories_rejected(self):
        for name in ("data.csv", "dispatch.nc", "weights.safetensors", "events.jsonl", "run.log", "solver.lic"):
            self.write(name, "placeholder")
        self.write(".env.local", "TOKEN=\n")
        self.write(".venv/runtime.txt", "placeholder")
        self.write("node_modules/package/index.js", "placeholder")
        rules = self.rules()
        self.assertEqual(rules.count("data, log, binary or credential artifact is not allowed"), 6)
        self.assertIn("environment credential file is not allowed", rules)
        self.assertEqual(rules.count("environment or generated directory is not allowed"), 2)

    def test_patterns_report_locations_without_echoing_secret_values(self):
        tokens = [prefix + "X" * 32 for prefix in ("sk" + "-ant-", "sk" + "-", "github" + "_pat_", "gh" + "p_")]
        absolute = "/" + "Users" + "/" + "test-person" + "/private/source.py"
        linux = "/" + "home" + "/" + "researcher" + "/source.py"
        key = "-----BEGIN " + "PRIVATE KEY-----"
        self.write("accidental.txt", "\n".join(tokens + [absolute, linux, key]))
        result = checker.check_release(self.root)
        rules = [x["rule"] for x in result["issues"]]
        self.assertEqual(rules.count("possible API or GitHub credential"), 4)
        self.assertEqual(rules.count("user-specific absolute path"), 2)
        self.assertIn("private key material", rules)
        for sensitive in tokens + [absolute, linux, key]:
            self.assertNotIn(sensitive, json.dumps(result))

    def test_memory_compilation_catches_syntax_and_size_without_execution(self):
        self.write("bad.py", "def broken(:\n")
        self.write("large.txt", "x" * 100)
        rules = self.rules(max_bytes=50)
        self.assertIn("Python syntax or encoding error", rules)
        self.assertIn("file exceeds maximum release size", rules)

    def test_readme_links_check_decoded_paths_and_ignore_fenced_examples(self):
        self.write("docs/a file.md", "# A\n")
        self.write("README.md", "[valid](docs/a%20file.md#part)\n[broken](docs/missing.md)\n"
                   "[external path](../outside.md)\n```markdown\n[example](does-not-exist.md)\n```\n"
                   "[reference]: docs/also-missing.md\n")
        rules = self.rules()
        self.assertEqual(rules.count("README local link target does not exist"), 2)
        self.assertEqual(rules.count("README local link escapes release"), 1)

    def test_manifest_hashes_are_checked_and_escape_is_rejected(self):
        path = self.write("run.py", "print('example')\n")
        manifest = {"files": [{"path": "run.py", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
        self.write("manifest.json", json.dumps(manifest))
        self.assertEqual(self.rules(manifest=Path("manifest.json")), [])
        self.write("run.py", "print('changed')\n")
        self.assertIn("manifest SHA-256 mismatch", self.rules(manifest=Path("manifest.json")))
        manifest["files"] = [{"path": "../outside.txt", "sha256": "0" * 64}]
        self.write("manifest.json", json.dumps(manifest))
        self.assertIn("manifest path escapes release or is a symlink", self.rules(manifest=Path("manifest.json")))

    def test_checker_and_tests_scan_themselves_without_pattern_false_positives(self):
        self.write("tools/check_release.py", SOURCE.read_text())
        self.write("tests/test_check_release.py", Path(__file__).read_text())
        self.assertEqual(self.rules(), [])


if __name__ == "__main__":
    unittest.main()
