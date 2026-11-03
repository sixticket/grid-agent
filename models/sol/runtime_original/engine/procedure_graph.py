"""Editable procedural guidance, inspired by arXiv:2609.09153.

The graph is advisory memory, not executable control flow. Edits are applied
atomically after structural validation and logged, without claiming that a
later novel experiment validates their quality. Roles, tools, world inputs
and physical acceptance live outside this memory.
"""

import copy
import json

MAX_NODES = 30
MAX_EDGES = 60
MAX_TEXT = 400

# Minimal expert skeleton: the order a planner works through, nothing more.
# The refiner may repair, extend or prune it.
SKELETON = {
    "nodes": {
        "adequacy": "Make the system meet the reliability threshold at all.",
        "portfolio": "Reduce cost by changing the technology mix and its bounds.",
        "storage_sizing": "Set battery power and duration, decide on long-duration storage.",
        "transmission_relief": "Relieve binding interfaces and corridors: candidate circuits, official projects.",
        "feeder_repair": "Fix the representative-feeder failure of a design that passes at system level.",
        "verify": "Confirm the committed design at both levels and stop when nothing informative is left.",
    },
    "edges": [
        {"from": "adequacy", "to": "portfolio",
         "condition": "Unmet energy is within the threshold.",
         "guidance": "Start cost reduction from the largest capex line.",
         "pitfalls": "Do not cut firm capacity before checking the worst unmet hours."},
        {"from": "portfolio", "to": "storage_sizing",
         "condition": "Curtailment is high or evening peaks are served by expensive units.",
         "guidance": "Test battery duration before adding more renewable capacity.",
         "pitfalls": "Storage sized on average days misses the multi-day lull."},
        {"from": "portfolio", "to": "transmission_relief",
         "condition": "An interface or corridor is binding for many hours.",
         "guidance": "Compare an official project against generic circuits on the same cut.",
         "pitfalls": "Adding generation behind a binding interface does not help the load side."},
        {"from": "storage_sizing", "to": "feeder_repair",
         "condition": "The distribution check fails after new batteries were added.",
         "guidance": "Look at when the batteries charge on the feeder before reinforcing lines.",
         "pitfalls": "A system-level optimum can charge batteries during the local evening peak."},
        {"from": "transmission_relief", "to": "portfolio",
         "condition": "Binding hours dropped but cost did not.",
         "guidance": "Re-examine the mix now that the network constraint moved.",
         "pitfalls": "Do not keep adding circuits on a corridor whose utilization is already low."},
        {"from": "feeder_repair", "to": "verify",
         "condition": "The feeder passes with the committed system design.",
         "guidance": "Re-run the distribution check on the final design, not on a variant.",
         "pitfalls": "A feeder fix that changes the dispatch needs a fresh system solve."},
        {"from": "portfolio", "to": "verify",
         "condition": "Two consecutive cycles changed cost by less than the solver gap.",
         "guidance": "Verify the most load-bearing remaining claim, then rule.",
         "pitfalls": "Cost differences inside the MIP gap are not evidence."},
    ],
}


def new_graph(prior=None):
    return copy.deepcopy(prior if prior is not None else SKELETON)


def validate(graph):
    """Structural validation; returns a list of error strings (empty = ok)."""
    if not isinstance(graph, dict):
        return ["graph must be an object"]
    errors = []
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, dict) or not nodes:
        return ["graph needs a non-empty 'nodes' mapping"]
    if not isinstance(edges, list):
        return ["graph needs an 'edges' list"]
    if len(nodes) > MAX_NODES:
        errors.append(f"too many nodes ({len(nodes)} > {MAX_NODES})")
    if len(edges) > MAX_EDGES:
        errors.append(f"too many edges ({len(edges)} > {MAX_EDGES})")
    for nid, desc in nodes.items():
        if not isinstance(nid, str) or not nid or not isinstance(desc, str):
            errors.append(f"node {nid!r} must map a non-empty id to a text")
        elif len(desc) > MAX_TEXT:
            errors.append(f"node {nid} description longer than {MAX_TEXT} chars")
    seen = set()
    for e in edges:
        if (not isinstance(e, dict) or not isinstance(e.get("from"), str)
                or not isinstance(e.get("to"), str)
                or e["from"] not in nodes or e["to"] not in nodes):
            errors.append(f"edge {e!r} must join two existing nodes")
            continue
        key = (e["from"], e["to"])
        if key in seen:
            errors.append(f"duplicate edge {key}")
        seen.add(key)
        for field in ("condition", "guidance", "pitfalls"):
            if not isinstance(e.get(field), str):
                errors.append(f"edge {key} missing text field '{field}'")
            elif len(e[field]) > MAX_TEXT:
                errors.append(f"edge {key} field '{field}' longer than {MAX_TEXT} chars")
    return errors


def apply_edits(graph, edits):
    """Apply a list of edit operations to a copy of the graph. Returns
    (candidate, errors). Nothing is applied when any edit is malformed."""
    if not isinstance(edits, list):
        return graph, ["edits must be a list"]
    g = copy.deepcopy(graph)
    errors = []
    for i, ed in enumerate(edits or []):
        op = ed.get("op") if isinstance(ed, dict) else None
        try:
            if op == "add_node":
                if ed["id"] in g["nodes"]:
                    raise KeyError(f"node {ed['id']} already exists")
                g["nodes"][ed["id"]] = ed["description"]
            elif op == "revise_node":
                if ed["id"] not in g["nodes"]:
                    raise KeyError(f"node {ed['id']} does not exist")
                g["nodes"][ed["id"]] = ed["description"]
            elif op == "delete_node":
                if ed["id"] not in g["nodes"]:
                    raise KeyError(f"node {ed['id']} does not exist")
                del g["nodes"][ed["id"]]
                g["edges"] = [e for e in g["edges"]
                              if e["from"] != ed["id"] and e["to"] != ed["id"]]
            elif op == "add_edge":
                g["edges"].append({k: ed[k] for k in
                                   ("from", "to", "condition", "guidance", "pitfalls")})
            elif op == "revise_edge":
                match = [e for e in g["edges"]
                         if e["from"] == ed["from"] and e["to"] == ed["to"]]
                if not match:
                    raise KeyError(f"edge {ed['from']}->{ed['to']} does not exist")
                for k in ("condition", "guidance", "pitfalls"):
                    if k in ed:
                        match[0][k] = ed[k]
            elif op == "delete_edge":
                before = len(g["edges"])
                g["edges"] = [e for e in g["edges"]
                              if not (e["from"] == ed["from"] and e["to"] == ed["to"])]
                if len(g["edges"]) == before:
                    raise KeyError(f"edge {ed['from']}->{ed['to']} does not exist")
            else:
                raise KeyError(f"unknown op {op!r}")
        except (KeyError, TypeError) as exc:
            errors.append(f"edit {i}: {exc}")
    errors += validate(g)
    return (graph if errors else g), errors


def localize(results, dist_check, acceptance, previous=None):
    """Code-computed position in the graph from simulator evidence.
    Returns (node_id, reason)."""
    if not results:
        return "adequacy", "no simulation yet"
    if results.get("status") == "rejected":
        return (previous or "adequacy"), "last proposal was invalid; position unchanged"
    checks = (acceptance or {}).get("checks", {})
    if not checks.get("solved", False):
        return "adequacy", f"solver status {results.get('status')}"
    if not checks.get("reliable", True):
        return "adequacy", f"unmet energy {results.get('unmet_demand_GWh')} GWh above threshold"
    if dist_check is not None and not checks.get("distribution", True):
        return "feeder_repair", "representative feeder fails for the committed design"
    tx = results.get("transmission") or {}
    hours = results.get("hours_modelled") or 0
    binding = [(i["interface"], i["binding_hours"]) for i in tx.get("interfaces", [])
               if i.get("binding_hours", 0) > 0.05 * max(hours, 1)]
    saturated = [c["corridor"] for c in tx.get("corridors", [])
                 if c.get("total_capacity_MW", 0) > 0 and c.get("peak_utilization", 0) >= 0.999]
    if binding:
        return "transmission_relief", f"interfaces binding over 5% of hours: {binding[:4]}"
    if len(saturated) >= 3:
        return "transmission_relief", f"{len(saturated)} corridors at capacity"
    return "portfolio", "system and feeder checks pass; remaining lever is cost"


def render(graph, position, reason):
    """Text shown to the agents: the whole (small) graph, the current
    position and its outgoing transitions."""
    lines = ["## Procedure graph (learned; a bias, not a rule)",
             f"Current procedure: {position} ({reason})", "", "Procedures:"]
    for nid, desc in graph["nodes"].items():
        marker = " <- you are here" if nid == position else ""
        lines.append(f"- {nid}: {desc}{marker}")
    out = [e for e in graph["edges"] if e["from"] == position]
    lines += ["", f"Transitions from {position}:"]
    if not out:
        lines.append("- (none recorded)")
    for e in out:
        lines.append(f"- to {e['to']} when: {e['condition']}\n"
                     f"    guidance: {e['guidance']}\n"
                     f"    pitfalls: {e['pitfalls']}")
    others = [e for e in graph["edges"] if e["from"] != position]
    if others:
        lines += ["", "Other transitions:"]
        lines += [f"- {e['from']} -> {e['to']} when: {e['condition']}" for e in others]
    return "\n".join(lines)


class Evolution:
    """Atomic memory edits with an audit trail; no performance gate."""

    def __init__(self, graph):
        self.graph = graph
        self.history = [copy.deepcopy(graph)]
        self.rejected = []
        self.applied = []

    def propose(self, edits, rationale, cycle):
        """Returns (adopted: bool, errors)."""
        if not edits:
            return False, []
        candidate, errors = apply_edits(self.graph, edits)
        record = dict(edits=copy.deepcopy(edits), rationale=rationale, cycle=cycle)
        if errors:
            self.rejected.append(dict(record, errors=errors))
            return False, errors
        self.graph = candidate
        self.applied.append(record)
        self.history.append(copy.deepcopy(candidate))
        return True, []

    def rejected_text(self, limit=8):
        if not self.rejected:
            return "(none)"
        return json.dumps(self.rejected[-limit:], indent=1)
