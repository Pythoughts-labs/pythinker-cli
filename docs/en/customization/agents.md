# Agents and Subagents

An agent defines the AI's behavior, including system prompts, available tools, and subagents. You can use built-in agents or create custom agents.

## Built-in agents

Pythinker Code provides built-in primary agents. You can select one at startup with the `--agent` flag:

```sh
pythinker --agent ask
pythinker --agent debug
pythinker --agent okabe
```

### `default`

The default agent, suitable for general use. Enabled tools:

`Agent`, `RunAgents`, `ReadSkill`, `AskUserQuestion`, `SetTodoList`, `UpdateGoal`, `Progress`, `Suggest`, `Memory`, `Recall`, `Scratchpad`, `Shell`, `TaskList`, `TaskOutput`, `TaskInput`, `TaskHandoff`, `TaskStop`, `ReadFile`, `ReadMediaFile`, `Glob`, `Grep`, `SmartSearch`, `WriteFile`, `StrReplaceFile`, `SearchWeb`, `FetchURL`, `ListMcpResources`, `ReadMcpResource`, `EnterPlanMode`, `ExitPlanMode`

### `ask`

Read-only primary mode for answering questions, explaining code, and recommending approaches without modifying the workspace.

### `debug`

Primary mode for systematic failure diagnosis. It reproduces or inspects failure evidence first, narrows root-cause hypotheses, then applies a minimal fix only when implementation is clearly requested.

### `okabe`

An experimental agent for testing new prompts and tools. It inherits from `default` but defines its own tool list, which adds `SendDMail` (D-Mail checkpoint rollback).

## Repository markdown agents

Pythinker Code also auto-discovers project-local markdown subagents compatible with
Claude-style agent files. Put `*.md` files in one of these directories at the project root
(the nearest `.git` ancestor of the working directory):

- `.pythinker/agents/`
- `.claude/agents/`
- `.agents/agents/`
- `.codex/agents/`

Each markdown file can define YAML frontmatter followed by the subagent system prompt:

```markdown
---
name: local-reviewer
description: Review this repository's conventions
tools: ["Read", "Grep", "Glob", "Bash"]
---
You are a repository-specific reviewer. Read nearby tests before reporting findings.
```

Recognized frontmatter fields are `name`, `description`, `tools`, `model`, and
`when_to_use`. Common Claude tool names are translated to Pythinker tools, for example
`Read` → `ReadFile`, `Grep` → `Grep`, `Glob` → `Glob`, `Bash` → `Shell`, `Write` →
`WriteFile`, and `Edit` → `StrReplaceFile`. Unknown tool names are skipped with a log
warning. Discovered markdown agents appear as `Agent` tool subagent types alongside the
built-in types.

### Agent definition validation rollout

Pythinker currently resolves YAML and repository markdown definitions into one agent catalogue.
Unknown definition fields are accepted with one aggregated startup warning per source in this
release. Warnings identify the field path but do not include field values, prompt content, or raw
absolute source paths.

The production policy will reject unknown fields in the immediately following minor release.
Fix warnings before upgrading: required YAML definitions will then fail to load, while an invalid
optional markdown definition will be skipped with a diagnostic. The existing `LaborMarket`
compatibility interface and generated markdown YAML wrappers will remain for that strict-default
release. Their earliest removal is one additional minor release later, and only after direct
catalogue launch has equivalent prompt, model, tool-policy, required-MCP, foreground, and
background behavior.

## Custom agent files

Agents are defined in YAML format. Load a custom agent with the `--agent-file` flag:

```sh
pythinker --agent-file /path/to/my-agent.yaml
```

**Basic structure**

```yaml
version: 1
agent:
  name: my-agent
  system_prompt_path: ./system.md
  tools:
    - "pythinker_code.tools.shell:Shell"
    - "pythinker_code.tools.file:ReadFile"
    - "pythinker_code.tools.file:WriteFile"
```

**Inheritance and overrides**

Use `extend` to inherit another agent's configuration and only override what you need to change:

```yaml
version: 1
agent:
  extend: default  # Inherit from default agent
  system_prompt_path: ./my-prompt.md  # Override system prompt
  exclude_tools:  # Exclude certain tools
    - "pythinker_code.tools.web:SearchWeb"
    - "pythinker_code.tools.web:FetchURL"
```

`extend: default` inherits from the built-in default agent. You can also specify a relative path to inherit from another agent file.

**Configuration fields**

| Field | Description | Required |
|-------|-------------|----------|
| `extend` | Agent to inherit from, can be `default` or a relative path | No |
| `name` | Agent name | Yes (optional when inheriting) |
| `system_prompt_path` | System prompt file path, relative to agent file | Yes (optional when inheriting) |
| `system_prompt_args` | Custom arguments passed to system prompt, merged when inheriting | No |
| `model` | Default model alias for this agent or subagent | No |
| `mode` | Agent mode: `primary`, `subagent`, `all`, or `hidden` | No |
| `hidden` | Hide this agent from default selection / background support metadata | No |
| `steps` | Per-agent maximum steps per turn | No |
| `temperature` | Agent model temperature metadata | No |
| `top_p` | Agent model top-p metadata | No |
| `tools` | Tool list, format is `module:ClassName` | Yes (optional when inheriting) |
| `allowed_tools` | Tool allowlist used instead of the inherited full tool list | No |
| `exclude_tools` | Tools to exclude | No |
| `subagents` | Subagent definitions | No |

## System prompt built-in parameters

The system prompt file is a Markdown template that can use `${VAR}` syntax to reference variables and supports the Jinja2 `{% include %}` directive to include other files. Built-in variables include:

| Variable | Description |
|----------|-------------|
| `${PYTHINKER_NOW}` | Current time (ISO format) |
| `${PYTHINKER_WORK_DIR}` | Working directory path |
| `${PYTHINKER_WORK_DIR_LS}` | Working directory file list |
| `${PYTHINKER_AGENTS_MD}` | Merged `AGENTS.md` content from project root to working directory |
| `${PYTHINKER_SKILLS}` | Loaded skills list |
| `${PYTHINKER_ADDITIONAL_DIRS_INFO}` | Information about additional directories added via `--add-dir` or `/add-dir` |

You can also define custom parameters via `system_prompt_args`:

```yaml
agent:
  system_prompt_args:
    MY_VAR: "custom value"
```

Then use `${MY_VAR}` in the prompt.

**System prompt example**

```markdown
# My Agent

You are a helpful assistant. Current time: ${PYTHINKER_NOW}.

Working directory: ${PYTHINKER_WORK_DIR}

${MY_VAR}
```

## Defining subagents in agent files

Subagents can handle specific types of tasks. After defining subagents in an agent file, the main agent can launch them via the `Agent` tool:

```yaml
version: 1
agent:
  extend: default
  subagents:
    coder:
      path: ./coder-sub.yaml
      description: "Handle coding tasks"
    reviewer:
      path: ./reviewer-sub.yaml
      description: "Code review expert"
```

Subagent files are also standard agent format, typically inheriting from the main agent:

```yaml
# coder-sub.yaml
version: 1
agent:
  extend: ./agent.yaml  # Inherit from main agent
  system_prompt_args:
    ROLE_ADDITIONAL: |
      You are now running as a subagent...
```

## Built-in subagent types

The default agent configuration includes focused built-in subagent types with different tool policies and use cases:

| Type | Purpose | Available tools |
|------|---------|----------------|
| `coder` | General software engineering with judgment: read/write files, run commands, search code | Read/search tools, `Shell`, write tools, web tools |
| `implementer` | Scoped implementation with minimal edits and quick verification | Read/search tools, `Shell`, write tools, web tools |
| `explore` | Fast read-only codebase exploration: search, read, summarize | Read/search tools and `Shell`; no write tools |
| `plan` | Implementation planning and architecture design | Read/search tools and web tools; no write tools |
| `planner` | Read-only recon planner that decomposes broad work into parallel seeds | Read/search tools and `Shell`; no write tools |
| `scout` | Read-only external docs, dependency-source, and API freshness researcher | Read/search tools, `Shell`, web tools; no write tools |
| `review` | Read-only severity-scored code review | Read/search tools and `Shell`; no write tools |
| `code-reviewer` | Diff-focused code review for the current branch | Read/search tools and `Shell`; no write tools |
| `security-reviewer` | Diff-focused security review with validated findings | Read/search tools and `Shell`; no write tools |
| `debugger` | Root-cause analysis for failures, logs, and stack traces | Read/search tools and `Shell`; no write tools |
| `verifier` | Read-only validation runner for tests, lint, type checks, and builds | Read/search tools and `Shell`; no write tools |
| `judge` | Independent final quality gate for answers, reports, and code-change summaries | Read/search tools and `Shell`; no write tools |

All subagent types are prohibited from nesting the `Agent` tool (subagents cannot create their own subagents). The `Agent` tool is only available to the root agent.

Reviewer-class types (`review`, `code-reviewer`, `security-reviewer`, `debugger`, `judge`, `verifier`, `explore`) run offline by design: their permission profiles block network and external doc-lookup tools because the content they analyze is untrusted. Third-party claims they cannot verify from the repository come back under RISKS as `needs verification` items; the parent agent resolves those against live docs — directly or by dispatching `scout`, the online research type (`plan` also keeps web access for planning research).

## How subagents run

Subagents launched via the `Agent` tool run in an isolated context and return results to the main agent when complete. Each subagent instance maintains its own context history and metadata under `subagents/<agent_id>/` in the session directory, and can be resumed across multiple invocations. Advantages of this approach:

- Isolated context, avoiding pollution of main agent's conversation history
- Multiple independent tasks can be processed in parallel
- Subagents can have targeted system prompts
- Persistent instances preserve context across multiple calls

## Built-in tools list

The following are all built-in tools in Pythinker Code.

### `Agent`

- **Path**: `pythinker_code.tools.agent:Agent`
- **Description**: Start or resume a subagent instance for a focused task. Multiple built-in subagent types are available — for example `coder`, `implementer`, `explore`, `plan`, `planner`, `scout`, `review`, `code-reviewer`, `security-reviewer`, `debugger`, `verifier`, and `judge`; see the built-in subagent types table above for each one's tool policy. Each instance maintains its own context history and supports foreground or background execution.

| Parameter | Type | Description |
|-----------|------|-------------|
| `description` | string | Short task description (3-5 words) |
| `prompt` | string | Detailed task description |
| `subagent_type` | string | Built-in subagent type, default `coder` |
| `model` | string | Optional model override |
| `resume` | string | Optional agent instance ID to resume an existing instance |
| `run_in_background` | bool | Whether to run in background, default false |
| `timeout` | int | Timeout in seconds, range 30–3600. Foreground defaults to no timeout (runs until completion), background defaults to the configured limit (1 hour); the task is stopped if the limit is exceeded |
| `dependencies` | array | Optional background task IDs this task depends on. Metadata only — launch dependent tasks after their prerequisites are ready |
| `budget_seconds` | int | Optional time budget (seconds) recorded as planning/synthesis metadata |
| `isolation` | string | `none` (default) or `worktree`. `worktree` records a git-worktree isolation intent for background agents; ignored for foreground runs |

### `RunAgents`

- **Path**: `pythinker_code.tools.agent:RunAgents`
- **Description**: Launch a batch of subagents (1–8) in one call, sharing a common `base_prompt` and each running its own `prompt`. Foreground batches run the children concurrently and return all results inline; background batches return task IDs immediately, and if a background batch exceeds the available slots only the fitting prefix is launched while the rest are reported as deferred. Like `Agent`, this tool is only available to the root agent.

| Parameter | Type | Description |
|-----------|------|-------------|
| `summary` | string | Short summary of the multi-agent run |
| `base_prompt` | string | Shared context prepended to every child prompt |
| `agents` | array | Child agents to launch (1–8); each has its own objective |
| `agents[].name` | string | Stable short name for the child agent |
| `agents[].prompt` | string | Child-specific task prompt |
| `agents[].title` | string | Optional 3–5 word display title, defaults to `name` |
| `agents[].subagent_type` | string | Built-in subagent type for the child, default `coder` |
| `model` | string | Optional model override applied to every child |
| `run_in_background` | bool | Whether to run in background, default true |
| `timeout` | int | Optional per-agent timeout in seconds, range 30–3600 |
| `isolation` | string | `none` (default) or `worktree` for background children |

To keep parallel children distinguishable in the task list, tree, and notifications, any child whose name is generic (or collides with a sibling) is given a stable `adjective-noun` codename (for example `amber-falcon`), while its subagent type stays visible separately.

### `AskUserQuestion`

- **Path**: `pythinker_code.tools.ask_user:AskUserQuestion`
- **Description**: Present structured questions and options to the user during execution, collecting preferences or decisions. Suitable for scenarios where the user needs to choose between approaches, resolve ambiguous instructions, or provide requirements. Should not be overused — only call when the user's choice genuinely affects subsequent actions.

| Parameter | Type | Description |
|-----------|------|-------------|
| `questions` | array | Questions list (1–4 questions) |
| `questions[].question` | string | Question text, ending with `?` |
| `questions[].header` | string | Short label, max 12 characters (e.g., `Auth`, `Style`) |
| `questions[].options` | array | Available options (2–4), the system adds an "Other" option automatically |
| `questions[].options[].label` | string | Option label (1–5 words), append `(Recommended)` for recommended options |
| `questions[].options[].description` | string | Option description |
| `questions[].multi_select` | bool | Allow multiple selections, default false |

### `SetTodoList`

- **Path**: `pythinker_code.tools.todo:SetTodoList`
- **Description**: Manage todo list, track task progress. Supports three usage modes: update mode (pass `todos` array to replace the entire list), query mode (omit `todos` to return the current list), and clear mode (pass an empty array `[]` to clear the list). Todo items are persisted to session state.

| Parameter | Type | Description |
|-----------|------|-------------|
| `todos` | array \| null | Todo list items. Omit to query the current list; pass `[]` to clear |
| `todos[].title` | string | Todo item title |
| `todos[].status` | string | Status: `pending`, `in_progress`, `done` |

### `Shell`

- **Path**: `pythinker_code.tools.shell:Shell`
- **Description**: Execute shell commands. Requires user approval. Uses the appropriate shell for the OS (bash/zsh on Unix, PowerShell on Windows).

| Parameter | Type | Description |
|-----------|------|-------------|
| `command` | string | Command to execute |
| `timeout` | int | Timeout in seconds, default 60, max 300 for foreground / 86400 for background |
| `run_in_background` | bool | Whether to run as a background task, default false |
| `description` | string | Short description for the background task, required when `run_in_background=true` |

When `run_in_background=true`, the command is launched as a background task and the tool immediately returns a task ID, allowing the AI to continue working. The system automatically sends a notification when the task completes. Ideal for long-running builds, tests, watchers, and servers.

### `ReadFile`

- **Path**: `pythinker_code.tools.file:ReadFile`
- **Description**: Read text file content. Max 1000 lines per read, max 2000 characters per line. Files outside working directory require absolute paths. Every read returns the total number of lines in the file. Sensitive files (such as `.env`, SSH private keys, and cloud credentials) are rejected.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | File path |
| `line_offset` | int | Starting line number, default 1. Supports negative values to read from the end of the file (e.g. `-100` reads the last 100 lines); absolute value cannot exceed 1000 |
| `n_lines` | int | Number of lines to read, default/max 1000 |

### `ReadMediaFile`

- **Path**: `pythinker_code.tools.file:ReadMediaFile`
- **Description**: Read image or video files. Max file size 100MB. Only available when the model supports image/video input. Files outside working directory require absolute paths.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | File path |

### `Glob`

- **Path**: `pythinker_code.tools.file:Glob`
- **Description**: Match files and directories by pattern. Returns max 1000 matches, patterns starting with `**` not allowed. Can also search within discovered skill roots, and `~` in paths is expanded to the user's home directory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string | Glob pattern (e.g., `*.py`, `src/**/*.ts`) |
| `directory` | string | Search directory, defaults to working directory |
| `include_dirs` | bool | Include directories, default true |

### `Grep`

- **Path**: `pythinker_code.tools.file:Grep`
- **Description**: Search file content with regular expressions, based on ripgrep. Hidden files (dotfiles) are searched by default, but files excluded by `.gitignore` are not. Sensitive files (such as `.env`, SSH private keys, and cloud credentials) are always filtered out, even when `include_ignored` is set.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string | Regular expression pattern |
| `path` | string | Search path, defaults to current directory |
| `glob` | string | File filter (e.g., `*.js`) |
| `type` | string | File type (e.g., `py`, `js`, `go`) |
| `output_mode` | string | Output mode: `files_with_matches` (default), `content`, `count_matches` |
| `-B` | int | Show N lines before match |
| `-A` | int | Show N lines after match |
| `-C` | int | Show N lines before and after match |
| `-n` | bool | Show line numbers, default true |
| `-i` | bool | Case insensitive |
| `multiline` | bool | Enable multiline matching |
| `head_limit` | int | Limit output lines, default 250 |
| `offset` | int | Skip first N results for pagination, default 0 |
| `include_ignored` | bool | Search files excluded by `.gitignore` (e.g. `node_modules`, build artifacts), default false |

### `WriteFile`

- **Path**: `pythinker_code.tools.file:WriteFile`
- **Description**: Write files. Requires user approval. Absolute paths are required when writing files outside the working directory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Absolute path |
| `content` | string | File content |
| `mode` | string | `overwrite` (default) or `append` |

### `StrReplaceFile`

- **Path**: `pythinker_code.tools.file:StrReplaceFile`
- **Description**: Edit files using string replacement. Requires user approval. Absolute paths are required when editing files outside the working directory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Absolute path |
| `edit` | object/array | Single edit or list of edits |
| `edit.old` | string | Original string to replace |
| `edit.new` | string | Replacement string |
| `edit.replace_all` | bool | Replace all matches, default false |

### `SearchWeb`

- **Path**: `pythinker_code.tools.web:SearchWeb`
- **Description**: Search the web. Requires search service configuration (auto-configured on Pythinker platform).

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Search keywords |
| `limit` | int | Number of results, default 5, max 20 |
| `include_content` | bool | Include page content, default false |

### `FetchURL`

- **Path**: `pythinker_code.tools.web:FetchURL`
- **Description**: Fetch webpage content, returns extracted main text. Uses fetch service if configured, otherwise uses local HTTP request.

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | string | URL to fetch |

### `Think`

- **Path**: `pythinker_code.tools.think:Think`
- **Description**: Let the agent record thinking process, suitable for complex reasoning scenarios

| Parameter | Type | Description |
|-----------|------|-------------|
| `thought` | string | Thinking content |

### `SendDMail`

- **Path**: `pythinker_code.tools.dmail:SendDMail`
- **Description**: Send delayed message (D-Mail), for checkpoint rollback scenarios

| Parameter | Type | Description |
|-----------|------|-------------|
| `message` | string | Message to send |
| `checkpoint_id` | int | Checkpoint ID to send back to (>= 0) |

### `EnterPlanMode`

- **Path**: `pythinker_code.tools.plan.enter:EnterPlanMode`
- **Description**: Request to enter plan mode. After calling, an approval request is presented to the user unless the session is in YOLO or auto mode; YOLO auto-approves entering plan mode, but `ExitPlanMode` still presents the final plan for user approval. Use this only when the user explicitly requests planning or when there is significant architectural ambiguity. See [Plan mode](../guides/interaction.md#plan-mode).

This tool takes no parameters.

### `ExitPlanMode`

- **Path**: `pythinker_code.tools.plan:ExitPlanMode`
- **Description**: Submit a plan for user approval while in plan mode. Before calling, the plan must be written to the plan file. This tool reads the plan file content and presents it to the user for approval. The user can select an implementation path (exit plan mode and start execution), reject (stay in plan mode and wait for feedback), or provide revision comments. See [Plan mode](../guides/interaction.md#plan-mode).

| Parameter | Type | Description |
|-----------|------|-------------|
| `options` | list \| null | When the plan contains multiple alternative implementation paths, list 2–3 options for the user to choose from. Each option has a `label` (1–8 word short name, may append "(Recommended)") and an optional `description` (brief summary). The labels "Approve", "Reject", and "Revise" are reserved and cannot be used. |

### `TaskList`

- **Path**: `pythinker_code.tools.background:TaskList`
- **Description**: List background tasks in the current session. Useful for re-enumerating task IDs after context compaction or checking which tasks are still running.

| Parameter | Type | Description |
|-----------|------|-------------|
| `active_only` | bool | List only active tasks, default true |
| `limit` | int | Maximum number of tasks to return (1–100), default 20 |

### `TaskOutput`

- **Path**: `pythinker_code.tools.background:TaskOutput`
- **Description**: Retrieve output and status of a background task. Returns a non-blocking status/output snapshot by default; use `ReadFile` with the returned `output_path` to read the full log if output is truncated.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Task ID to query |
| `block` | bool | Whether to wait for task completion, default false |
| `timeout` | int | Maximum wait time in seconds when `block=true` (0–3600), default 30 |

### `TaskStop`

- **Path**: `pythinker_code.tools.background:TaskStop`
- **Description**: Stop a running background task. Requires user approval. Use only when a task must be cancelled; for normal completion, wait for the automatic notification. Not available in plan mode.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Task ID to stop |
| `reason` | string | Reason for stopping (optional), default "Stopped by TaskStop" |


## Tool security boundaries

**Workspace scope**

- File reading and writing are typically done within the working directory (and additional directories added via `--add-dir` or `/add-dir`)
- Absolute paths are required when reading files outside the workspace
- Write and edit operations require user approval; absolute paths are required when operating on files outside the workspace

**Approval mechanism**

The following operations require user approval:

| Operation | Approval required |
|-----------|-------------------|
| Shell command execution | Each execution |
| File write/edit | Each operation |
| MCP tool calls | Each call |
| Stop background task | Each stop |
