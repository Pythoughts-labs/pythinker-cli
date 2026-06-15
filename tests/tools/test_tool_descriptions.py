from __future__ import annotations

# ruff: noqa

import platform
import pytest
from inline_snapshot import snapshot

from pythinker_code.tools.agent import Agent as AgentTool
from pythinker_code.tools.background import TaskList, TaskOutput, TaskStop
from pythinker_code.tools.dmail import SendDMail
from pythinker_code.tools.file.glob import Glob
from pythinker_code.tools.file.grep_local import Grep
from pythinker_code.tools.file.read import ReadFile
from pythinker_code.tools.file.read_media import ReadMediaFile
from pythinker_code.tools.file.replace import StrReplaceFile
from pythinker_code.tools.file.write import WriteFile
from pythinker_code.tools.shell import Shell
from pythinker_code.tools.think import Think
from pythinker_code.tools.todo import SetTodoList
from pythinker_code.tools.web.fetch import FetchURL
from pythinker_code.tools.web.search import SearchWeb


def test_agent_description(agent_tool: AgentTool):
    """Test the description of Agent tool."""
    assert agent_tool.base.description == snapshot(
        """\
Start a subagent instance to work on a focused task.

The Agent tool can either create a new subagent instance or resume an existing one by `agent_id`.
Each instance keeps its own context history under the current session, so repeated use of the same
instance can preserve previous findings and work.

**Available Built-in Agent Types**

- `mocker`: The mock agent for testing purposes. (Tools: *, Model: inherit, Background: yes).

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
"""
    )


def test_send_dmail_description(send_dmail_tool: SendDMail):
    """Test the description of SendDMail tool."""
    assert send_dmail_tool.base.description == snapshot(
        """\
Send a message to the past, just like sending a D-Mail in Steins;Gate.

This tool is provided to enable you to proactively manage the context. You can see some `user` messages with text `CHECKPOINT {checkpoint_id}` wrapped in `<system>` tags in the context. When you feel there is too much irrelevant information in the current context, you can send a D-Mail to revert the context to a previous checkpoint with a message containing only the useful information. When you send a D-Mail, you must specify an existing checkpoint ID from the before-mentioned messages.

Typical scenarios you may want to send a D-Mail:

- You read a file, found it very large and most of the content is not relevant to the current task. In this case you can send a D-Mail immediately to the checkpoint before you read the file and give your past self only the useful part.
- You searched the web, the result is large.
  - If you got what you need, you may send a D-Mail to the checkpoint before you searched the web and put only the useful result in the mail message.
  - If you did not get what you need, you may send a D-Mail to tell your past self to try another query.
- You wrote some code and it did not work as expected. You spent many struggling steps to fix it but the process is not relevant to the ultimate goal. In this case you can send a D-Mail to the checkpoint before you wrote the code and give your past self the fixed version of the code and tell yourself no need to write it again because you already wrote to the filesystem.

After a D-Mail is sent, the system will revert the current context to the specified checkpoint, after which, you will no longer see any messages which you can now see after that checkpoint. The message in the D-Mail will be appended to the end of the context. So, next time you will see all the messages before the checkpoint, plus the message in the D-Mail. You must make it very clear in the message, tell your past self what you have done/changed, what you have learned and any other information that may be useful, so that your past self can continue the task without confusion and will not repeat the steps you have already done.

You must understand that, unlike D-Mail in Steins;Gate, the D-Mail you send here will not revert the filesystem or any external state. That means, you are basically folding the recent messages in your context into a single message, which can significantly reduce the waste of context window.

When sending a D-Mail, DO NOT explain to the user. The user do not care about this. Just explain to your past self.
"""
    )


def test_think_description(think_tool: Think):
    """Test the description of Think tool."""
    assert think_tool.base.description == snapshot(
        """\
Record an explicit reasoning step — a plan, a hypothesis, a trade-off analysis, or a checkpoint before an irreversible or multi-tool action. It obtains no new information, reads or changes nothing, and runs nothing; it only appends your thought to the log.

**When to use:**
- Before a destructive, hard-to-reverse, or multi-step tool sequence, to lay out the plan and the checks first.
- When several pieces of evidence must be reconciled before deciding (e.g. conflicting logs, an ambiguous root cause).
- To checkpoint intermediate conclusions on a long task so they survive later steps.

**When NOT to use:**
- For routine, obvious next actions — just take them. A think step that only restates the task wastes a turn.
- As a substitute for acting: if the next move is clear, call the real tool instead of narrating intent.
"""
    )


def test_set_todo_list_description(set_todo_list_tool: SetTodoList):
    """Test the description of SetTodoList tool."""
    assert set_todo_list_tool.base.description == snapshot(
        """\
# Manage your todo list for tracking task progress during execution.

**When to set todos (Update mode):**
Set the todo list **only after the user has explicitly agreed on the plan**. The todo list marks the start of execution — it is not a planning scratch-pad. Do not call this tool while exploring, gathering context, presenting options, or waiting for user feedback. The moment the user says "yes", "do it", "go ahead", or otherwise confirms the approach, set the list and begin.

**Usage modes:**

- **Update mode**: Pass `todos` to set the entire todo list. The previous list is replaced.
- **Query mode**: Omit `todos` (or pass null) to retrieve the current todo list without changes.
- **Clear mode**: Pass an empty array `[]` to clear all todos when work is fully done.

Once the todo list is set, it is the single source of truth for in-progress work. During execution, update item statuses as you complete work (`pending` → `in_progress` → `done`). When scope evidence makes a planned item irrelevant, mark it `cancelled` (do not silently delete it) so the on-screen plan history stays honest for the watching user — this is the in-list way to express the scope change you should first surface to the user. Only restructure or replace the list when evidence genuinely changes the scope — not for convenience replanning. When in doubt, surface the new evidence to the user before changing the plan.

Once you finish a subtask/milestone, update its status before moving to the next item.

Keep at most one item in_progress at a time for your own sequential work — the only exception is parallel-subagent fan-out, where one in_progress sub-todo per running child is expected. Do not jump an item from `pending` to `done`: set it `in_progress` first, and do not batch-complete multiple items after the fact.

**Do NOT use this tool:**

- During the planning or exploration phase, before the user has confirmed the approach.
- When the user asks a question or requests a review without agreeing to a concrete plan.
- When the task only takes a few steps/tool calls. E.g. "Fix the unit test function 'test_xxx'".
- When the user prompt is very specific and fully self-contained. E.g. "Replace xxx to yyy in file zzz".

**IMPORTANT:** Do not call this tool repeatedly without making real progress between calls. Use Query mode to check current state before updating. If you cannot advance any task, surface the blocker to the user instead of replanning. Repeated todo updates without real work are counterproductive.
"""
    )


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
def test_shell_description(shell_tool: Shell):
    """Test the description of Shell tool."""
    assert shell_tool.base.description == snapshot(
        """\
Execute a bash (`/bin/bash`) command. Use this tool to explore the filesystem, edit files, run scripts, get system information, etc.

**Output:**
The stdout and stderr will be combined and returned as a string. The output may be truncated if it is too long. If the command failed, the exit code will be provided in a system tag.

If `run_in_background=true`, the command will be started as a background task and this tool will return a task ID instead of waiting for command completion. When doing that, provide a short `description`; if omitted, a generic description is used. You will be automatically notified when the task completes. Use `TaskOutput` for a non-blocking status/output snapshot, and only set `block=true` when you explicitly want to wait for completion. Use `TaskStop` only if the task must be cancelled. For human users in the interactive shell, background tasks are managed through `/task` only; do not suggest `/task list`, `/task output`, `/task stop`, `/tasks`, or any other invented shell subcommands.

**Guidelines for safety and security:**
- Each shell tool call will be executed in a fresh shell environment. The shell variables, current working directory changes, and the shell history is not preserved between calls.
- Foreground commands must finish promptly and use `timeout <= 300`. For long-running builds, scans, test suites, watchers, or servers, set `run_in_background=true`, provide a concise `description`, and use a longer `timeout` up to 86400 seconds. If you accidentally request `timeout > 300` without `run_in_background`, the tool will automatically start it as a background task instead of failing validation.
- Avoid using `..` to access files or directories outside of the working directory.
- Avoid modifying files outside of the working directory unless explicitly instructed to do so.
- Never run commands that require superuser privileges unless explicitly instructed to do so.

**Guidelines for efficiency:**
- For multiple related commands, use `&&` to chain them in a single call, e.g. `cd /path && ls -la`
- Use `;` to run commands sequentially regardless of success/failure
- Use `||` for conditional execution (run second command only if first fails)
- Use pipe operations (`|`) and redirections (`>`, `>>`) to chain input and output between commands
- Always quote file paths containing spaces with double quotes (e.g., cd "/path with spaces/")
- Use `if`, `case`, `for`, `while` control flows to execute complex logic in a single call.
- Verify directory structure before create/edit/delete files or directories to reduce the risk of failure.
- Prefer `run_in_background=true` for long-running builds, scans, tests, watchers, or servers when you need the conversation to continue before the command finishes or when the command needs more than 300 seconds.
- After starting a background task, do not guess its outcome. Rely on the automatic completion notification whenever possible. Use `TaskOutput` for non-blocking progress snapshots by default, and set `block=true` only when you intentionally want to wait.
- If you need to tell a human shell user how to manage background tasks, only mention `/task`. Do not invent `/task list`, `/task output`, `/task stop`, or `/tasks`.

**Commands available:**
- Shell environment: cd, pwd, export, unset, env
- File system operations: ls, find, mkdir, rm, cp, mv, touch, chmod, chown
- File viewing/editing: cat, grep, head, tail, diff, patch
- Text processing: awk, sed, sort, uniq, wc
- System information/operations: ps, kill, top, df, free, uname, whoami, id, date
- Network operations: curl, wget, ping, telnet, ssh
- Archive operations: tar, zip, unzip
- Other: Other commands available in the shell environment. Check the existence of a command by running `which <command>` before using it.
"""
    )


def test_task_output_description(task_output_tool: TaskOutput):
    assert task_output_tool.base.description == snapshot(
        """\
Retrieve output from a running or completed background task.

Use this after `Shell(run_in_background=true)` when you need to inspect progress or explicitly wait for completion.

Guidelines:
- Prefer relying on automatic completion notifications. Use this tool only when you need task output before the automatic notification arrives.
- By default this tool is non-blocking and returns a current status/output snapshot.
- Use `block=true` only when you intentionally want to wait for completion or timeout.
- When several background tasks are running, do not `block=true` on a single one — blocking waits only for that task and freezes the turn until the slowest finishes. Return control and rely on the automatic completion notifications.
- This tool returns structured task metadata, a fixed-size output preview, and an `output_path` for the full log.
- When the preview is truncated, use `ReadFile` with the returned `output_path` to inspect the full log in pages.
- This tool works with the generic background task system and should remain the primary read path for future task types, not just bash.
"""
    )


def test_task_list_description(task_list_tool: TaskList):
    assert task_list_tool.base.description == snapshot(
        """\
List background tasks from the current session.

Use this when you need to re-enumerate which background tasks still exist, especially after context compaction or when you are no longer confident which task IDs are still active.

Guidelines:

- Prefer the default `active_only=true` unless you specifically need completed or failed tasks.
- Use `TaskOutput` to inspect one task in detail after you have identified the correct task ID.
- Do not guess which tasks are still running when you can call this tool directly.
- This tool is read-only and safe to use in plan mode.
"""
    )


def test_task_stop_description(task_stop_tool: TaskStop):
    assert task_stop_tool.base.description == snapshot(
        """\
Stop a running background task.

Use this only when a background task must be cancelled. For normal task completion, prefer waiting for the automatic notification or using `TaskOutput`.

Guidelines:
- This is a generic task stop capability, not a bash-specific kill tool.
- Use it sparingly because stopping a task is destructive and may leave partial side effects.
- If the task is already complete, this tool will simply return its current state.
"""
    )


def test_read_file_description(read_file_tool: ReadFile):
    """Test the description of ReadFile tool."""
    assert read_file_tool.base.description == snapshot(
        """\
Read text content from a file.

**Tips:**
- Make sure you follow the description of each tool parameter.
- A `<system>` tag will be given before the read file content.
- The system will notify you when there is anything wrong when reading the file.
- This tool is a tool that you typically want to use in parallel. Always read multiple files in one response when possible.
- This tool can read text files and returns a compact tree listing when `path` is a directory. To read images or videos, use the ReadMediaFile tool. To read other file types, use appropriate commands via the Shell tool.
- If the file doesn't exist or path is invalid, an error will be returned.
- If you want to search for a certain content/pattern, prefer Grep tool over ReadFile.
- Content will be returned with a line number before each line like `cat -n` format.
- Use `line_offset` and `n_lines` parameters when you only need to read a part of the file.
- Use negative `line_offset` to read from the end of the file (e.g. `line_offset=-100` reads the last 100 lines). This is useful for viewing the tail of log files. The absolute value cannot exceed 1000.
- The tool always returns the total number of lines in the file in its message, which you can use to plan subsequent reads.
- The maximum number of lines that can be read at once is 1000.
- A result reporting fewer lines than the file total is a partial read — continue with `line_offset` until you have covered what the task depends on, especially for spec, skill, or checklist files you are implementing against.
- Any lines longer than 2000 characters will be truncated, ending with "...".
"""
    )


def test_read_media_file_description(read_media_file_tool: ReadMediaFile):
    """Test the description of ReadMediaFile tool."""
    assert read_media_file_tool.base.description == snapshot(
        """\
Read media content from a file.

**Tips:**
- Make sure you follow the description of each tool parameter.
- A `<system>` tag will be given before the read file content.
- The system will notify you when there is anything wrong when reading the file.
- This tool is a tool that you typically want to use in parallel. Always read multiple files in one response when possible.
- This tool can only read image or video files. To read other types of files, use the ReadFile tool. To list directories, use the Glob tool or `ls` command via the Shell tool.
- If the file doesn't exist or path is invalid, an error will be returned.
- The maximum size that can be read is 100MB. An error will be returned if the file is larger than this limit.
- The media content will be returned in a form that you can directly view and understand.

**Capabilities**
- This tool supports image and video files for the current model.
"""
    )


def test_glob_description(glob_tool: Glob):
    """Test the description of Glob tool."""
    assert glob_tool.base.description == snapshot(
        """\
Find files and directories using glob patterns. This tool supports standard glob syntax like `*`, `?`, and `**` for recursive searches.

**When to use:**
- Find files matching specific patterns (e.g., all Python files: `*.py`)
- Search for files recursively in subdirectories (e.g., `src/**/*.js`)
- Locate configuration files (e.g., `*.config.*`, `*.json`)
- Find test files (e.g., `test_*.py`, `*_test.go`)

**Example patterns:**
- `*.py` - All Python files in current directory
- `src/**/*.js` - All JavaScript files in src directory recursively
- `test_*.py` - Python test files starting with "test_"
- `*.config.{js,ts}` - Config files with .js or .ts extension

**Bad example patterns:**
- `**`, `**/*.py` - Any pattern starting with '**' will be rejected. Because it would recursively search all directories and subdirectories, which is very likely to yield large result that exceeds your context size. Always use more specific patterns like `src/**/*.py` instead.
- `node_modules/**/*.js` - Although this does not start with '**', it would still highly possible to yield large result because `node_modules` is well-known to contain too many directories and files. Avoid recursively searching in such directories, other examples include `venv`, `.venv`, `__pycache__`, `target`. If you really need to search in a dependency, use more specific patterns like `node_modules/react/src/*` instead.
"""
    )


def test_grep_description(grep_tool: Grep):
    """Test the description of Grep tool."""
    assert grep_tool.base.description == snapshot(
        """\
A powerful search tool based on ripgrep.

**When to use:**
- Find where a specific symbol, string, or pattern appears across the codebase.

**Tips:**
- ALWAYS use the Grep tool instead of running `grep` or `rg` via the Shell tool.
- Use ripgrep pattern syntax, not grep syntax. E.g. escape braces like `\\\\{` to search for `{`.
- Hidden files (dotfiles like `.gitlab-ci.yml`, `.eslintrc.json`) are always searched. To also search files excluded by `.gitignore` (e.g. `node_modules`, build outputs), set `include_ignored` to `true`. Sensitive files (such as `.env`) are still skipped for safety, even when `include_ignored` is `true`.

**Scope the search so results fit your context:**
- Narrow with `path`, a `glob`, or a file `type` rather than scanning the whole repo for a common token.
- For "does this exist / where" questions, start with `output_mode="files_with_matches"` to get just the file list, then read the promising files.
- Use `head_limit` to cap matches. A broad pattern — a bare common word, or searching under `node_modules`/`.venv`/`dist` — can return enormous output that floods your context; narrow it first.

**When to escalate:**
- For open-ended investigation that will clearly need more than ~3 searches across many files, delegate to a read-only `explore` subagent (via `Agent`/`RunAgents`) instead of running many Grep calls yourself, to keep your own context clean.
"""
    )


def test_write_file_description(write_file_tool: WriteFile):
    """Test the description of WriteFile tool."""
    assert write_file_tool.base.description == snapshot(
        """\
Write content to a file, creating it or overwriting/appending to an existing one.

**When to use:**
- Create a genuinely new file, or fully replace a file whose entire contents you are rewriting.

**When NOT to use:**
- To change part of an existing file, prefer StrReplaceFile — it is safer (exact-match) and avoids accidentally dropping content you did not mean to touch. Never blindly recreate a large existing file from memory with WriteFile.
- Do not proactively create documentation (`README`, `*.md`) unless the user asked for it.

**Tips:**
- When `mode` is not specified, it defaults to `overwrite`. Always write with caution.
- When the content to write is too long (e.g. > 100 lines), use this tool multiple times instead of a single call: `overwrite` mode for the first write, then `append` mode for the rest.
"""
    )


def test_str_replace_file_description(str_replace_file_tool: StrReplaceFile):
    """Test the description of StrReplaceFile tool."""
    assert str_replace_file_tool.base.description == snapshot(
        """\
Replace specific strings within a file. Prefer this over WriteFile for editing existing files.

**When to use:**
- Make a targeted edit to part of an existing text file.

**Tips:**
- Only use this tool on text files.
- Multi-line strings are supported; you can specify a single edit or a list of edits in one call.
- Unless `replace_all` is true, the old string must match **exactly once**. If it appears multiple times the edit fails — add surrounding lines until the match is unique. If it appears zero times the edit fails — re-read the file (its content may differ from what you expect) rather than guessing.
- Prefer this tool over the WriteFile tool and over Shell `sed`/`awk`.
"""
    )


def test_search_web_description(search_web_tool: SearchWeb):
    """Test the description of PythinkerAISearch tool."""
    assert search_web_tool.base.description == snapshot(
        """\
Search the internet for current information — news, documentation, release notes, blog posts, papers. Returns ranked results with snippets. Results may be limited to a configured set of allowed domains.

**When to use:**
- You need information newer than your training data, or facts you cannot derive from the repository.
- You are looking for the *latest* version, release, or API of something — anchor the query to the current date rather than a year you assume from training.

**Tips:**
- Prefer specific, keyword-rich queries over questions; include the current year when recency matters (e.g. `fastmcp resources API 2026`, not `how does fastmcp work`).
- WebSearch finds pages; to read one in full, follow up with FetchURL on the most promising result.
- If results are empty or off-topic, broaden or rephrase once — do not loop on near-identical queries.

**When NOT to use:**
- For anything answerable from the working directory — read the code and docs first.
- Note: queries may be restricted to allowed domains, so a blocked search returns fewer or no results rather than an error.
"""
    )


def test_fetch_url_description(fetch_url_tool: FetchURL):
    """Test the description of FetchURL tool."""
    assert fetch_url_tool.base.description == snapshot(
        """\
Fetch a web page from a URL and extract its main text content.

**When to use:**
- Read the full content of a specific, known URL (a doc page, a changelog, an issue, or a result returned by WebSearch).

**Tips:**
- Use WebSearch first when you do not already have the exact URL, then FetchURL the best result.
- Prefer the most specific/canonical URL (a doc page over a site root) so the extracted text stays on topic.

**When NOT to use / failure modes:**
- Requests may be restricted to a configured set of allowed domains; fetching a disallowed host — including via an HTTP redirect — returns an error rather than content. If you hit this, surface the blocked host to the user instead of retrying the same URL.
- Do not guess or construct URLs. Only fetch URLs the user gave you, that appear in local files, or that WebSearch returned.
"""
    )
