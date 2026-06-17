# Pythinker Security Scan Python-native migration

This document records the production migration of the source TypeScript scanner at
`upstream review package` into Pythinker's Python architecture and user-facing Pythinker Security
Scan branding.

## Source audit

The source scanner was a TypeScript/pnpm monorepo with these runtime packages:

- `packages/core`: schemas, data paths, project/run/file record persistence, plugin contracts.
- `packages/scanner`: tech detection, matcher registry, regex scanner, language stats.
- `packages/processor`: prompt assembly, agent adapters, batching, processing, triage,
  revalidation, enrichment.
- `packages/deepsec`: Commander CLI, formatters, preflight, file-source resolution, sandbox and
  data commit helpers.
- `packages/scanner/src/matchers`: 198 matcher files.
- `packages/processor/src/prompt`: core security prompt, tech highlights, slug notes.
- `docs/`: architecture, configuration, data layout, plugin authoring, model guidance.

## Target mapping

| Pythinker Security Scan source | Pythinker target | Decision |
| --- | --- | --- |
| `packages/core/src/types.ts`, `schemas.ts` | `src/pythinker_review/security_scan/models.py` | Rewritten as Pydantic models. |
| `packages/core/src/paths.ts` | `src/pythinker_review/security_scan/paths.py` | Rewritten with Python path validation. |
| `packages/core/src/run.ts` | `src/pythinker_review/security_scan/store.py` | Rewritten as pure-Python JSON store. |
| `packages/scanner/src/detect-tech.ts` | `src/pythinker_review/security_scan/tech.py` | Ported and expanded from existing Pythinker detector. |
| `packages/scanner/src/matcher-registry.ts` | `src/pythinker_review/security_scan/matchers.py` | Rewritten registry/dataclasses. |
| `packages/scanner/src/matchers/*.ts` | `src/pythinker_review/security_scan/matchers_data.py`, `matchers.py` | Metadata generated for all 198; curated Python patterns added for high-value custom matchers. |
| `packages/scanner/src/index.ts` | `src/pythinker_review/security_scan/scanner.py` | Rewritten scanner/orchestration. |
| `packages/processor/src/batch.ts` | `src/pythinker_review/security_scan/processor.py` | Ported batching. |
| `packages/processor/src/prompt/*` | `src/pythinker_review/security_scan/prompt.py`, `prompts/system.md` | Rewritten prompt with clearer policy and Python assembly. |
| `packages/processor/src/index.ts`, `triage.ts` | `src/pythinker_review/security_scan/processor.py` | Rewritten processing, triage, revalidation through `ReviewLLM`. |
| `packages/deepsec/src/commands/*` | `src/pythinker_review/cli/security_scan.py` | Rebuilt Typer CLI. |
| `packages/deepsec/src/formatters.ts` | `src/pythinker_review/security_scan/reporting.py` | Rewritten report/export/status/metrics helpers. |
| sandbox/Vercel code | Not ported | Dropped from core runtime; Pythinker can run jobs locally/CI. Remote execution can be added later through a Python executor. |
| JS/TS config/package files | Not ported | Not needed after Python-native migration. |

## Pythonization decisions

- The authoritative runtime is Python under `pythinker_review.security_scan`.
- No Node, pnpm, Commander, Vercel Sandbox, or SDK-specific agent wrappers are required.
- Model calls use the existing `ReviewLLM` protocol, so `pythinker security-scan` uses the active
  Pythinker model and standalone `pythinker-security-scan` can use the fake test LLM.
- Data remains Pythinker Security Scan-compatible JSON (`project.json`, `files/**/*.json`, `runs/*.json`,
  `tech.json`, `reports/`) but defaults to `.pythinker-review/security-scan/data` instead of `.security-scan/data`.
- Matchers are pure-Python regex/dataclass specs. All 198 source matcher slugs are represented;
  regexMatcher-based source patterns were mechanically translated and high-value custom matchers
  were rewritten by hand as curated Python patterns.

## Non-ported items

- Vercel Sandbox orchestration and request proxy.
- Data git commit/push automation.
- JavaScript plugin loading from the source TypeScript config format.
- External ownership/people/notifier providers.

These were intentionally excluded because they depend on non-Python runtime assumptions or remote
infrastructure. Python plugin/executor interfaces can be added later if needed.
