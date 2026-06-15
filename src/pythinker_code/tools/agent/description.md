Start a subagent instance to work on a focused task.

The Agent tool can either create a new subagent instance or resume an existing one by `agent_id`.
Each instance keeps its own context history under the current session, so repeated use of the same
instance can preserve previous findings and work.

**Available Built-in Agent Types**

${BUILTIN_AGENT_TYPES_MD}

**Usage**

- Always provide a short `description` (3-5 words).
- Use `subagent_type` to select a built-in agent type. If omitted, `coder` is used.
- Use `model` when you need to override the built-in type's default model or the parent agent's current model.
- Use `resume` when you want to continue an existing instance instead of starting a new one.
- If an existing subagent already has relevant context or the task is a continuation of its prior work, prefer `resume` over creating a new instance.
- Default to foreground execution. Use `run_in_background=true` only when the task can continue independently, you do not need the result immediately, and there is a clear benefit to returning control before it finishes.
- If your only next step is to wait for and synthesize the results (e.g. parallel reviews feeding one report), run in the foreground — `RunAgents` foreground children still execute concurrently and return results inline, with no polling or notification handling. Reserve background for when you have other work to do while children run.
- Be explicit about whether the subagent should write code, only research, review, or verify.
- Provide the subagent all required context and success criteria. New subagents do not inherit your transcript automatically.
- Brief the agent like a capable teammate joining mid-task: state the goal, why it matters, what you already learned or ruled out, exact paths/commands when known, and the output format you need.
- Include a prompt packet for non-trivial work: Goal, Evidence/Context, Scope and non-goals, Constraints, Expected Output, Verification, and Risks/Blockers.
- For review or fix orchestration, prefer durable feature/finding IDs when available (`pythinker review map`, `review`, `next`, `show`). They make delegated work bounded, resumable, and evidence-checkable.
- Keep each delegated prompt to one objective. Split unrelated goals into separate agents so each result is reviewable.
- Do not delegate synthesis with vague prompts such as "based on your findings, fix it". First understand the finding yourself, then give the subagent a concrete scoped task.
- Spawn multiple subagents in the same turn when they can investigate independent regions concurrently, but keep background launches within available task slots.
- For thorough large-codebase exploration, prefer scoped questions over one broad scan, and pass an explicit longer `timeout` (for example 1800-3600 seconds) when using background agents. If an agent times out, do not relaunch the same broad prompt unchanged; use targeted direct scans or resume the saved agent with a narrower continuation prompt.
- Cross-check at least one load-bearing subagent finding before making changes from it.
- The subagent result is only visible to you. If the user should see it, summarize it yourself.

**Prompt Hygiene**

When spawning a fresh agent, brief it like a smart colleague who just walked in: include the goal,
what was tried, what is in and out of scope, the expected output contract, and how the result will
be verified. For lookups, pass the exact command or symbol; for investigations, pass the question,
not a prescribed sequence of steps.

**Agent Workflow Design**

Use subagents as focused logical roles, not just extra tool capacity:

- `explore` / scout: collect facts, relevant files, constraints, and risks. Read-only.
- `plan`: turn gathered context into an implementation plan. Read-only.
- `coder`: general software engineering work when the brief still needs judgment.
- `implementer`: land a specific, already-scoped change with minimum edits.
- `review`: read and grade changed code with severity-scored findings.
- `verifier`: run validation gates and report PASS / FAIL / FLAKY without fixing.
- `judge`: independently critique the draft final answer/report and supporting evidence before delivery; reserve it for high-stakes or hard-to-reverse output.

Recommended workflows:

- Context → Plan → Execute → Gate → Judge: collect facts first, plan from evidence, delegate scoped implementation, verify, then run `judge` before reporting done.
- Scout → Plan → Implement: run `explore`, then `plan` with the explorer's findings, then `implementer` or `coder` with the plan.
- Implement → Review → Fix → Verify → Judge: run `implementer`, then `review`, then resume/launch `implementer` to apply feedback, then `verifier` for the relevant gate, then `judge` for final answer/report quality.
- Parallel scouting: launch multiple `explore` agents for independent questions, then synthesize their findings before editing. If a background batch exceeds available slots, RunAgents launches what fits and reports deferred children for a follow-up batch.
- Parallel review/verification: when review and tests do not depend on each other, run `review` and `verifier` concurrently, then pass both summaries to `judge`.
- Stateful review loop: for repo-wide quality work, map feature slices first, review bounded slices, triage findings, then dispatch one fix at a time with the finding ID and minimum fix scope. Revalidate before claiming fixed.

When chaining manually, include the previous agent's summary in the next agent prompt. Newly-created
subagents do not see your current context automatically.

**Explore Agent — Preferred for Codebase Research**

When you need to understand the codebase before making changes, fixing bugs, or planning features,
prefer `subagent_type="explore"` over doing the search yourself. The explore agent is optimized for
fast, read-only codebase investigation. Use it when:
- Your task will clearly require more than 3 search queries
- You need to understand how a module, feature, or code path works
- You are about to enter plan mode and want to gather context first
- You want to investigate multiple independent questions — launch multiple explore agents concurrently

When calling explore, specify the desired thoroughness in the prompt:
- "quick": targeted lookups — find a specific file, function, or config value
- "medium": understand a module — how does auth work, what calls this API
- "thorough": cross-cutting analysis — architecture overview, dependency mapping, multi-module investigation

**When Not To Use Agent**

- Reading a known file path
- Searching a small number of known files
- Tasks that can be completed in one or two direct tool calls

**Effort Scaling — How Many Agents To Spawn**

Match the number of parallel agents to the task's independent subparts, not to ambition:

- Trivial / known path (read a file, one lookup) → no subagent; use direct tools.
- A single open-ended question → 1 `explore` agent.
- A bounded comparison, or 2-3 genuinely independent regions → 2-4 agents.
- Only genuinely broad, cross-cutting work → more, up to the `RunAgents` cap of 8.

Prefer the fewest children that cover the independent objectives — the cap of 8 is a ceiling, not a target. Over-provisioning burns the multi-agent token premium (a fan-out can cost several times a single thread) and produces results you then have to reconcile. Do not launch a subagent for what one or two direct reads or greps would answer.

When spawning a fresh agent, brief it like a smart colleague who just walked in — include the goal, what was tried, what is in/out of scope, the expected output contract, and how the result will be verified. Lookups: pass the exact command. Investigations: pass the question, not prescribed steps.
