# CLAUDE.md

This repository's agent guidance lives in `AGENTS.md` (the portable, tracked standard injected
into Pythinker sessions via `PYTHINKER_AGENTS_MD`). Claude Code does not read `AGENTS.md`
automatically, so this file imports it — plus the machine-local overlay — to keep a single source
of truth.

Read both, in order:

1. **`AGENTS.md`** — non-negotiable repository rules. Always applies.
2. **`AGENTS.local`** — machine-specific / private local instructions (gitignored). Read it after
   `AGENTS.md`. It may add workflow detail (e.g. a local code-graph workflow) but must not
   weaken or override the rules in `AGENTS.md`.

@AGENTS.md

@AGENTS.local
