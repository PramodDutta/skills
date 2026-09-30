# LangGraph for QA Engineers

Learn LangGraph by building the QA tools you wish you had: a test report summarizer, a failure
triage router, a flaky-test retrier, an AI test case generator, an API testing agent, and more.
The course ends with an **autonomous API QA agent** that finds real bugs, asks for your approval,
and files them.

- **For:** QA engineers and SDETs who know basic Python (functions, dicts, `pytest`, `requests`).
  No AI background needed.
- **Runs offline:** every lesson works **without an API key**, using a scripted fake LLM. Add an
  `ANTHROPIC_API_KEY` and the same code runs on Claude.
- **Real QA use cases only:** JUnit reports, flaky tests, triage, test design, API testing, Jira.
- **Tested:** the course ships with its own test suite, and Lesson 10 uses it to teach you how to
  **test AI agents**.
- **Bugs included:** a local practice API with 4 planted bugs for your agents to hunt.

## Setup (5 minutes)

```bash
cd langgraph-for-qa
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python lessons/l01_first_graph.py    # your first graph!
```

You need Python 3.10 or newer (tested on 3.11).

**Optional: use Claude.** Get a key at https://platform.claude.com and:

```bash
export ANTHROPIC_API_KEY=sk-ant-...  # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | not set | set: lessons call Claude. Not set: offline fake model |
| `QA_MODEL` | `claude-opus-5-5` | any Claude model id, e.g. `claude-sonnet-5-5`, `claude-haiku-4-5` |
| `QA_EFFORT` | `medium` | how hard Claude thinks: `low`, `medium`, `high`, `xhigh`, `max` |
| `QA_OFFLINE` | not set | `1` forces offline mode even when a key is set |

All model settings live in one file: [`lessons/qa_llm.py`](lessons/qa_llm.py).

## The roadmap

| # | Lesson | You build | LangGraph concepts | LLM |
|---|---|---|---|---|
| 1 | [Your first graph](lessons/l01_first_graph.md) | Test report summarizer (JUnit XML to GO/NO-GO) | State, nodes, edges, `compile`, `invoke`, `stream` | none |
| 2 | [Conditional edges](lessons/l02_conditional_routing.md) | Failure triage router | `add_conditional_edges`, reducers, `batch` | none |
| 3 | [Loops](lessons/l03_loops_retry.md) | Flaky test retrier | cycles, `recursion_limit`, `RetryPolicy` | none |
| 4 | [Adding Claude](lessons/l04_llm_testcase_generator.md) | Test case generator with a quality gate | LLM nodes, structured output, generator/reviewer loop | yes |
| 5 | [Tools and agents](lessons/l05_tools_api_agent.md) | API test agent | `@tool`, `ToolNode`, `tools_condition`, ReAct, `create_agent` | yes |
| 6 | [Memory and checkpoints](lessons/l06_memory_checkpoints.md) | Resumable regression run | checkpointers, `thread_id`, resume, time travel | part 2 |
| 7 | [Human-in-the-loop](lessons/l07_human_in_the_loop.md) | Bug report approval by the QA lead | `interrupt`, `Command(resume=...)` | yes |
| 8 | [Parallel execution](lessons/l08_parallel_send.md) | Parallel API test runner | `Send`, map-reduce, `max_concurrency` | none |
| 9 | [Multi-agent](lessons/l09_multi_agent_qa_team.md) | A QA team with a Test Lead | supervisor, `Command(goto=...)`, subgraphs | yes |
| 10 | [Testing your agents](lessons/l10_testing_your_agents.md) | The test suite for this whole course | fake LLMs, path, structure and contract tests, evals | live tests only |
| ★ | [Capstone](lessons/capstone_api_qa_agent.md) | Autonomous API QA agent | everything above | yes |

"yes" means the lesson uses an LLM. Offline mode still runs it end to end.

**Suggested pace:** one lesson a day, 30 to 60 minutes each. Read the notes, run the code,
do two exercises, then answer the interview questions at the bottom without peeking.

## Cheat sheet

```python
from typing import Annotated, TypedDict
import operator
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.types import Command, Send, interrupt, RetryPolicy
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolNode, tools_condition
from langchain.agents import create_agent

class State(TypedDict):
    results: Annotated[list, operator.add]         # reducer: append instead of overwrite (L2)

builder = StateGraph(State)
builder.add_node("run", run_fn, retry_policy=RetryPolicy(retry_on=ConnectionError))     # L1, L3
builder.add_edge(START, "run")                                                          # L1
builder.add_conditional_edges("run", router_fn, {"retry": "run", "done": END})          # L2, L3
builder.add_conditional_edges("plan", lambda s: [Send("worker", x) for x in s["items"]], ["worker"])  # L8
graph = builder.compile(checkpointer=InMemorySaver())                                   # L6

config = {"configurable": {"thread_id": "run-1"}, "recursion_limit": 25}
graph.invoke({"results": []}, config)             # run
graph.stream(inputs, config, stream_mode="updates")   # watch step by step
graph.get_state(config).next                      # where is it paused / where did it crash?
graph.invoke(None, config)                        # resume after a crash (L6)
answer = interrupt({"draft": ...})                # inside a node: pause for a human (L7)
graph.invoke(Command(resume="approve"), config)   # continue with the human's answer (L7)
return Command(goto="bug_reporter", update={...}) # inside a node: update + route (L9)
llm.with_structured_output(Schema, method="json_schema")   # typed LLM output (L4)
```

## The practice API

[`lessons/booking_api.py`](lessons/booking_api.py) is a local mini clone of **restful-booker**,
the well-known API-testing practice app. The lessons start it automatically on a free port. To
explore it yourself with curl or Postman:

```bash
python lessons/booking_api.py        # http://127.0.0.1:3001 - prints the spec
curl -i http://127.0.0.1:3001/booking/1
```

It has **4 planted bugs**. The spoilers are at the very bottom of the file, so let your agents find them first.

## Tests

```bash
pytest              # 66 offline tests: nodes, routes, loops, agents, checkpoints, HITL, contracts, scripts
pytest -m live      # real-Claude evals, incl. LLM-as-judge (needs ANTHROPIC_API_KEY; costs money)
```

## Project layout

```text
langgraph-for-qa/
├── README.md                        <- you are here
├── requirements.txt
├── pytest.ini
├── lessons/
│   ├── qa_llm.py                    <- get_llm(): Claude, or the offline fake
│   ├── booking_api.py               <- the practice API (with planted bugs)
│   ├── l01_first_graph.py / .md     <- code + notes for each lesson
│   ├── ...
│   ├── l10_testing_your_agents.md
│   └── capstone_api_qa_agent.py / .md
└── tests/                           <- Lesson 10 in action
```

## Versions

Tested in September 2026 with `langgraph 1.2.12`, `langchain 1.4.3`, `langchain-core 1.6.6`,
`langchain-anthropic 1.7.5` on Python 3.11. If you follow older tutorials, watch out for:

- `from langgraph.prebuilt import create_react_agent` is deprecated. Use `from langchain.agents import create_agent`.
- `ChatAnthropic(temperature=0)` is rejected by the newest Claude models (Opus 5.5, Sonnet 5.5). Use
  structured output plus deterministic checks for reliability instead (Lesson 4).
- Checkpointers only restore custom classes you allow-list, so keep checkpointed state as plain data (Lesson 7).
