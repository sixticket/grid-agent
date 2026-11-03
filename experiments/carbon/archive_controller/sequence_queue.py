#!/usr/bin/env python3
"""Run Claude single -> GPT single -> Claude multi -> GPT multi in order.

This file is deliberately separate from the hash-pinned experiment controller.
Stopping this queue leaves any experiment controller and worker running.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
import run_campaign as campaign_controller

BASE = Path(__file__).resolve().parent
STAGES = (("claude_single", "opus", "single", False),
          ("gpt_single", "sol", "single", True),
          ("claude_multi", "opus", "multi", False),
          ("gpt_multi", "sol", "multi", False))


class QueueBlocked(RuntimeError):
    """A recorded condition needs deliberate recovery, not an automatic retry."""


def require(value, message):
    if not value:
        raise QueueBlocked(message)


def read_json(path):
    return json.loads(Path(path).read_text())


def controller_alive(pid, campaign, stage, base=BASE):
    """Check PID identity, including the campaign path when --resume is present."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        return False
    try:
        args = shlex.split(result.stdout.strip())
    except ValueError:
        return False
    if str(base / "run_campaign.py") not in args:
        return False
    if "--resume" in args:
        at = args.index("--resume")
        return at + 1 < len(args) and Path(args[at + 1]).resolve() == Path(campaign)

    def values(flag):
        if flag not in args:
            return []
        rest = args[args.index(flag) + 1:]
        return rest[:next((i for i, item in enumerate(rest) if item.startswith("--")), len(rest))]

    return ("--execute" in args and values("--models") == [stage["model_key"]]
            and values("--agents") == [stage["agent"]])


def solver_available(base=BASE):
    with (base.parent / "gpt_sol/.paper_campaign.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock, fcntl.LOCK_UN)
        return True


class SequenceQueue:
    def __init__(self, sequence, state_path, *, base=BASE, controller=campaign_controller):
        self.base = Path(base).resolve()
        self.sequence = Path(sequence).resolve()
        self.state_path = Path(state_path).resolve()
        self.controller = controller
        self.children = {}
        self.config = read_json(self.sequence)
        require(self.config.get("schema_version") == 1, "Unsupported sequence schema")
        stages = self.config.get("stages", [])
        require(len(stages) == len(STAGES), f"Exactly {len(STAGES)} approved stages are required")
        paths = []
        for stage, expected in zip(stages, STAGES):
            require(tuple(stage.get(k) for k in ("id", "model_key", "agent", "retry_initial_failure"))
                    == expected, "Sequence order or model/architecture changed")
            path = Path(stage["campaign"]).resolve()
            require(path.is_relative_to(self.base / "runs"), "Campaign must remain under co2_constraint/runs")
            require(stage["campaign"] == str(path), "Campaign paths must be canonical absolute paths")
            paths.append(path)
        require(len(set(paths)) == len(STAGES), "Campaigns must be distinct")
        digest = self.controller.sha(self.sequence)
        if self.state_path.exists():
            self.state = read_json(self.state_path)
            require(self.state.get("sequence_sha256") == digest, "Sequence changed after queue initialization")
            require([(s["id"], s["campaign"]) for s in self.state["stages"]]
                    == [(s["id"], s["campaign"]) for s in stages], "Stored queue stages changed")
        else:
            self.state = dict(schema_version=1, sequence=str(self.sequence), sequence_sha256=digest,
                              created_at=self.controller.now(), status="initialized",
                              stages=[dict(id=s["id"], campaign=s["campaign"], status="pending",
                                           launch_claimed=False) for s in stages])
        for stage in stages:
            self.audit_campaign(stage)

    def save(self):
        self.state.update(pid=os.getpid(), updated_at=self.controller.now())
        self.controller.save(self.state_path, self.state)

    def audit_campaign(self, stage):
        campaign = read_json(Path(stage["campaign"]) / "campaign.json")
        require(campaign.get("execution_mode") == "agent", "Sequence only accepts agent campaigns")
        selection = campaign.get("selection", {})
        require(selection.get("models") == [stage["model_key"]]
                and selection.get("agents") == [stage["agent"]], "Campaign model or architecture differs")
        cases = set(self.controller.cases_by_id())
        require(len(selection.get("cases", [])) == 10 and set(selection["cases"]) == cases,
                "Each campaign must contain the same ten approved cases")
        expected = {f'{stage["model_key"]}/{case}/{stage["agent"]}' for case in cases}
        jobs = campaign.get("jobs", [])
        require(len(jobs) == 10 and {j["id"] for j in jobs} == expected,
                "Campaign does not contain exactly the ten expected jobs")
        self.controller.assert_unchanged(campaign)
        return campaign

    def alive(self, pid, stage):
        child = self.children.get(pid)
        if child is not None:
            return child.poll() is None
        return controller_alive(pid, stage["campaign"], stage, self.base)

    @staticmethod
    def initial_authentication_failure(campaign):
        """Only the known first GPT attempt can be retried automatically once."""
        jobs = campaign["jobs"]
        first = jobs[0]
        attempts = first.get("attempts", [])
        if not (campaign["status"] == "failed" and first["id"] == "sol/2023/nyiso/single"
                and first["status"] == "failed" and len(attempts) == 1
                and attempts[0].get("status") == "failed"
                and Path(attempts[0].get("output", "")).name == "attempt_001"
                and all(j["status"] == "pending" and not j.get("attempts") for j in jobs[1:])):
            return False
        log = Path(attempts[0]["output"]) / "execution.log"
        if not log.is_file():
            return False
        error = log.read_text(errors="replace")
        return "openai.AuthenticationError" in error and "401" in error and "expired_secret_key" in error

    def launch(self, stage, tracked, retry):
        self.audit_campaign(stage)
        tracked.update(status="launching", launch_claimed=True, retry_initial_failure_used=bool(retry),
                       launch_claimed_at=self.controller.now())
        self.state["status"] = "running"
        # Claim before Popen: a crash in this gap requires recovery, never a duplicate launch.
        self.save()
        logs = self.base / "sequence_logs"
        logs.mkdir(exist_ok=True)
        log_path = logs / f'{stage["id"]}.log'
        args = [sys.executable, str(self.base / "run_campaign.py"), "--resume", stage["campaign"], "--execute"]
        if retry:
            args.append("--retry-failed")
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        with log_path.open("a") as log:
            child = subprocess.Popen(args, cwd=self.base.parent, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.children[child.pid] = child
        tracked.update(status="running", controller_pid=child.pid, controller_log=str(log_path),
                       launched_at=self.controller.now())
        self.save()

    def tick(self):
        """Reconcile existing work, then start at most one eligible campaign."""
        for child in self.children.values():
            child.poll()  # Reap finished child controllers without affecting running ones.
        require(self.controller.sha(self.sequence) == self.state["sequence_sha256"],
                "Sequence configuration changed while running")
        for stage, tracked in zip(self.config["stages"], self.state["stages"]):
            campaign = read_json(Path(stage["campaign"]) / "campaign.json")
            self.state["current_stage"] = stage["id"]
            status = campaign.get("status")
            if status == "completed":
                require(len(campaign["jobs"]) == 10 and all(j["status"] == "completed" for j in campaign["jobs"]),
                        f'{stage["id"]}: campaign marked completed before all ten jobs completed')
                accepted = sum(j.get("summary", {}).get("accepted") is True for j in campaign["jobs"])
                tracked.update(status="completed", completed_at=campaign.get("completed_at"),
                               jobs_completed=10, jobs_accepted=accepted, jobs_not_accepted=10 - accepted)
                continue
            owned_pid = tracked.get("controller_pid")
            # A just-launched controller may not yet have changed campaign.json.
            if owned_pid and self.alive(owned_pid, stage):
                tracked["status"] = "running"
                self.state["status"] = "waiting"
                self.save()
                return "waiting"
            if status == "running":
                require(self.alive(campaign.get("pid"), stage),
                        f'{stage["id"]}: campaign says running but its controller is no longer alive')
                tracked.update(status="running", attached_controller_pid=campaign["pid"])
                self.state["status"] = "waiting"
                self.save()
                return "waiting"
            require(not tracked.get("launch_claimed"),
                    f'{stage["id"]}: previously launched controller stopped with campaign status {status}; automatic retry refused')
            retry = bool(stage["retry_initial_failure"] and self.initial_authentication_failure(campaign))
            require(status == "planned" or retry,
                    f'{stage["id"]}: campaign status {status} requires deliberate recovery')
            if not solver_available(self.base):
                tracked["status"] = "waiting_for_solver"
                self.state["status"] = "waiting"
                self.save()
                return "waiting"
            self.launch(stage, tracked, retry)
            return "launched"
        self.state.update(status="completed", completed_at=self.controller.now(), current_stage=None)
        self.save()
        return "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence", type=Path, default=BASE / "sequence.json")
    parser.add_argument("--state", type=Path, default=BASE / "sequence_state.json")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--once", action="store_true", help="Evaluate once; may launch one eligible stage")
    args = parser.parse_args()
    require(0 < args.poll_seconds <= 15, "Polling interval must be between zero and fifteen seconds")
    with (BASE / ".sequence_queue.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A CO2 sequence queue is already running.", flush=True)
            return 3
        queue = None
        def stop(signum, frame):
            raise KeyboardInterrupt
        handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            queue = SequenceQueue(args.sequence, args.state)
            queue.state.update(status="running", started_at=campaign_controller.now())
            queue.save()
            while True:
                result = queue.tick()
                if result == "completed":
                    return 0
                if args.once:
                    queue.state.update(status="paused", pause_reason="Single queue evaluation completed")
                    queue.save()
                    return 0
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            if queue is not None:
                queue.state.update(status="interrupted", stopped_at=campaign_controller.now(),
                                   reason="Queue stopped; existing experiment controllers were left running")
                queue.save()
            return 130
        except Exception as exc:
            if queue is not None:
                queue.state.update(status="blocked", stopped_at=campaign_controller.now(),
                                   error_type=type(exc).__name__, reason=str(exc))
                queue.save()
            elif args.state.exists():
                state = read_json(args.state)
                state.update(status="blocked", stopped_at=campaign_controller.now(),
                             error_type=type(exc).__name__, reason=str(exc))
                campaign_controller.save(args.state, state)
            else:
                campaign_controller.save(args.state.with_name("sequence_startup_error.json"),
                                         dict(status="blocked", stopped_at=campaign_controller.now(),
                                              error_type=type(exc).__name__, reason=str(exc)))
            print(f"CO2 sequence stopped: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
