"""
Lesson 3 - Loops: a Flaky Test Retrier (with guardrails)
========================================================
Before calling a failure a bug, re-run it:

  passed on the first attempt     -> PASSED
  failed, then passed on a retry  -> FLAKY   (quarantine it, don't file a bug)
  failed MAX_ATTEMPTS times       -> FAILED  (a real failure - triage it)

New concepts:
  * a CYCLE: a conditional edge that points back to an earlier node
  * loop guards: an attempt counter in the state, plus recursion_limit as a safety net
  * RetryPolicy: LangGraph's built-in automatic retry for a node that RAISES

Run it:
  python lessons/l03_loops_retry.py
"""

import operator
import time
from typing import Annotated, Literal, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

MAX_ATTEMPTS = 3

# A pretend test runner: each test has a script of outcomes, one per attempt, so the
# lesson is deterministic. In real life run_test would call pytest / Playwright / Selenium.
SCRIPTED_OUTCOMES = {
    "test_login": ["pass"],
    "test_search_suggestions": ["fail", "fail", "pass"],
    "test_checkout": ["fail"],  # fails every time
}


class RetryState(TypedDict):
    test_name: str
    attempts: int
    history: Annotated[list[str], operator.add]  # outcome of every attempt
    verdict: str


def run_test(state: RetryState) -> dict:
    attempt = state.get("attempts", 0) + 1
    script = SCRIPTED_OUTCOMES[state["test_name"]]
    outcome = script[min(attempt, len(script)) - 1]  # past the script: repeat the last outcome
    return {"attempts": attempt, "history": [outcome]}


def wait_before_retry(state: RetryState) -> dict:
    time.sleep(0.05 * 2 ** (state["attempts"] - 1))  # exponential backoff: 0.05s, 0.1s, 0.2s...
    return {}  # a node may change nothing


def decide_verdict(state: RetryState) -> dict:
    history = state["history"]
    if history[-1] == "fail":
        verdict = "FAILED"
    elif len(history) == 1:
        verdict = "PASSED"
    else:
        verdict = "FLAKY"
    return {"verdict": verdict}


# The loop guard lives in the router: retry only while attempts < MAX_ATTEMPTS.
def should_retry(state: RetryState) -> Literal["retry", "done"]:
    if state["history"][-1] == "fail" and state["attempts"] < MAX_ATTEMPTS:
        return "retry"
    return "done"


def build_graph(router=should_retry):
    builder = StateGraph(RetryState)
    builder.add_node("run_test", run_test)
    builder.add_node("wait_before_retry", wait_before_retry)
    builder.add_node("decide_verdict", decide_verdict)

    builder.add_edge(START, "run_test")
    builder.add_conditional_edges("run_test", router, {"retry": "wait_before_retry", "done": "decide_verdict"})
    builder.add_edge("wait_before_retry", "run_test")  # <- the CYCLE
    builder.add_edge("decide_verdict", END)
    return builder.compile()


# --- Part 3: RetryPolicy for transient exceptions ------------------------------
class SessionState(TypedDict):
    session: str


grid_calls = {"count": 0}


def open_browser_session(state: SessionState) -> dict:
    grid_calls["count"] += 1
    if grid_calls["count"] < 3:  # the Selenium grid is still waking up
        raise ConnectionError("selenium-grid:4444 refused the connection")
    return {"session": f"browser session ready (took {grid_calls['count']} attempts)"}


def build_session_graph():
    builder = StateGraph(SessionState)
    builder.add_node(
        "open_browser_session",
        open_browser_session,
        # LangGraph re-runs the node when it raises ConnectionError, with backoff.
        retry_policy=RetryPolicy(max_attempts=5, initial_interval=0.1, retry_on=ConnectionError),
    )
    builder.add_edge(START, "open_browser_session")
    builder.add_edge("open_browser_session", END)
    return builder.compile()


if __name__ == "__main__":
    print("=== 1. The retry loop ===")
    graph = build_graph()
    for test_name in SCRIPTED_OUTCOMES:
        print(f"\n{test_name}")
        for step in graph.stream({"test_name": test_name}, stream_mode="updates"):
            if "run_test" in step:
                update = step["run_test"]
                print(f"   attempt {update['attempts']}: {update['history'][0]}")
            if "decide_verdict" in step:
                print(f"   verdict: {step['decide_verdict']['verdict']}")

    print("\n=== 2. Guardrail: a buggy router that forgot the attempt limit ===")

    def retry_forever(state: RetryState) -> str:
        return "retry" if state["history"][-1] == "fail" else "done"

    try:
        build_graph(router=retry_forever).invoke({"test_name": "test_checkout"}, {"recursion_limit": 10})
    except GraphRecursionError as error:
        print(f"Stopped by recursion_limit: {str(error).splitlines()[0]}")

    print("\n=== 3. RetryPolicy: automatic retries for exceptions ===")
    print(build_session_graph().invoke({})["session"])
