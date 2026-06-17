# Code-reviewr to Pythinker migration

This document records the production migration decision for `upstream review package` ("code-reviewr") into Pythinker Review.

## 1. Repository audit

### Source tree summary

The source repository contains 252 non-`.git` files:

| Area | Count | Role |
| --- | ---: | --- |
| `code_review/` | 100 | Runtime package: CLI, agent dispatcher, PR tools, providers, prompts, config, servers. |
| `tests/` | 57 | Unit, e2e, and health tests for providers, prompt parsing, patch handling, Codex OAuth, and tools. |
| `docs/` | 54 | MkDocs documentation, installation pages, tool guides, and superpower specs. |
| `.github/` | 14 | CI, CodeQL, release, docs, coverage, pre-commit, and GitHub Action workflows/templates. |
| Root config/assets | 27 | `pyproject.toml`, `requirements*.txt`, `setup.py`, Dockerfiles, `action.yaml`, `.code_review.toml`, security/community docs. |

Key directories and files:

- `code_review/agent/code_review.py` — command router for `/review`, `/describe`, `/improve`, `/ask`, `/add_docs`, `/update_changelog`, `/generate_labels`, `/similar_issue`, and config/help commands.
- `code_review/tools/*.py` — tool implementations for PR review, description generation, code suggestions, questions, line questions, docs updates, changelog drafts, labels, help docs, and ticket compliance.
- `code_review/settings/*.toml` and `code_review/settings/code_suggestions/*.toml` — prompt/config single sources of truth.
- `code_review/algo/*.py` — token budgeting, patch processing, language detection, file filters, CLI arg validation, utility formatting, and AI handler support.
- `code_review/git_providers/*.py` — provider integrations for GitHub, GitLab, Bitbucket, Azure DevOps, CodeCommit, Gerrit, Gitea, and local git.
- `code_review/servers/*.py` — webhook/app/action runners for hosted provider deployments.
- `identity_providers/` and `secret_providers/` — auth/secret abstraction layers.
- `docs/docs/tools/*.md` and `docs/docs/usage-guide/*.md` — user-facing behavior contracts for tools.
- `.code_review.toml` — repo-local overrides enabling agentic review, auto approval, and GitHub app commands.

### Key modules

- Agent routing: `code_review/agent/code_review.py`.
- PR review: `code_review/tools/pr_reviewer.py` + `settings/pr_reviewer_prompts.toml`.
- PR description: `code_review/tools/pr_description.py` + `settings/pr_description_prompts.toml`.
- Code suggestions: `code_review/tools/pr_code_suggestions.py` + `settings/code_suggestions/*.toml`.
- Questions: `code_review/tools/pr_questions.py`, `pr_line_questions.py`, and their prompt TOML files.
- Labels/changelog/docs/help: `pr_generate_labels.py`, `pr_update_changelog.py`, `pr_add_docs.py`, `pr_help_docs.py`, prompt TOMLs.
- Provider publishing and comments: `git_providers/`, `servers/`, GitHub Action Docker entrypoints.
- AI/model handling: `algo/ai_handlers/*`, `algo/token_handler.py`, `config_loader.py`.

### Prompt-related files

Migrated or adapted:

- `settings/pr_reviewer_prompts.toml`
- `settings/pr_description_prompts.toml`
- `settings/code_suggestions/pr_code_suggestions_prompts.toml`
- `settings/pr_questions_prompts.toml`
- `settings/pr_custom_labels.toml`
- `settings/pr_update_changelog_prompts.toml`
- `settings/pr_add_docs.toml`
- `pr_compliance_checklist.yaml`

Deferred as provider-specific or lower priority:

- `pr_information_from_user_prompts.toml`, `pr_evaluate_prompt_response.toml`, hosted publishing prompts, and full self-reflection dual-publishing prompts.

### Config/runtime files

- Runtime defaults in `settings/configuration.toml` were merged conceptually into Pythinker CLI options and defaults rather than copied wholesale.
- `.code_review.toml` was not copied as runtime config; Pythinker uses explicit CLI flags and active model configuration.
- `requirements.txt`, `setup.py`, Dockerfiles, and GitHub Action metadata were not copied because Pythinker Review is a `uv` workspace package.
- Provider/webhook/runtime files were treated as source references, not direct dependencies.

### Risks and gaps

- Direct provider publishing/commenting is intentionally deferred; code-reviewr-derived artifact commands stay read-only. Reviewflow-derived fix/open-pr commands are separate, explicit stateful remediation commands keyed by saved findings.
- The original Dynaconf configuration surface is very large; only high-value review/artifact behavior was ported.
- Hosted webhook deployments are not migrated.
- `similar_issue` is migrated as dependency-free local lexical search, with optional in-memory ChromaDB vector search using deterministic in-process hash embeddings when ChromaDB is installed separately; `--persist-index` explicitly opts into local index writes. Provider issue indexing and hosted/vector services remain deferred.
- Line-question and interactive ticket workflows are locally represented by `ask-line`, strict JSON artifacts, and future provider adapter space.

## 2. Migration plan

### Source → destination mapping

| Source | Destination | Decision |
| --- | --- | --- |
| `code_review/agent/code_review.py` | `src/pythinker_code/agents/default/code_reviewer.yaml`, `packages/pythinker-review/src/pythinker_review/cli/review.py` | Rewritten as Pythinker subagent + Typer commands. |
| `tools/pr_reviewer.py` | `reviewers/prompts/code_review.system.md`, `reviewers/code_review.py`, `engine/runner.py` | Already ported; prompt upgraded. |
| `tools/pr_description.py` | `reviewers/prompts/pr_description.system.md`, `reviewers/artifacts.py`, `reviewers/pr_artifacts.py`, `cli/review.py describe` | Rewritten read-only. |
| `tools/pr_code_suggestions.py` | `reviewers/prompts/code_suggestions.system.md`, `reviewers/artifacts.py`, `cli/review.py suggest/improve` | Rewritten read-only. |
| `tools/pr_questions.py` | `reviewers/prompts/pr_questions.system.md`, `cli/review.py ask` | Rewritten read-only. |
| `tools/pr_line_questions.py` | `reviewers/prompts/line_questions.system.md`, `cli/review.py ask-line` | Rewritten read-only over explicit local diff line ranges. |
| `tools/pr_help_docs.py` | `reviewers/help_docs.py`, `reviewers/prompts/help_docs.system.md`, `cli/review.py help-docs` | Rewritten read-only for local docs; remote clone/comment publishing deferred. |
| `tools/pr_generate_labels.py` + `settings/pr_custom_labels.toml` | `reviewers/prompts/labels.system.md`, `cli/review.py labels` | Rewritten with stable labels and optional local custom-label files. |
| `tools/pr_update_changelog.py` | `reviewers/prompts/changelog.system.md`, `cli/review.py changelog` | Rewritten as draft generation with optional current changelog and PR-link context. |
| `tools/pr_add_docs.py` | `reviewers/prompts/docs.system.md`, `cli/review.py docs` | Rewritten as docs planning with local docs-style/file/symbol targeting options. |
| `tools/pr_similar_issue.py` | `reviewers/similar_issues.py`, `cli/review.py similar-issues` | Rewritten as local lexical search over issue documents, with optional in-memory ChromaDB support when installed separately and explicit `--persist-index` for local index writes; provider issue indexing deferred. |
| `tools/ticket_pr_compliance_check.py`, `pr_compliance_checklist.yaml` | `reviewers/default_compliance.yaml`, `reviewers/compliance.py`, `reviewers/prompts/compliance.system.md`, `cli/review.py compliance` | Rewritten read-only; provider issue fetching deferred. |
| `algo/git_patch_processing.py`, `pr_processing.py` | `engine/diff_source.py`, `engine/structured_diff.py`, `engine/chunker.py`, `engine/artifact_context.py` | Merged into stdlib git + structured diff renderer. |
| `algo/token_handler.py` | `engine/token_budget.py`, `chunker.py`, `artifact_context.py` | Replaced by deterministic character budgeting. |
| `algo/ai_handlers/*` | `llm/protocol.py`, `src/pythinker_code/cli/review.py` | Replaced by active Pythinker model adapter + fake test LLM. |
| `git_providers/*`, `servers/*`, Docker/GitHub Action files | Future provider phase | Dropped from runtime; documented as deferred. |
| `settings/configuration.toml` | CLI options + docs | Merged selectively. |
| `tests/unittest/*` | `packages/pythinker-review/tests/*` | Rewritten focused tests for Pythinker contracts. |
| `docs/docs/tools/*` | `README.md`, this migration doc | Merged/adapted. |

### Files to keep

- Prompt concepts and structured diff conventions.
- Review/describe/improve/ask/ask-line/labels/changelog/docs/compliance/help/help-docs/similar-issues tool semantics.
- Ignore/generated-file intent, token-bounded prompts, strict output parsing, and fail-closed behavior.

### Files to merge

- Patch processing into `engine/diff_source.py`, `structured_diff.py`, `chunker.py`, `artifact_context.py`.
- AI handlers into the Pythinker active-model bridge and `ReviewLLM` protocol.
- Config defaults into explicit CLI options and docs.

### Files to rewrite

- Agent orchestration, prompt templates, schema definitions, output rendering, and tests.

### Files to remove/drop from runtime

- Hosted servers, provider comment publishers, Docker/GitHub Action wrappers, Dynaconf loaders, secret providers, identity providers, and vector-backed similar-issue services.

## 3. Pythinker implementation

Final target structure added or changed:

```text
packages/pythinker-review/
├── docs/code-reviewr-migration.md
├── src/pythinker_review/
│   ├── engine/artifact_context.py
│   ├── output/artifacts.py
│   └── reviewers/
│       ├── artifacts.py
│       ├── help_docs.py
│       ├── pr_artifacts.py
│       ├── similar_issues.py
│       └── prompts/
│           ├── changelog.system.md
│           ├── code_review.system.md
│           ├── code_suggestions.system.md
│           ├── compliance.system.md
│           ├── docs.system.md
│           ├── help_docs.system.md
│           ├── labels.system.md
│           ├── line_questions.system.md
│           ├── pr_description.system.md
│           └── pr_questions.system.md
└── tests/
    ├── e2e/test_cli_artifacts.py
    └── unit/test_artifacts.py

src/pythinker_code/agents/default/code_reviewer.yaml
```

Important improvements:

- All new artifact workflows use strict Pydantic JSON instead of YAML repair heuristics.
- The source compliance checklist is bundled as `reviewers/default_compliance.yaml`, and `pythinker-review compliance` accepts optional ticket text/file context.
- Custom labels, extra instructions, local best practices, score filtering, changelog PR-link context, docs targeting, line-specific Q&A, help-docs, static help/config, and similar-issue search are local/read-only.
- Pythinker owns model/provider selection; no duplicated LiteLLM/Dynaconf stack.
- Provider-publishing side effects are removed from the review engine.
- `improve` is retained as a source-compatible alias for `suggest`.
- Prompt files are smaller, role-specific, deterministic, and fail-closed.
- Subagent instructions now specify exact commands, read-only behavior, and final reporting contracts.

## 4. New system prompt

The production code-review system prompt is `reviewers/prompts/code_review.system.md`. It defines role/scope, priorities, non-findings, validation workflow, strict JSON schema, and no-finding behavior.

## 5. Validation plan

Checks to run:

```bash
make check-pythinker-review
make test-pythinker-review
```

Focused checks:

```bash
uv run --directory packages/pythinker-review pytest tests/unit/test_artifacts.py tests/e2e/test_cli_artifacts.py
uv run --directory packages/pythinker-review ruff format --check src tests
uv run --directory packages/pythinker-review ruff check src tests
```

## 6. Remaining follow-up

- Provider adapters for publishing descriptions, labels, comments, and inline suggestions.
- Provider-side ticket extraction for issue links and acceptance criteria.
- Provider inline-comment thread retrieval/publishing for line-specific Q&A.
- Hosted/vector-service similar-issue retrieval beyond local lexical and optional local ChromaDB backends.
- Provider-backed revalidation/comment publishing parity beyond the local Reviewflow stateful workflow.
