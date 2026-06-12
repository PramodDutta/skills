---
name: tiered-model-orchestration
description: Run large or multi-phase tasks as a tiered workflow - the top model (Fable/Opus) orchestrates while subagents on cheaper models (Sonnet, Haiku) do the bulk of the work, stretching usage limits without sacrificing quality. Use this skill whenever the user mentions hitting rate limits or usage limits, wants to "save tokens", asks to orchestrate or delegate work across models, says "use subagents", or gives any big task (multi-file refactor, full feature build, large research sweep, repo-wide analysis) that would burn significant tokens if done in a single session. Also use proactively when a task clearly decomposes into independent phases, even if the user never mentions limits or models.
---

# Tiered Model Orchestration

## Why this works

Usage limits are metered by token cost, weighted by model. Every token the top-tier model spends - including reading files, scanning search results, and writing boilerplate - costs multiples of what Sonnet or Haiku would spend on the same work. The expensive model's actual edge is judgment: decomposing the task, deciding what matters, catching errors, integrating results. So keep the expensive model's turns short and high-leverage, and push token-heavy work down to subagents on cheaper models.

This is a reduction, not a free lunch: every orchestration turn still costs top-tier tokens. The win comes from never letting the orchestrator do work a cheaper model could do.

## Routing table

| Tier | Model | Use for |
|------|-------|---------|
| Orchestrator | Fable / Opus (session model) | Task decomposition, ambiguity resolution, integrating subagent results, final review, anything requiring full conversation context |
| Hard reasoning | `opus` | Architecture decisions, gnarly debugging, security analysis, algorithm design, tasks a Sonnet subagent already failed |
| Workhorse (default) | `sonnet` | Implementation, refactors, tests, docs, most code generation, summarization, drafting |
| Grunt work | `haiku` | Codebase/file searches, inventories, log scanning, mechanical transforms, format conversions, fan-out reads |

Default to `sonnet` for any subagent. Route to `opus` only when the task is *obviously* hard or a Sonnet attempt came back wrong. Route to `haiku` when the task is mechanical and the output is easy to verify. Never route routine work upward "to be safe" - that defeats the purpose.

## Mechanics

Spawn subagents with the Agent/Task tool and set the `model` parameter (`"opus"`, `"sonnet"`, `"haiku"`). Key practices:

1. **Self-contained prompts.** Subagents don't see the conversation. Include every file path, constraint, naming convention, and acceptance criterion in the prompt. A vague prompt to a cheap model produces expensive rework.
2. **Ask for conclusions, not dumps.** Tell the subagent exactly what to return ("the list of files that import X and one-line reason each", not "everything you found"). The orchestrator pays top-tier rates to read whatever comes back.
3. **Batch independent work.** Launch parallel subagents in a single message so they run concurrently. Sequential spawning wastes wall-clock time and adds orchestrator turns.
4. **Don't duplicate reads.** If a subagent read the files, trust its summary. The orchestrator re-reading the same files at top-tier prices is the most common leak.
5. **Reasoning effort is a multiplier, not a default.** High/max reasoning increases token burn. Keep the orchestrator's effort at default; request deep reasoning only inside the specific Opus subagent that needs it.

## Escalation rules

- Start one tier cheaper than instinct suggests; escalate only on failure or visible quality gaps.
- When escalating, pass the failed attempt to the higher-tier subagent ("Sonnet produced X, which is wrong because Y") - context from failure makes the expensive retry cheap.
- One escalation per subtask. If Opus also fails, the problem is the prompt or the decomposition - fix that at the orchestrator level instead of retrying.

## Standard workflow

For a substantial task, run these phases:

1. **Plan (orchestrator).** Decompose into independent subtasks with explicit inputs/outputs. Write a short plan; this is the highest-leverage tokens spent all session.
2. **Recon (haiku, parallel).** Map relevant files, configs, existing patterns. Conclusions only.
3. **Build (sonnet, parallel).** One subagent per independent subtask, each with a self-contained prompt built from the recon results.
4. **Hard spots (opus, only if flagged).** Anything the plan marked as genuinely difficult, or any Sonnet output that failed review.
5. **Integrate and verify (orchestrator + sonnet).** Orchestrator reviews summaries and diffs; spawn a Sonnet verification subagent to run tests/checks rather than reading everything directly.

## Examples

**"Add input validation across all 40 API endpoints"**
Haiku: inventory endpoints and current validation per file → Sonnet x4 (parallel): implement validation for 10 endpoints each, with the convention spelled out in the prompt → Sonnet: run test suite, report failures → Orchestrator: review summary, handle the 2 endpoints with weird auth flows itself or via one Opus subagent.

**"Why is checkout intermittently failing in prod?"**
Haiku: pull and filter relevant logs/traces into a timeline → Sonnet: correlate timeline with recent deploys and config changes → Opus: reason over the assembled evidence to identify root cause (this is the genuinely hard step) → Orchestrator: validate the explanation against the evidence, propose the fix.

**Counter-example - don't orchestrate:** "Rename this function and update its 3 call sites." Single-sitting tasks are cheaper done directly; the overhead of spawning and briefing subagents exceeds the savings.
