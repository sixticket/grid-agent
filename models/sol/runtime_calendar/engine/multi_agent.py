"""Role-specialized planning with a shared fixed contract and editable memory.

Specialists propose; the orchestrator selects one experiment; numerical tools
solve and check it; evaluator and critic interpret; refiner updates advisory
procedure memory; compressor records evidence; judge chooses whether to stop.
"""

import os
import argparse
import datetime
import json
import pathlib

from tqdm import tqdm

import providers
import procedure_graph as pg
from agent_contract import OBJECTIVE, FRAMEWORK_VERSION, compose, has_current_evidence
import simulator as sim
from planning import add_planning_arguments, configure_from_arguments

# Role identities and call order are code-owned; graph edits cannot replace them.
INITIAL_ROLE_PROMPTS = {
    "architect": "System Architect: assess the overall technology portfolio and network.",
    "renewable": "Renewable Specialist: assess solar/wind output, complementarity and curtailment.",
    "storage": "Storage Specialist: assess storage power, duration and dispatch constraints.",
    "nuclear_firm": "Firm-Generation Specialist: assess firm supply and its system tradeoffs.",
    "evaluator": "Evaluator: explain cost, reliability and physical failures from this cycle's results.",
    "critic": "Critic: check the evidence behind claims, alternatives and unresolved questions.",
}

TASK_CONTRACTS = {
    "specialist": "Propose an experiment using named design variables and values, or recommend no change. Explain why.",
    "evaluator": "Interpret the new results and identify the main remaining issue.",
    "critic": "Review the conclusions and suggest a useful next step, if any.",
}

ORCHESTRATOR_SYSTEM = (
    "Orchestrator: select one experiment from the shared state and specialist "
    "proposals. You may combine changes or select an unchanged design. "
    "Use select_experiment; the judge decides whether to continue."
)

JUDGE_SYSTEM = (
    "Convergence Judge: decide whether another cycle is useful from the supplied "
    "results and trajectory. Use verification tools if a remaining question "
    "needs them; existing simulation evidence is enough to stop. Stop when "
    "further improvement seems unproductive or the search is stuck. "
    "Report the reason with judge_convergence."
)

VERIFY_TOOL = {
    "name": "verify_design",
    "description": "Run a verification simulation on a hypothetical variant "
                   "of the current design WITHOUT committing it. Use this to "
                   "check quantitative claims (e.g. 'longer battery duration "
                   "would only save $X') before ruling. Returns full "
                   "simulation results for the variant.",
    "input_schema": {
        "type": "object",
        "properties": {"changes": {
            "type": "object",
            "description": "design-variable changes to apply to a copy of "
                           "the current design, e.g. {\"battery_hours\": 8}"}},
        "required": ["changes"],
    },
}

CHECK_DIST_TOOL = {
    "name": "check_distribution",
    "description": "Validate the CURRENT committed design at the distribution "
                   "level: maps its system-level dispatch onto a "
                   "representative feeder including battery charge/discharge. "
                   "Checks scenario voltage limits, convergence and supplied "
                   "line ratings, with worst-hour diagnostics. The system-level "
                   "optimum is only a hypothesis until it survives "
                   "lower-level checks.",
    "input_schema": {"type": "object", "properties": {}},
}

VERDICT_TOOL = {
    "name": "judge_convergence",
    "description": "Commit the continue/stop verdict for the study.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stop": {"type": "boolean",
                     "description": "true = end the study now"},
            "reasoning": {"type": "string"},
        },
        "required": ["stop", "reasoning"],
    },
}

REFINER_SYSTEM = (
    "Procedure Refiner: update advisory procedure-graph guidance from this cycle's "
    "evidence. Return edits or an empty list. Valid edits are applied immediately "
    "and logged; application is not proof that guidance improves performance. "
    "Roles, tools and the fixed contract are not editable. "
    "Node/edge texts must fit the graph's 400-character field limit."
)


def compose_prompt(name: str, guidance: str) -> str:
    task = TASK_CONTRACTS.get(name, TASK_CONTRACTS["specialist"])
    return compose(INITIAL_ROLE_PROMPTS[name] + " " + task, guidance)


COMPRESSOR_SYSTEM = (
    "Memory Compressor: record the tested hypothesis, findings and open questions "
    "faithfully for the next cycle. Distinguish observations from conjectures."
)

SELECT_TOOL = {
    "name": "select_experiment",
    "description": "Commit this cycle's experiment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "changes": {"type": "object",
                        "description": "design-variable changes to apply, "
                                       "e.g. {\"hydrogen\": true}"},
            "rationale": {"type": "string"},
        },
        "required": ["changes", "rationale"],
    },
}

EDIT_GRAPH_TOOL = {
    "name": "edit_procedure_graph",
    "description": "Propose edits to the procedure graph. Empty edits = no "
                   "change. Ops: add_node {id, description}, revise_node {id, "
                   "description}, delete_node {id}, add_edge {from, to, "
                   "condition, guidance, pitfalls}, revise_edge {from, to, any "
                   "of condition/guidance/pitfalls}, delete_edge {from, to}.",
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {"type": "array", "items": {"type": "object"},
                      "description": "ordered list of edit operations"},
            "rationale": {"type": "string"},
        },
        "required": ["edits", "rationale"],
    },
}

RECORD_TOOL = {
    "name": "record_cycle",
    "description": "Store the structured summary of this cycle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tested_hypothesis": {"type": "string"},
            "selected_changes": {"type": "object"},
            "simulation_findings": {"type": "array", "items": {"type": "string"}},
            "accepted_findings": {"type": "array", "items": {"type": "string"}},
            "rejected_hypotheses": {"type": "array", "items": {"type": "string"}},
            "agent_disagreements": {"type": "array", "items": {"type": "string"}},
            "unresolved_questions": {"type": "array", "items": {"type": "string"}},
            "next_experiment": {"type": "string"},
        },
        "required": ["tested_hypothesis", "selected_changes",
                     "simulation_findings", "accepted_findings",
                     "unresolved_questions", "next_experiment"],
    },
}

SPECIALIST_NAMES = ["architect", "renewable", "storage", "nuclear_firm"]


class Study:
    def __init__(self, provider: str, model: str | None, out: pathlib.Path,
                 graph_prior: dict | None = None):
        global sim
        self.provider = provider
        self.model = model
        self.design = dict(sim.DEFAULT_DESIGN)
        self.last_results = None
        self.last_dist_check = None
        self.memory = []          # compressor summaries, one per cycle (§9-§10)
        self.trajectory = []      # code-computed per-cycle outcome table
        self.evolution = pg.Evolution(pg.new_graph(graph_prior))
        self.position = "adequacy"
        self.position_reason = "no simulation yet"
        self.simulated = set()    # design keys already solved in this study
        self.metrics = {"api_calls": 0, "sim_runs": 0, "verification_sims": 0,
                        "distribution_checks": 0, "solve_seconds": 0.0, "invalid_proposals": 0,
                        "repeated_designs": 0, "graph_edits_proposed": 0,
                        "graph_edits_applied": 0,
                        "graph_edits_malformed": 0, "input_tokens": 0,
                        "output_tokens": 0, "tokens_by_agent": {}}
        from recovery import Recovery
        self.recovery = Recovery(out)
        self.recovery.install_providers(providers)
        sim = self.recovery.install_simulator(sim)
        from best_accepted import BestAccepted
        self.best_accepted = BestAccepted(out, sim.SCENARIO, sim, audit=self.recovery.audit)
        self.log_f = open(out / "log.jsonl", "a")

    def log(self, event: str, payload) -> None:
        self.recovery.log(self.log_f, event, payload)

    # ---- LLM plumbing ----------------------------------------------------
    def _account(self, agent: str, r: dict) -> None:
        self.metrics["api_calls"] += 1
        self.metrics["input_tokens"] += r["usage"][0]
        self.metrics["output_tokens"] += r["usage"][1]
        per = self.metrics["tokens_by_agent"].setdefault(
            agent, {"calls": 0, "input": 0, "output": 0})
        per["calls"] += 1
        per["input"] += r["usage"][0]
        per["output"] += r["usage"][1]
        self.log("llm", {"agent": agent, "response": r["raw"]})

    def _call(self, agent: str, system: str, user: str, tool=None,
              max_tokens=None):
        r = providers.single_call(self.provider, self.model, system, user,
                                  tool, max_tokens=max_tokens)
        self._account(agent, r)
        if tool is not None:
            return r["tool_calls"][0]["input"] if r["tool_calls"] else {}
        return r["text"]

    def _judge(self, cycle: int, summary: dict, critique: str) -> bool:
        """Convergence judge with a verification-simulation budget: it may run
        verify_design on hypothetical variants (never committed) before
        ruling via judge_convergence."""
        MAX_VERIFICATIONS = 2
        chat = providers.make_chat(self.provider, self.model)
        chat.send_user(
            "Outcome trajectory so far (code-computed):\n"
            + json.dumps(self.trajectory, indent=1)
            + "\n\nCurrent state:\n" + self.render_state(cycle)
            + "\n\nThis cycle's summary:\n" + json.dumps(summary)
            + "\n\nCritic review:\n" + critique
            + f"\n\nYou may run up to {MAX_VERIFICATIONS} verify_design "
            "simulations to check claims, then commit your verdict with "
            "judge_convergence.")
        verifications = 0
        dist_checks = 0
        for _ in range(3 + MAX_VERIFICATIONS):
            r = chat.step(compose(JUDGE_SYSTEM),
                          tools=[VERIFY_TOOL, CHECK_DIST_TOOL, VERDICT_TOOL])
            self._account("judge", r)
            tool_results = []
            for tc in r["tool_calls"]:
                if tc["name"] == "judge_convergence":
                    if tc["input"].get("stop") and not has_current_evidence(self.last_results, self.last_dist_check):
                        tool_results.append({"id": tc["id"], "content": json.dumps({
                            "error": "Stop requires a simulation of the committed design and, if solved, its OpenDSS check"})})
                        continue
                    self.log("verdict", {"cycle": cycle,
                                         "verifications": verifications,
                                         **tc["input"]})
                    return bool(tc["input"].get("stop"))
                if tc["name"] == "check_distribution":
                    if not self.last_results or self.last_results.get("status") not in ("optimal", "feasible_time_limit"):
                        out = {"error": "Current design needs a successful system solve first", "passes": False}
                    else:
                        out = sim.run_distribution_check(self.design)
                        self.metrics["distribution_checks"] += 1
                        dist_checks += 1
                        self.last_dist_check = out
                        self.best_accepted.observe(self.design, self.last_results, self.last_dist_check,
                                                   {"cycle": cycle, "event": "distribution_check"},
                                                   replayed=bool(self.recovery.peek()))
                    self.log("distribution_check", {"cycle": cycle,
                                                    "results": out})
                    tool_results.append({"id": tc["id"],
                                         "content": json.dumps(out)})
                    continue
                if tc["name"] == "verify_design":
                    changes = tc["input"].get("changes", {})
                    errors = sim.validate_changes(changes)
                    if verifications >= MAX_VERIFICATIONS:
                        out = {"error": "verification budget exhausted; "
                                        "rule now with judge_convergence"}
                    elif errors:
                        out = {"ok": False, "errors": errors}
                    else:
                        trial = dict(self.design)
                        trial.update(changes)
                        # uncommitted: must not replace the committed design's cached solve
                        out = sim.run_simulation(trial, commit=False)
                        verifications += 1
                        self.metrics["verification_sims"] += 1
                        self.metrics["solve_seconds"] += out.get("solve_seconds", 0.0)
                    self.log("verification", {"cycle": cycle,
                                              "changes": changes,
                                              "results": out})
                    tool_results.append({"id": tc["id"],
                                         "content": json.dumps(out)})
            if tool_results:
                chat.send_tool_results(tool_results)
            else:
                chat.send_user("Commit your verdict with judge_convergence.")
        self.log("verdict", {"cycle": cycle, "stop": False,
                             "reasoning": "judge returned no verdict; "
                                          "defaulting to continue"})
        return False

    # ---- cycle steps -----------------------------------------------------
    def render_state(self, cycle: int) -> str:
        return json.dumps({
            "objective": OBJECTIVE,
            "cycle": cycle,
            "scenario": sim.SCENARIO_SUMMARY,
            "design_space": sim.DESIGN_SPACE,
            "current_design": self.design,
            "latest_results": self.last_results,
            "latest_distribution_check": self.last_dist_check,
            "previous_cycle_summaries": self.memory,
        }, indent=1)

    def guidance_text(self) -> str:
        return pg.render(self.evolution.graph, self.position, self.position_reason)

    def run_cycle(self, cycle: int) -> bool:
        """Returns True if the study should stop."""
        from reporting import assess
        acceptance = (assess(self.last_results, self.last_dist_check, sim.SCENARIO)
                      if self.last_results else None)
        self.position, self.position_reason = pg.localize(
            self.last_results, self.last_dist_check, acceptance, self.position)
        self.log("localization", {"cycle": cycle, "position": self.position,
                                  "reason": self.position_reason})
        guidance = self.guidance_text()
        state = self.render_state(cycle)

        proposals = {}
        for name in SPECIALIST_NAMES:
            proposals[name] = self._call(
                name, compose_prompt(name, guidance), state)

        decision = self._call(
            "orchestrator", compose(ORCHESTRATOR_SYSTEM),
            state + "\n\nSpecialist proposals:\n"
            + json.dumps(proposals, indent=1), tool=SELECT_TOOL)

        if "changes" not in decision:
            # the orchestrator returned no select_experiment call (e.g. prose
            # only after nudges): treat as an invalid proposal, do not crash
            errors = ["orchestrator committed no experiment this cycle "
                      "(select_experiment was not called)"]
        else:
            errors = sim.validate_changes(decision["changes"])
        if errors:
            self.last_dist_check = None
            self.metrics["invalid_proposals"] += 1
            self.last_results = {"status": "rejected",
                                 "validation_errors": errors}
            repeated = False
        else:
            self.design.update(decision["changes"])
            key = json.dumps(self.design, sort_keys=True)
            repeated = key in self.simulated
            self.simulated.add(key)
            if repeated:
                self.metrics["repeated_designs"] += 1
            self.metrics["sim_runs"] += 1
            self.last_dist_check = None
            self.last_results = sim.run_simulation(self.design)
            self.metrics["solve_seconds"] += self.last_results.get("solve_seconds", 0.0)
            if self.last_results.get("status") in ("optimal", "feasible_time_limit"):
                # the Tool Agent runs the full chain: the committed design is
                # always validated at distribution level (cheap — the system
                # solve is cached), so evaluator/critic/judge see both levels
                self.last_dist_check = sim.run_distribution_check(self.design)
                self.metrics["distribution_checks"] += 1
        self.best_accepted.observe(self.design, self.last_results, self.last_dist_check,
                                   {"cycle": cycle, "event": "simulation"},
                                   replayed=bool(self.recovery.peek()))
        self.log("simulation", {"cycle": cycle, "decision": decision,
                                "design": self.design,
                                "results": self.last_results,
                                "distribution_check": self.last_dist_check})
        dist = self.last_dist_check or {}
        cost = self.last_results.get("total_cost_billion")
        dist_capex = dist.get("distribution_capex_billion", 0.0)
        self.trajectory.append({
            "cycle": cycle, "changes": decision.get("changes", {}),
            "cost_billion": cost,
            "dist_capex_billion": dist_capex,
            "all_in_cost_billion": (round(cost + dist_capex, 3)
                                    if cost is not None else None),
            "unmet_GWh": self.last_results.get("unmet_demand_GWh"),
            "dist_violation_hours": (dist.get("overvoltage_hours", 0)
                                     + dist.get("undervoltage_hours", 0)
                                     if dist else None),
            "dist_passes": dist.get("passes"),
            "dist_nonconverged_hours": dist.get("nonconverged_hours"),
            "dist_thermal_overload_hours": dist.get("thermal_overload_hours"),
            "status": self.last_results.get("status")})

        cycle_record = (state
                        + "\n\nSpecialist proposals:\n" + json.dumps(proposals)
                        + "\n\nOrchestrator decision:\n" + json.dumps(decision)
                        + "\n\nNew simulation results (system level):\n"
                        + json.dumps(self.last_results)
                        + "\n\nDistribution check of the committed design:\n"
                        + json.dumps(self.last_dist_check))

        evaluation = self._call(
            "evaluator", compose_prompt("evaluator", guidance), cycle_record)
        critique = self._call(
            "critic", compose_prompt("critic", guidance),
            cycle_record + "\n\nEvaluator analysis:\n" + evaluation,
            max_tokens=12000)

        refinement = self._call(
            "procedure_refiner", compose(REFINER_SYSTEM),
            "Current procedure graph:\n" + json.dumps(self.evolution.graph)
            + "\n\nMalformed edits:\n" + self.evolution.rejected_text()
            + "\n\nThis cycle:\n" + cycle_record
            + "\n\nEvaluator analysis:\n" + evaluation
            + "\n\nCritic review:\n" + critique,
            tool=EDIT_GRAPH_TOOL)
        edits = refinement.get("edits") or []
        adopted, edit_errors = self.evolution.propose(
            edits, refinement.get("rationale"), cycle)
        if edits:
            self.metrics["graph_edits_proposed"] += 1
        if adopted:
            self.metrics["graph_edits_applied"] += 1
        if edit_errors:
            self.metrics["graph_edits_malformed"] += 1
        self.log("graph_edit_proposed", {"cycle": cycle, "edits": edits,
                                         "rationale": refinement.get("rationale"),
                                         "applied": adopted,
                                         "errors": edit_errors})

        summary = self._call("compressor", compose(COMPRESSOR_SYSTEM),
                             cycle_record + "\n\nEvaluator analysis:\n"
                             + evaluation + "\n\nCritic review:\n" + critique,
                             tool=RECORD_TOOL)
        summary["cycle_id"] = cycle
        self.memory.append(summary)

        return self._judge(cycle, summary, critique)


def main() -> None:
    global sim
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["anthropic", "openai"],
                    default="anthropic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-cycles", type=int, default=6)
    ap.add_argument("--scenario", required=True,
                    help="regional experiment directory: miso, ercot, nyiso, south_africa")
    ap.add_argument("--procedure-graph", type=pathlib.Path, default=None,
                    help="JSON procedure graph to start from (e.g. exported by "
                         "another region's study); default = expert skeleton")
    add_planning_arguments(ap)
    args = ap.parse_args()
    if args.scenario:
        import scenario_core
        sim = scenario_core.load_pack(args.scenario, policy=args.policy)

    sim = configure_from_arguments(sim, args, ap)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = getattr(sim, "NAME", "base")
    scen_part = "" if tag == "base" else f"_{tag}"
    out = pathlib.Path("runs") / f"multi_{args.provider}{scen_part}_{args.planning_mode}_{stamp}"
    out = pathlib.Path(os.environ.get("GRID_RUN_OUTPUT", str(out)))
    out.mkdir(parents=True, exist_ok=True)

    prior = None
    if args.procedure_graph:
        prior = json.loads(args.procedure_graph.read_text())
        problems = pg.validate(prior)
        if problems:
            ap.error("invalid procedure graph: " + "; ".join(problems))
    study = Study(args.provider, args.model, out, graph_prior=prior)
    study.log("meta", {"baseline": "C_multi_agent", "provider": args.provider,
                       "model": args.model
                       or providers.DEFAULT_MODELS[args.provider],
                       "scenario": tag,
                       "scenario_hash": sim.SCENARIO_HASH,
                       "planning": sim.PLANNING,
                       # experiment-design freedom: agents choose stepwise vs
                       # whole-portfolio (one-change contract removed 09-05)
                       "experiment_contract": "free-form",
                       "procedure_graph_prior": (str(args.procedure_graph)
                                                 if args.procedure_graph else "skeleton"),
                       "max_cycles": args.max_cycles})

    import time
    t0 = time.time()
    stop = False
    for cycle in tqdm(range(args.max_cycles), desc="cycles"):
        stop = study.run_cycle(cycle)
        if not study.recovery.audit:
            (out / "cycle_summaries.json").write_text(
                json.dumps(study.memory, indent=2))
            (out / "procedure_graph.json").write_text(
                json.dumps(study.evolution.graph, indent=2))
            (out / "procedure_graph_history.json").write_text(
                json.dumps({"history": study.evolution.history,
                            "applied": study.evolution.applied,
                            "rejected": study.evolution.rejected}, indent=2))
        if stop:
            break

    model = args.model or providers.DEFAULT_MODELS[args.provider]
    study.metrics["estimated_llm_cost_usd"] = providers.estimate_cost_usd(
        model, study.metrics["input_tokens"], study.metrics["output_tokens"])
    study.metrics["runtime_seconds"] = round(time.time() - t0, 1)
    final = {"framework_version": FRAMEWORK_VERSION,
             "scenario_hash": sim.SCENARIO_HASH,
             "electricity_policy": sim.SCENARIO["electricity_policy"],
             "final_design": study.design, "final_results": study.last_results,
             "final_distribution_check": study.last_dist_check,
             "cycles_run": len(study.memory),
             "stopped_by": "judge" if stop else "max_cycles",
             "trajectory": study.trajectory,
             "final_procedure_graph": study.evolution.graph,
             "procedure_graph_edits": {"applied": study.evolution.applied,
                                       "rejected": study.evolution.rejected},
             "metrics": study.metrics}
    from reporting import assess, implementation_info
    final["recovery"] = study.recovery.provenance()
    if study.recovery.resume:
        final["metrics"]["resume_process_seconds"] = final["metrics"]["runtime_seconds"]
        final["metrics"]["runtime_seconds"] = None  # prior active wall time was not checkpointed
    final["implementation"] = implementation_info()
    final["acceptance"] = assess(final["final_results"], final["final_distribution_check"], sim.SCENARIO)
    study.best_accepted.apply_to_final(final)
    study.recovery.audit_final(final)
    (out / "final.json").write_text(json.dumps(final, indent=2))
    study.log_f.close()
    print(json.dumps(final, indent=2))
    print(f"logs: {out}")


if __name__ == "__main__":
    main()
