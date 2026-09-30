"""
Lesson 8 - Parallel execution: run API tests like pytest-xdist, with Send
=========================================================================
  START -> plan_tests -> run_test x N (in PARALLEL, one per test case) -> summarize -> END

New concepts:
  * Send("node", payload)   start a node once per item - the number of branches is decided at runtime
  * each worker gets its OWN small input (one test case), not the whole state
  * a reducer collects the parallel results (map -> reduce)
  * max_concurrency         cap the number of parallel workers (like pytest -n 4)

Run it:
  python lessons/l08_parallel_send.py
"""

import operator
import time
from typing import Annotated, TypedDict

import requests
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from booking_api import VALID_BOOKING, start_booking_api

SIMULATED_TEST_SECONDS = 0.3  # the local API answers in ~1 ms; real tests are slower

_NO_FIRSTNAME = {k: v for k, v in VALID_BOOKING.items() if k != "firstname"}
_DATES_REVERSED = {**VALID_BOOKING, "bookingdates": {"checkin": "2026-02-10", "checkout": "2026-02-01"}}

TEST_CASES = [
    {"id": "TC01", "name": "health check", "method": "GET", "path": "/ping", "expect": 201, "tags": ["smoke"]},
    {"id": "TC02", "name": "list bookings", "method": "GET", "path": "/booking", "expect": 200, "tags": ["smoke"]},
    {"id": "TC03", "name": "get existing booking", "method": "GET", "path": "/booking/1", "expect": 200, "tags": ["smoke"]},
    {"id": "TC04", "name": "get unknown booking", "method": "GET", "path": "/booking/999999", "expect": 404, "tags": []},
    {"id": "TC05", "name": "get non-numeric id", "method": "GET", "path": "/booking/abc", "expect": 404, "tags": []},
    {"id": "TC06", "name": "create valid booking", "method": "POST", "path": "/booking", "body": VALID_BOOKING, "expect": 200, "tags": ["smoke"]},
    {"id": "TC07", "name": "create without firstname", "method": "POST", "path": "/booking", "body": _NO_FIRSTNAME, "expect": 400, "tags": []},
    {"id": "TC08", "name": "create with negative price", "method": "POST", "path": "/booking", "body": {**VALID_BOOKING, "totalprice": -50}, "expect": 400, "tags": []},
    {"id": "TC09", "name": "create with checkout before checkin", "method": "POST", "path": "/booking", "body": _DATES_REVERSED, "expect": 400, "tags": []},
    {"id": "TC10", "name": "update without token", "method": "PUT", "path": "/booking/1", "body": VALID_BOOKING, "expect": 403, "tags": []},
    {"id": "TC11", "name": "update with token", "method": "PUT", "path": "/booking/2", "body": VALID_BOOKING, "auth": True, "expect": 200, "tags": []},
    {"id": "TC12", "name": "delete with token", "method": "DELETE", "path": "/booking/3", "auth": True, "expect": 201, "tags": []},
]


class SuiteState(TypedDict):
    base_url: str
    tag: str                                      # "smoke" or "regression" (= everything)
    token: str
    selected: list[dict]
    results: Annotated[list[dict], operator.add]  # REDUCER: every worker appends its result
    summary: str


class WorkerInput(TypedDict):  # what ONE parallel worker receives
    base_url: str
    token: str
    case: dict


def plan_tests(state: SuiteState) -> dict:
    """Select the tests and log in ONCE - like a session-scoped pytest fixture."""
    selected = [c for c in TEST_CASES if state["tag"] == "regression" or state["tag"] in c["tags"]]
    token = requests.post(f"{state['base_url']}/auth", json={"username": "admin", "password": "password123"},
                          timeout=10).json()["token"]
    return {"selected": selected, "token": token}


def fan_out(state: SuiteState) -> list[Send]:
    """One Send per test case: LangGraph runs all of them in the same step, in parallel."""
    return [Send("run_test", {"base_url": state["base_url"], "token": state["token"], "case": case})
            for case in state["selected"]]


def run_test(job: WorkerInput) -> dict:
    case = job["case"]
    time.sleep(SIMULATED_TEST_SECONDS)
    response = requests.request(case["method"], job["base_url"] + case["path"], json=case.get("body"),
                                cookies={"token": job["token"]} if case.get("auth") else None, timeout=10)
    return {"results": [{"id": case["id"], "name": case["name"], "expected": case["expect"],
                         "actual": response.status_code, "passed": response.status_code == case["expect"]}]}


def summarize(state: SuiteState) -> dict:
    results = sorted(state["results"], key=lambda r: r["id"])
    failed = [r for r in results if not r["passed"]]
    lines = [f"{len(results) - len(failed)}/{len(results)} passed"]
    lines += [f"  FAIL {r['id']} {r['name']}: expected {r['expected']}, got {r['actual']}" for r in failed]
    return {"summary": "\n".join(lines)}


def build_graph():
    builder = StateGraph(SuiteState)
    builder.add_node("plan_tests", plan_tests)
    builder.add_node("run_test", run_test)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "plan_tests")
    builder.add_conditional_edges("plan_tests", fan_out, ["run_test"])  # the fan-out
    builder.add_edge("run_test", "summarize")  # the fan-in: waits for ALL workers
    builder.add_edge("summarize", END)
    return builder.compile()


if __name__ == "__main__":
    graph = build_graph()
    runs = [
        ("smoke, parallel", "smoke", {}),
        ("regression, sequential (max_concurrency=1)", "regression", {"max_concurrency": 1}),
        ("regression, 4 workers  (max_concurrency=4)", "regression", {"max_concurrency": 4}),
        ("regression, parallel", "regression", {}),
    ]
    for label, tag, config in runs:
        # A fresh API per run: parallel tests must never depend on each other's data.
        with start_booking_api() as api:
            started = time.perf_counter()
            result = graph.invoke({"base_url": api.base_url, "tag": tag}, config)
            seconds = time.perf_counter() - started
        print(f"{label:45} {len(result['results']):>2} tests in {seconds:.2f}s")

    print(f"\nRegression summary:\n{result['summary']}")
