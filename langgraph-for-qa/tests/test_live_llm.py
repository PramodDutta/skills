"""Live tests: the REAL model. Skipped by default - they cost money and are non-deterministic.

Run them on purpose (e.g. nightly, or before changing a prompt):
    ANTHROPIC_API_KEY=sk-ant-... pytest -m live
"""

import os

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import l04_llm_testcase_generator as l04
from qa_llm import get_llm

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"),
]


@pytest.fixture(autouse=True)
def real_llm(monkeypatch):
    monkeypatch.delenv("QA_OFFLINE", raising=False)


def test_claude_suite_passes_the_quality_gate():
    """Assert PROPERTIES of the output, never exact text: the wording changes every run."""
    final = l04.build_graph().invoke({"user_story": l04.USER_STORY})
    assert final["review_issues"] == []
    assert final["drafts"] <= l04.MAX_DRAFTS


class Judgement(BaseModel):
    score: int = Field(description="1 (unusable) to 5 (a new tester could run it without questions)")
    problems: list[str] = Field(description="Concrete problems, empty if none")


JUDGE_PROMPT = """You are a strict QA lead reviewing manual test cases.
Score how clear and executable they are for a tester who is new to the product."""


def test_llm_as_judge_rates_step_clarity():
    """LLM-as-judge: for qualities code can't check, like 'are these steps clear?'."""
    final = l04.build_graph().invoke({"user_story": l04.USER_STORY})
    judge = get_llm(offline=lambda m: None).with_structured_output(Judgement, method="json_schema")
    verdict = judge.invoke([SystemMessage(JUDGE_PROMPT),
                            HumanMessage(final["suite"].model_dump_json(indent=2))])
    assert verdict.score >= 4, verdict.problems
