"""Validate fresh Opus thinking-v1 journals without calling an API or solver.

Every physical response, including hidden tool-choice nudges, must have one
content-free transformation record. Fresh weather experiments have no exemption
for historical responses. Provider and recovery code remain frozen separately.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

MODEL = "claude-opus-5-5"
REVISION = "20260928-opus-thinking-v1"
BETA = "thinking-binding-controls-2026-08-01"
PREFIX_MISMATCH_BEHAVIOR = "drop_block"
BASE = Path(__file__).resolve().parent
_AUDIT_KEYS = {
    "schema_version", "recorded_at", "provider", "requested_model", "response_id",
    "beta", "prefix_mismatch_behavior", "drop_count", "input_transformations_present",
    "input_transformations", "response_sha256",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    _require(all(isinstance(row, dict) for row in rows), "Journal rows must be objects")
    return rows


def _index(rows, label):
    indexed = {}
    for row in rows:
        response_id = row.get("response_id")
        _require(isinstance(response_id, str) and response_id.startswith("msg_"),
                 f"Invalid response identity in {label}")
        _require(response_id not in indexed, f"Duplicate response in {label}")
        indexed[response_id] = row
    return indexed


def validate_journals(out):
    """Return verified protocol metadata and per-response drop counts.

    This is a read-only post-worker check. It raises on missing, duplicate,
    mismatched, or unknown records rather than weakening the frozen protocol.
    Like the source protocol, an absent provider transformation field is recorded
    explicitly; it is not silently interpreted as proof that nothing was dropped.
    """
    out = Path(out)
    origin_path = BASE / "runtime_origin.json"
    origin = json.loads(origin_path.read_text())
    protocol = origin["thinking_protocol"]
    options = {"beta": BETA, "prefix_mismatch_behavior": PREFIX_MISMATCH_BEHAVIOR}
    _require(protocol.get("revision") == REVISION and protocol.get("options") == options,
             "Frozen Opus protocol identity/options differ")
    source_manifest = protocol.get("source_manifest")
    _require(isinstance(source_manifest, dict) and set(source_manifest) == {"path", "sha256"}
             and isinstance(source_manifest["path"], str)
             and isinstance(source_manifest["sha256"], str)
             and re.fullmatch(r"[0-9a-f]{64}", source_manifest["sha256"]),
             "Invalid source protocol manifest provenance")

    raw_path = out / "api_responses.jsonl"
    audit_path = out / "input_transformations.jsonl"
    raw = _index(_rows(raw_path), "raw response journal")
    audit = _index(_rows(audit_path), "transformation journal")
    _require(bool(raw), "No physical Opus responses were recorded")
    _require(set(audit) == set(raw),
             "Thinking transformation audit does not cover exactly all physical responses")
    counts = {}
    absent_fields = []
    reasons = {"model_binding_mismatch": 0, "prefix_binding_mismatch": 0}
    for response_id, row in audit.items():
        saved = raw[response_id]
        response = saved.get("response")
        _require(isinstance(response, dict), "Raw response must be an object")
        _require(set(row) == _AUDIT_KEYS and type(row["schema_version"]) is int
                 and row["schema_version"] == 1
                 and row["provider"] == saved.get("provider") == "anthropic"
                 and row["requested_model"] == saved.get("requested_model") == MODEL
                 and saved.get("response_model") == response.get("model") == MODEL
                 and response.get("id") == response_id
                 and response.get("type") == "message" and response.get("role") == "assistant"
                 and row["beta"] == BETA
                 and row["prefix_mismatch_behavior"] == PREFIX_MISMATCH_BEHAVIOR,
                 "Thinking transformation audit identity/options differ")
        digest = hashlib.sha256(json.dumps(response, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        _require(row["response_sha256"] == digest,
                 "Thinking transformation audit raw response hash differs")
        transformations = response.get("input_transformations")
        _require(transformations is None or isinstance(transformations, list),
                 "Invalid raw transformation metadata")
        safe = []
        for item in transformations or []:
            _require(isinstance(item, dict) and item.get("type") == "thinking_dropped"
                     and item.get("reason") in reasons and isinstance(item.get("path"), str)
                     and re.fullmatch(r"messages(?:\[\d+\]|\.\d+)\.content(?:\[\d+\]|\.\d+)",
                                      item["path"]),
                     "Unrecognized thinking transformation metadata")
            safe.append({key: item[key] for key in ("type", "path", "reason")})
            reasons[item["reason"]] += 1
        present = "input_transformations" in response
        _require(row["input_transformations"] == safe and type(row["drop_count"]) is int
                 and row["drop_count"] == len(safe)
                 and row["input_transformations_present"] is present,
                 "Thinking transformation audit count/content differs")
        if not present:
            absent_fields.append(response_id)
        counts[response_id] = len(safe)

    return {
        "revision": REVISION, "model": MODEL, **options,
        "protocol_source_manifest": source_manifest,
        "runtime_origin": {"path": str(origin_path), "sha256": _sha(origin_path)},
        "raw_journal": {"path": raw_path.name, "sha256": _sha(raw_path),
                        "recorded_responses": len(raw)},
        "audit": {
            "path": audit_path.name, "sha256": _sha(audit_path), "coverage_complete": True,
            "audited_responses": len(audit), "audited_response_ids": sorted(audit),
            "historical_response_ids_not_audited": [],
            "total_dropped_blocks": sum(counts.values()),
            "responses_with_drops": sorted(key for key, count in counts.items() if count),
            "response_drop_counts": dict(sorted(counts.items())),
            "dropped_blocks_by_reason": reasons,
            "responses_without_input_transformations_field": sorted(absent_fields),
        },
        "scientific_caveat": "drop_block may remove invalid earlier thinking before a request. "
                             "Retained prompts and tool results do not imply identical reasoning continuity.",
    }
