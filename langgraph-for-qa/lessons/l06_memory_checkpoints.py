"""
Lesson 6 - Memory & Checkpoints: resume a crashed regression run
================================================================
A 2-hour regression run dies at minute 90 because the Selenium grid went down.
Without checkpoints you start again from zero. With a checkpointer, LangGraph saves
the state after every step: fix the grid, then RESUME from the failed stage.
Stages that already passed are NOT re-run.

  START -> setup_env -> smoke_tests -> api_tests -> ui_tests -> publish_report -> END
                                                      ^ crashes on the first run

New concepts:
  * compile(checkpointer=InMemorySaver())   save the state after every step
  * thread_id                               one saved run (or conversation) per id
  * get_state / get_state_history           where is the run now? what happened, step by step?
  * invoke(None, config)                    resume after a failure
  * update_state + invoke from an old checkpoint = "time travel" (fork a run)
  * the same mechanism gives a chat assistant its memory (Part 2)

Run it:
  python lessons/l06_memory_checkpoints.py
"""

import operator
import re
from collections import Counter
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from qa_llm import get_llm

executions = Counter()  # how many times each stage really ran (to prove nothing re-runs)
grid = {"up": True}     # the lesson switches the Selenium grid off to cause a crash


class RunState(TypedDict):
    build: str
    channel: str
    results: Annotated[list[str], operator.add]
    report: str


def setup_env(state: RunState) -> dict:
    executions["setup_env"] += 1
    return {"results": [f"setup: staging deployed with build {state['build']}"]}


def smoke_tests(state: RunState) -> dict:
    executions["smoke_tests"] += 1
    return {"results": ["smoke: 12/12 passed"]}


def api_tests(state: RunState) -> dict:
    executions["api_tests"] += 1
    return {"results": ["api: 48/48 passed"]}


def ui_tests(state: RunState) -> dict:
    executions["ui_tests"] += 1
    if not grid["up"]:
        raise ConnectionError("selenium-grid:4444 is not responding")
    return {"results": ["ui: 35/36 passed (1 known issue)"]}


def publish_report(state: RunState) -> dict:
    executions["publish_report"] += 1
    body = "\n".join(f"  - {line}" for line in state["results"])
    return {"report": f"Regression report for build {state['build']} -> {state['channel']}\n{body}"}


def build_graph(checkpointer=None):
    builder = StateGraph(RunState)
    builder.add_sequence([setup_env, smoke_tests, api_tests, ui_tests, publish_report])
    builder.add_edge(START, "setup_env")
    # The ONLY change needed for persistence: give compile() a checkpointer.
    return builder.compile(checkpointer=checkpointer or InMemorySaver())


# --- Part 2: memory for a chat assistant is the same mechanism --------------------
def build_assistant(llm=None):
    llm = llm or get_llm(offline=offline_assistant)

    def assistant(state: MessagesState) -> dict:
        return {"messages": [llm.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("assistant", assistant)
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    return builder.compile(checkpointer=InMemorySaver())


def offline_assistant(messages):
    """Pretend LLM: it can only answer from what is in the conversation history."""
    history = " ".join(m.text for m in messages if isinstance(m, HumanMessage))
    build = re.search(r"build (\d+\.\d+\.\d+)", history)
    if "which build" in messages[-1].text.lower():
        return f"You are testing build {build.group(1)}." if build else "I don't know yet - which build is it?"
    return "Noted."


if __name__ == "__main__":
    graph = build_graph()
    config = {"configurable": {"thread_id": "nightly-2026-09-30"}}

    print("=== 1. Start the nightly run... and the grid goes down ===")
    grid["up"] = False
    try:
        graph.invoke({"build": "2.4.1", "channel": "#qa-nightly"}, config)
    except ConnectionError as error:
        print(f"CRASH in ui_tests: {error}")

    print("\n=== 2. Inspect the saved state ===")
    snapshot = graph.get_state(config)
    print(f"next stage to run: {snapshot.next}")
    print(f"results so far:    {snapshot.values['results']}")

    print("\n=== 3. Fix the grid and RESUME (invoke with None + the same thread_id) ===")
    grid["up"] = True
    final = graph.invoke(None, config)
    print(final["report"])
    print(f"stage executions: {dict(executions)}   <- smoke and api ran only ONCE")

    print("\n=== 4. The audit trail: get_state_history (newest first) ===")
    for checkpoint in graph.get_state_history(config):
        print(f"  step {checkpoint.metadata['step']:>2}  next={checkpoint.next}  "
              f"results={len(checkpoint.values.get('results', []))}")

    print("\n=== 5. Time travel: re-publish to another channel WITHOUT re-running tests ===")
    before_publish = next(c for c in graph.get_state_history(config) if c.next == ("publish_report",))
    fork = graph.update_state(before_publish.config, {"channel": "#release-managers"})
    print(graph.invoke(None, fork)["report"].splitlines()[0])
    print(f"stage executions: {dict(executions)}   <- only publish_report ran again")

    print("\n=== Part 2: chat memory = checkpoints + thread_id ===")
    assistant = build_assistant()
    sprint = {"configurable": {"thread_id": "sprint-42"}}
    other = {"configurable": {"thread_id": "sprint-43"}}
    assistant.invoke({"messages": [HumanMessage("We are testing build 3.0.0 on staging today.")]}, sprint)
    reply = assistant.invoke({"messages": [HumanMessage("Which build am I testing?")]}, sprint)
    print(f"[thread sprint-42] {reply['messages'][-1].text}")
    reply = assistant.invoke({"messages": [HumanMessage("Which build am I testing?")]}, other)
    print(f"[thread sprint-43] {reply['messages'][-1].text}")
