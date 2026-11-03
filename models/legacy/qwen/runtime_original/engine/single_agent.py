"""Single-agent baseline: one conversation with editable strategy notes.

Shares the fixed planning contract, numerical models and acceptance with the
multi-agent study. Tool use and stopping are chosen by the agent.
"""

import os
import argparse
import datetime
import json
import pathlib

from tqdm import tqdm

import providers
from agent_contract import OBJECTIVE, FRAMEWORK_VERSION, compose, has_current_evidence
import simulator as sim
from planning import add_planning_arguments, configure_from_arguments

TOOLS = [
    {
        "name": "get_state",
        "description": "Read the scenario summary, the design space, the current "
                       "design, and the latest simulation results.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_design",
        "description": "Modify design variables. Pass only the keys you want to "
                       "change. Invalid keys/values are rejected with errors and "
                       "the design is left untouched.",
        "input_schema": {
            "type": "object",
            "properties": {"changes": {
                "type": "object",
                "description": "e.g. {\"hydrogen\": true, \"battery_hours\": 8}",
            }},
            "required": ["changes"],
        },
    },
    {
        "name": "run_simulation",
        "description": "Solve the capacity-expansion optimization for the current "
                       "design and automatically check its dispatch in OpenDSS. "
                       "Return system results and distribution evidence.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_distribution_check",
        "description": "Validate the current design at the distribution level: "
                       "maps the latest system-level dispatch onto a "
                       "representative feeder including battery charge/discharge. "
                       "Checks scenario voltage limits, convergence and any "
                       "supplied line ratings, with worst-hour diagnostics. The "
                       "system-level optimum is only a hypothesis until it "
                       "passes lower-level checks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_strategy",
        "description": "Rewrite your own strategy notes (the mutable section of "
                       "your instructions). Use it to record what you learned "
                       "and how you will proceed; the new text fully replaces "
                       "the old notes.",
        "input_schema": {
            "type": "object",
            "properties": {"notes": {"type": "string"}},
            "required": ["notes"],
        },
    },
    {
        "name": "finish_design",
        "description": "End the search and summarize the outcome. Code reports "
                       "whether the final design actually passes.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]

INITIAL_NOTES = "(no notes yet — write some with update_strategy as you learn)"


def system_prompt(notes: str) -> str:
    return compose(
        "You are an energy-system planning engineer working alone. "
        "Read the state, choose experiments, interpret results and revise the design. "
        "run_simulation includes OpenDSS; after distribution-only edits, use run_distribution_check. "
        "Use update_strategy for lessons; earlier conversation remains available. "
        "Call finish_design when further work seems unproductive or the search is stuck.",
        notes,
    )


class World:
    """Executes tool calls against simulator.py and tracks agentic-process metrics."""

    def __init__(self, max_runs: int):
        self.design = dict(sim.DEFAULT_DESIGN)
        self.last_results = None
        self.last_dist_check = None
        self.max_runs = max_runs
        self.notes = INITIAL_NOTES
        self.metrics = {"sim_runs": 0, "distribution_checks": 0,
                        "invalid_proposals": 0, "api_calls": 0, "solve_seconds": 0.0,
                        "strategy_updates": 0, "input_tokens": 0,
                        "output_tokens": 0}
        self.finished = False
        self.final_summary = None
        self.stopped_by = None

    def execute(self, name: str, args: dict) -> str:
        if name == "get_state":
            return json.dumps({
                "scenario": sim.SCENARIO_SUMMARY,
                "design_space": sim.DESIGN_SPACE,
                "current_design": self.design,
                "latest_results": self.last_results,
                "latest_distribution_check": self.last_dist_check,
            })
        if name == "update_design":
            errors = sim.validate_changes(args.get("changes", {}))
            if errors:
                self.metrics["invalid_proposals"] += 1
                return json.dumps({"ok": False, "errors": errors})
            self.design.update(args["changes"])
            self.last_dist_check = None
            if any(not key.startswith("dist_") for key in args["changes"]):
                self.last_results = None
            return json.dumps({"ok": True, "current_design": self.design})
        if name == "run_simulation":
            if self.metrics["sim_runs"] >= self.max_runs:
                return json.dumps({"error": "simulation cap of this run reached "
                                            "(safety ceiling, not a design budget); "
                                            "call finish_design"})
            self.metrics["sim_runs"] += 1
            self.last_results = sim.run_simulation(self.design)
            self.metrics["solve_seconds"] += self.last_results.get("solve_seconds", 0.0)
            self.last_dist_check = None
            if self.last_results.get("status") in ("optimal", "feasible_time_limit"):
                self.metrics["distribution_checks"] += 1
                self.last_dist_check = sim.run_distribution_check(self.design)
            return json.dumps({"results": self.last_results,
                               "distribution_check": self.last_dist_check})
        if name == "run_distribution_check":
            if not self.last_results or self.last_results.get("status") not in ("optimal", "feasible_time_limit"):
                return json.dumps({"error": "Run a successful simulation of the current design first", "passes": False})
            self.metrics["distribution_checks"] += 1
            self.last_dist_check = sim.run_distribution_check(self.design)
            return json.dumps(self.last_dist_check)
        if name == "update_strategy":
            self.notes = args.get("notes", self.notes)
            self.metrics["strategy_updates"] += 1
            return json.dumps({"ok": True})
        if name == "finish_design":
            if not has_current_evidence(self.last_results, self.last_dist_check):
                return json.dumps({"ok": False, "error": "Run the current design through the system solve and OpenDSS check before finishing"})
            from reporting import assess
            acceptance = assess(self.last_results, self.last_dist_check, sim.SCENARIO)
            self.finished = True
            self.final_summary = args.get("summary", "")
            return json.dumps({"ok": True, "acceptance": acceptance, "note": "Stopped; accepted only if all code-computed checks pass"})
        return json.dumps({"error": f"unknown tool {name}"})


def main() -> None:
    global sim
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["anthropic", "openai"],
                    default="anthropic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-runs", type=int, default=12)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--scenario", required=True,
                    help="regional experiment directory: miso, ercot, nyiso, south_africa")
    add_planning_arguments(ap)
    args = ap.parse_args()
    if args.scenario:
        import scenario_core
        sim = scenario_core.load_pack(args.scenario, policy=args.policy)

    sim = configure_from_arguments(sim, args, ap)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = getattr(sim, "NAME", "base")
    scen_part = "" if tag == "base" else f"_{tag}"
    out = pathlib.Path("runs") / f"single_{args.provider}{scen_part}_{args.planning_mode}_{stamp}"
    out = pathlib.Path(os.environ.get("GRID_RUN_OUTPUT", str(out)))
    out.mkdir(parents=True, exist_ok=True)
    from recovery import Recovery
    recovery = Recovery(out)
    recovery.install_providers(providers)
    sim = recovery.install_simulator(sim)
    log_f = open(out / "log.jsonl", "a")

    def log(event: str, payload) -> None:
        recovery.log(log_f, event, payload)

    import exploration
    chat = providers.make_chat(args.provider, args.model)
    world = World(args.max_runs)
    notes_history = [world.notes]
    log("meta", {"baseline": "B_single_agent", "provider": args.provider,
                 "model": chat.model, "scenario": tag,
                 "scenario_hash": sim.SCENARIO_HASH,
                 "planning": sim.PLANNING,
                 "max_runs": args.max_runs, "max_turns": args.max_turns})

    chat.send_user("Begin. Read the state, then iterate on the design.")

    import time
    t0 = time.time()
    for turn in tqdm(range(args.max_turns), desc="agent turns"):
        exploration.checkpoint(out, world.design, world.last_results, world.last_dist_check, world.metrics, sim.SCENARIO, turns=turn, replaying=recovery.peek() is not None)
        exploration.begin(turn=turn, sim_runs=world.metrics["sim_runs"])
        r = chat.step(system_prompt(world.notes), TOOLS)
        world.metrics["api_calls"] += 1
        world.metrics["input_tokens"] += r["usage"][0]
        world.metrics["output_tokens"] += r["usage"][1]
        log("assistant", r["raw"])

        if r["stop"] == "refusal":
            log("error", {"reason": "refusal"})
            world.stopped_by = "refusal"
            break

        if not r["tool_calls"]:
            if world.finished:
                break
            chat.send_user("Continue: use the tools, or call finish_design.")
            continue

        tool_results = []
        for tc in r["tool_calls"]:
            result = world.execute(tc["name"], tc["input"])
            log("tool", {"turn": turn, "name": tc["name"],
                         "input": tc["input"], "result": json.loads(result)})
            exploration.checkpoint(out, world.design, world.last_results, world.last_dist_check, world.metrics, sim.SCENARIO, turns=turn, replaying=recovery.peek() is not None)
            exploration.begin(sim_runs=world.metrics["sim_runs"])
            if tc["name"] == "update_strategy":
                notes_history.append(world.notes)
            tool_results.append({"id": tc["id"], "content": result})
        chat.send_tool_results(tool_results)

        if world.finished:
            world.stopped_by = "finish_design"
            break
    if world.stopped_by is None:
        world.stopped_by = "max_turns"

    world.metrics["estimated_llm_cost_usd"] = providers.estimate_cost_usd(
        chat.model, world.metrics["input_tokens"],
        world.metrics["output_tokens"])
    world.metrics["runtime_seconds"] = round(time.time() - t0, 1)
    final = {"framework_version": FRAMEWORK_VERSION,
             "scenario_hash": sim.SCENARIO_HASH,
             "electricity_policy": sim.SCENARIO["electricity_policy"],
             "final_design": world.design, "final_results": world.last_results,
             "final_distribution_check": world.last_dist_check,
             "final_summary": world.final_summary,
             "stopped_by": world.stopped_by,
             "strategy_notes_history": notes_history,
             "metrics": world.metrics}
    from reporting import assess, implementation_info
    final["recovery"] = recovery.provenance()
    if recovery.resume:
        final["metrics"]["resume_process_seconds"] = final["metrics"]["runtime_seconds"]
        final["metrics"]["runtime_seconds"] = None  # prior active wall time was not checkpointed
    final["implementation"] = implementation_info()
    final["acceptance"] = assess(final["final_results"], final["final_distribution_check"], sim.SCENARIO)
    exploration.checkpoint(out, world.design, world.last_results, world.last_dist_check, world.metrics, sim.SCENARIO, turns=turn+1, terminal=True, replaying=recovery.audit)
    exploration.annotate_final(final, {"max_turns": args.max_turns, "max_runs": args.max_runs})
    recovery.audit_final(final)
    (out / "final.json").write_text(json.dumps(final, indent=2))
    log_f.close()
    print(json.dumps(final, indent=2))
    print(f"logs: {out}")


if __name__ == "__main__":
    main()
