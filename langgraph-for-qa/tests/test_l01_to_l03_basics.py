"""Lessons 1-3: nodes are plain functions, so most tests need no graph at all."""

import pytest
from langgraph.errors import GraphRecursionError

import l01_first_graph as l01
import l02_conditional_routing as l02
import l03_loops_retry as l03


# --- Lesson 1: unit-test nodes directly, then the whole graph --------------------------
def test_parse_junit_reads_every_status():
    results = l01.parse_junit({"junit_xml": l01.SAMPLE_JUNIT_XML})["results"]
    statuses = [r["status"] for r in results]
    assert len(results) == 10
    assert statuses.count("failed") == 3  # 2 failures + 1 error
    assert statuses.count("skipped") == 1


def test_pass_rate_ignores_skipped_tests():
    results = [{"status": "passed"}, {"status": "failed"}, {"status": "skipped"}]
    assert l01.compute_stats({"results": results})["stats"]["pass_rate"] == 50.0


def test_report_says_no_go_when_tests_fail():
    report = l01.build_graph().invoke({"junit_xml": l01.SAMPLE_JUNIT_XML})["report"]
    assert "Release verdict: NO-GO" in report
    assert "test_checkout_with_upi (15.0s)" in report


def test_report_says_go_when_everything_passes():
    xml = '<testsuite><testcase classname="t" name="a" time="1"/><testcase classname="t" name="b" time="2"/></testsuite>'
    report = l01.build_graph().invoke({"junit_xml": xml})["report"]
    assert "Release verdict: GO" in report
    assert "Pass rate 100.0%" in report


def test_graph_structure_is_a_straight_line():
    """A structure test catches accidental rewiring of the graph."""
    edges = {(e.source, e.target) for e in l01.build_graph().get_graph().edges}
    assert edges == {
        ("__start__", "parse_junit"), ("parse_junit", "compute_stats"),
        ("compute_stats", "find_slow_tests"), ("find_slow_tests", "write_report"),
        ("write_report", "__end__"),
    }


# --- Lesson 2: one parametrized test covers every route ---------------------------------
@pytest.mark.parametrize("failure, expected", [
    (l02.FAILURES[0], "product_bug"),
    (l02.FAILURES[1], "flaky_test"),
    (l02.FAILURES[2], "environment"),
    (l02.FAILURES[3], "test_code_bug"),
    (l02.FAILURES[4], "product_bug"),
])
def test_every_failure_is_routed_to_the_right_category(failure, expected):
    assert l02.classify_failure(failure)["category"] == expected


@pytest.mark.parametrize("runs, flips", [
    (["fail"], 0), (["pass", "pass", "fail"], 1), (["pass", "fail", "pass", "fail"], 3),
])
def test_count_flips(runs, flips):
    assert l02.count_flips(runs) == flips


def test_flaky_failure_takes_the_quarantine_path():
    """A path test: which nodes ran, in which order?"""
    steps = l02.build_graph().stream(l02.FAILURES[1], stream_mode="updates")
    assert [node for step in steps for node in step] == ["classify_failure", "quarantine_test", "notify_team"]


def test_actions_reducer_keeps_the_full_audit_trail():
    actions = l02.build_graph().invoke(l02.FAILURES[2])["actions"]
    assert len(actions) == 3  # classify + branch + notify: appended, not overwritten


# --- Lesson 3: loops and their guards -------------------------------------------------------
@pytest.mark.parametrize("test_name, verdict, attempts", [
    ("test_login", "PASSED", 1),
    ("test_search_suggestions", "FLAKY", 3),
    ("test_checkout", "FAILED", 3),
])
def test_retry_verdicts(no_sleep, test_name, verdict, attempts):
    final = l03.build_graph().invoke({"test_name": test_name})
    assert (final["verdict"], final["attempts"]) == (verdict, attempts)


def test_a_loop_without_a_limit_is_stopped_by_recursion_limit(no_sleep):
    def retry_forever(state):
        return "retry" if state["history"][-1] == "fail" else "done"

    with pytest.raises(GraphRecursionError):
        l03.build_graph(router=retry_forever).invoke({"test_name": "test_checkout"}, {"recursion_limit": 10})


def test_retry_policy_recovers_from_transient_errors(no_sleep, monkeypatch):
    monkeypatch.setitem(l03.grid_calls, "count", 0)
    assert "3 attempts" in l03.build_session_graph().invoke({})["session"]
