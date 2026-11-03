"""Shared, fixed boundaries for single- and multi-agent planning.

Only strategy notes / procedure graphs are editable through agent tools.
Scenario validation and acceptance remain in the numerical code.
"""

FRAMEWORK_VERSION = "minimal-v2"

OBJECTIVE = (
    "Find a physically accepted design at the lowest total annualized cost "
    "you can reach, including distribution upgrades, under the supplied policy."
)

FIXED_CONTRACT = f"""[Fixed contract]
Objective: {OBJECTIVE}
PySAM/resource profiles are fixed inputs. PyPSA chooses investment and hourly
dispatch within the allowed design bounds. OpenDSS checks the same dispatch;
it adds no energy and does not redispatch storage.
Change only listed design variables. Demand, weather, costs, policy, existing
assets and network assumptions are fixed. Code validates changes and computes
acceptance; a narrative or strategy edit cannot replace that evidence.
Every successfully solved committed design must be checked in OpenDSS. Completion requires
current simulator evidence; PyPSA alone cannot establish physical acceptance.
Learned guidance is advisory. It cannot change this contract, your assigned
role or the available tools. Choose experiments and when to stop from evidence;
stopping an unsuccessful search does not make its design accepted."""


def compose(role, guidance=""):
    """Rebuild fixed instructions on every call; memory is never executable."""
    memory = f"\n\n[Advisory learned guidance]\n{guidance}" if guidance else ""
    return role + memory + "\n\n" + FIXED_CONTRACT


def has_current_evidence(result, distribution):
    """Failed searches may stop, but an untested design may not be finalized."""
    if not result or result.get("status") in (None, "rejected"):
        return False
    if result["status"] in ("optimal", "feasible_time_limit"):
        return distribution is not None
    return True  # A failed system solve is evidence of an unsuccessful attempt.
