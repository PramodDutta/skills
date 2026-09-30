"""
Lesson 1 - Your first LangGraph: a Test Report Summarizer
=========================================================
No LLM yet. Just the 4 building blocks that every LangGraph app has:

  1. STATE    a typed dict that flows through the graph (think: the test context)
  2. NODES    plain Python functions: read the state, return the keys they changed
  3. EDGES    arrows that say which node runs next
  4. COMPILE  turn the blueprint into an app, then .invoke() it

The graph:
  START -> parse_junit -> compute_stats -> find_slow_tests -> write_report -> END

Run it:
  python lessons/l01_first_graph.py                 # uses the sample report below
  python lessons/l01_first_graph.py my-junit.xml    # your own: pytest --junitxml=my-junit.xml
"""

import sys
import xml.etree.ElementTree as ET
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

SLOW_SECONDS = 5.0


# 1) STATE ---------------------------------------------------------------------
# Every node receives the whole state and returns only the keys it changed.
class ReportState(TypedDict):
    junit_xml: str         # input: raw JUnit XML text
    results: list[dict]    # parsed test results
    stats: dict            # total / passed / failed / skipped / pass_rate
    slow_tests: list[str]  # tests slower than SLOW_SECONDS
    report: str            # output: the final summary


# 2) NODES ---------------------------------------------------------------------
def parse_junit(state: ReportState) -> dict:
    """Turn JUnit XML (pytest, Maven Surefire, Jenkins...) into a list of results."""
    root = ET.fromstring(state["junit_xml"])
    results = []
    for case in root.iter("testcase"):
        status, message = "passed", ""
        for tag, tag_status in (("failure", "failed"), ("error", "failed"), ("skipped", "skipped")):
            element = case.find(tag)
            if element is not None:
                status, message = tag_status, element.get("message", "")
        results.append({
            "name": f"{case.get('classname')}.{case.get('name')}",
            "status": status,
            "time": float(case.get("time", 0)),
            "message": message,
        })
    return {"results": results}  # only the key this node changed


def compute_stats(state: ReportState) -> dict:
    results = state["results"]
    passed = sum(r["status"] == "passed" for r in results)
    failed = sum(r["status"] == "failed" for r in results)
    skipped = len(results) - passed - failed
    executed = passed + failed
    return {"stats": {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(100 * passed / executed, 1) if executed else 0.0,
    }}


def find_slow_tests(state: ReportState) -> dict:
    slow = [r for r in state["results"] if r["time"] > SLOW_SECONDS]
    slow.sort(key=lambda r: r["time"], reverse=True)
    return {"slow_tests": [f"{r['name']} ({r['time']:.1f}s)" for r in slow]}


def write_report(state: ReportState) -> dict:
    s = state["stats"]
    failures = [f"- {r['name']}: {r['message']}" for r in state["results"] if r["status"] == "failed"]
    slow = [f"- {name}" for name in state["slow_tests"]]
    lines = [
        "# Test Run Summary",
        f"Release verdict: {'GO' if s['failed'] == 0 else 'NO-GO'}",
        f"Total {s['total']} | Passed {s['passed']} | Failed {s['failed']} | "
        f"Skipped {s['skipped']} | Pass rate {s['pass_rate']}%",
        "",
        "## Failures",
        *(failures or ["- none"]),
        "",
        f"## Slow tests (> {SLOW_SECONDS:.0f}s)",
        *(slow or ["- none"]),
    ]
    return {"report": "\n".join(lines)}


# 3) EDGES + 4) COMPILE ---------------------------------------------------------
def build_graph():
    builder = StateGraph(ReportState)

    builder.add_node("parse_junit", parse_junit)
    builder.add_node("compute_stats", compute_stats)
    builder.add_node("find_slow_tests", find_slow_tests)
    builder.add_node("write_report", write_report)

    builder.add_edge(START, "parse_junit")
    builder.add_edge("parse_junit", "compute_stats")
    builder.add_edge("compute_stats", "find_slow_tests")
    builder.add_edge("find_slow_tests", "write_report")
    builder.add_edge("write_report", END)

    return builder.compile()


SAMPLE_JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="ecommerce-regression" tests="10" failures="2" errors="1" skipped="1">
    <testcase classname="tests.test_login" name="test_valid_login" time="1.2"/>
    <testcase classname="tests.test_login" name="test_invalid_password_shows_error" time="0.9"/>
    <testcase classname="tests.test_login" name="test_locked_account" time="0.8">
      <failure message="AssertionError: expected 'Account locked' but got 'Invalid password'"/>
    </testcase>
    <testcase classname="tests.test_search" name="test_search_by_keyword" time="2.1"/>
    <testcase classname="tests.test_search" name="test_search_no_results" time="1.7"/>
    <testcase classname="tests.test_cart" name="test_add_to_cart" time="3.4"/>
    <testcase classname="tests.test_cart" name="test_apply_coupon" time="6.8"/>
    <testcase classname="tests.test_checkout" name="test_checkout_with_card" time="12.3">
      <failure message="AssertionError: expected 'Order confirmed' but got 'Payment declined'"/>
    </testcase>
    <testcase classname="tests.test_checkout" name="test_checkout_with_upi" time="15.0">
      <error message="TimeoutException: payment gateway did not respond in 15s"/>
    </testcase>
    <testcase classname="tests.test_profile" name="test_update_avatar" time="0.0">
      <skipped message="feature flag avatar_v2 is off"/>
    </testcase>
  </testsuite>
</testsuites>
"""

if __name__ == "__main__":
    xml_text = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else SAMPLE_JUNIT_XML
    graph = build_graph()

    print("=== The graph as Mermaid (paste into https://mermaid.live) ===")
    print(graph.get_graph().draw_mermaid())

    print("=== Watch the state flow, one node at a time ===")
    for step in graph.stream({"junit_xml": xml_text}, stream_mode="updates"):
        for node_name, update in step.items():
            print(f"[{node_name}] returned keys: {list(update)}")

    print("\n=== The normal way to run a graph: invoke() -> final state ===")
    final_state = graph.invoke({"junit_xml": xml_text})
    print(final_state["report"])
