"""
Lesson 2 - Conditional edges: a Failure Triage Router
=====================================================
Every failed test needs a different next step:

  product_bug    -> draft a bug report for the dev team
  flaky_test     -> quarantine the test and open a ticket to stabilise it
  environment    -> alert DevOps (don't blame the product!)
  test_code_bug  -> send it to the automation engineer who owns the test

New concepts:
  * add_conditional_edges(source, router, path_map)   "if/else" between nodes
  * reducers: Annotated[list, operator.add]           a key that APPENDS instead of overwriting
  * graph.batch([...])                                run the graph on many inputs at once

Run it:
  python lessons/l02_conditional_routing.py
"""

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class TriageState(TypedDict):
    test_name: str
    error: str
    recent_runs: list[str]  # results of the last few runs, oldest first: "pass" / "fail"
    category: str
    reason: str
    # A REDUCER: every node's "actions" get APPENDED, so we keep a full audit trail.
    # Without Annotated[..., operator.add] each node would overwrite the previous list.
    actions: Annotated[list[str], operator.add]


ENVIRONMENT_SIGNS = ("connection refused", "502", "503", "dns", "selenium-grid", "no space left")
TEST_CODE_SIGNS = ("nosuchelement", "stale element", "unable to locate", "attributeerror", "fixture")


def count_flips(runs: list[str]) -> int:
    """pass->fail or fail->pass changes. A regression flips once; a flaky test flips a lot."""
    return sum(1 for before, after in zip(runs, runs[1:]) if before != after)


# --- nodes ----------------------------------------------------------------------
def classify_failure(state: TriageState) -> dict:
    error = state["error"].lower()
    flips = count_flips(state["recent_runs"])
    if any(sign in error for sign in ENVIRONMENT_SIGNS):
        category, reason = "environment", "the error points at infrastructure, not the app"
    elif flips >= 2:
        category, reason = "flaky_test", f"result flipped {flips} times in {state['recent_runs']}"
    elif any(sign in error for sign in TEST_CODE_SIGNS):
        category, reason = "test_code_bug", "the test could not find or drive the UI element"
    else:
        category, reason = "product_bug", "consistent assertion failure: the app is wrong"
    return {
        "category": category,
        "reason": reason,
        "actions": [f"classified as {category} ({reason})"],
    }


def draft_bug_report(state: TriageState) -> dict:
    return {"actions": [f"drafted a bug report ({state['error']}) for the dev team"]}


def quarantine_test(state: TriageState) -> dict:
    return {"actions": ["quarantined the test and opened a ticket to stabilise it"]}


def alert_devops(state: TriageState) -> dict:
    return {"actions": ["alerted #devops; re-run after the environment is fixed"]}


def assign_to_automation(state: TriageState) -> dict:
    return {"actions": ["assigned to the automation team: fix the locator / test code"]}


def notify_team(state: TriageState) -> dict:
    return {"actions": [f"posted triage result to #qa-triage: {state['category']}"]}


# --- the router: a plain function that returns a LABEL --------------------------
def route_by_category(
    state: TriageState,
) -> Literal["product_bug", "flaky_test", "environment", "test_code_bug"]:
    return state["category"]


def build_graph():
    builder = StateGraph(TriageState)
    builder.add_node("classify_failure", classify_failure)
    builder.add_node("draft_bug_report", draft_bug_report)
    builder.add_node("quarantine_test", quarantine_test)
    builder.add_node("alert_devops", alert_devops)
    builder.add_node("assign_to_automation", assign_to_automation)
    builder.add_node("notify_team", notify_team)

    builder.add_edge(START, "classify_failure")
    # After classify_failure, call route_by_category and follow the matching arrow.
    builder.add_conditional_edges(
        "classify_failure",
        route_by_category,
        {  # path_map: label returned by the router -> node to run next
            "product_bug": "draft_bug_report",
            "flaky_test": "quarantine_test",
            "environment": "alert_devops",
            "test_code_bug": "assign_to_automation",
        },
    )
    # All four branches meet again at notify_team.
    for branch in ("draft_bug_report", "quarantine_test", "alert_devops", "assign_to_automation"):
        builder.add_edge(branch, "notify_team")
    builder.add_edge("notify_team", END)
    return builder.compile()


FAILURES = [
    {"test_name": "test_checkout_with_card",
     "error": "AssertionError: expected 'Order confirmed' but got 'Payment declined'",
     "recent_runs": ["fail", "fail", "fail", "fail"]},
    {"test_name": "test_search_suggestions",
     "error": "TimeoutError: suggestions dropdown did not appear within 5s",
     "recent_runs": ["pass", "fail", "pass", "pass", "fail"]},
    {"test_name": "test_login_sso",
     "error": "ConnectionError: [Errno 111] Connection refused (selenium-grid:4444)",
     "recent_runs": ["pass", "pass", "pass", "fail"]},
    {"test_name": "test_add_to_wishlist",
     "error": "NoSuchElementException: Unable to locate element [data-test='wishlist-btn']",
     "recent_runs": ["pass", "pass", "fail", "fail"]},
    {"test_name": "test_apply_coupon",
     "error": "AssertionError: expected total 900.0 but got 1000.0 (coupon SAVE10 not applied)",
     "recent_runs": ["pass", "pass", "pass", "fail"]},
]

if __name__ == "__main__":
    graph = build_graph()
    print(graph.get_graph().draw_mermaid())

    # batch() runs the graph once per input (concurrently) and returns the final states.
    results = graph.batch(FAILURES)

    for result in results:
        print(f"\n{result['test_name']}  ->  {result['category'].upper()}")
        for step, action in enumerate(result["actions"], start=1):
            print(f"   {step}. {action}")
