# Breaking changes and migration

This page documents breaking changes in Pythinker Code releases and provides migration guidance.

## Unreleased

## 0.59.0 (2026-07-17)

No breaking changes. This release is compatible with 0.58.0 user configuration, native installs, and session data.

## 0.58.0 (2026-07-11)

No breaking changes. This release is compatible with 0.57.0 user configuration, native installs, and session data.

## 0.57.0 (2026-07-05)

No breaking changes. This release is compatible with 0.56.0 user configuration, native installs, and session data.

## 0.56.0 (2026-07-02)

No breaking changes. This release is compatible with 0.55.0 user configuration, native installs, and session data.

## 0.55.0 (2026-06-30)

No breaking changes. This release is compatible with 0.54.0 user configuration, native installs, and session data.

## 0.54.0 (2026-06-30)

No breaking changes. This release is compatible with 0.53.0 user configuration, native installs, and session data.

## 0.53.0 (2026-06-23)

No breaking changes. This release is compatible with 0.52.0 user configuration, native installs, and session data.

## 0.52.0 (2026-06-23)

## 0.51.0 (2026-06-22)

No breaking changes. This release is compatible with 0.50.0 user configuration, native installs, and session data.

## 0.50.0 (2026-06-20)

No breaking changes. This release is compatible with 0.49.0 user configuration, native installs, and session data.

## 0.49.0 (2026-06-20)

No breaking changes. This release is compatible with 0.48.0 user configuration, native installs, and session data.

## 0.48.0 (2026-06-17)

No breaking changes. This release is compatible with 0.47.0 user configuration, native installs, and session data.

## 0.47.0 (2026-06-16)

No breaking changes. This release is compatible with 0.46.0 user configuration, native installs, and session data.

## 0.46.0 (2026-06-14)

No breaking changes. This release is compatible with 0.45.0 user configuration, native installs, and session data.

## 0.45.0 (2026-06-14)

No breaking changes. This release is compatible with 0.44.0 user configuration, native installs, and session data.

## 0.44.0 (2026-06-13)

No configuration or session-data breaking changes. The Windows in-app updater fix and the new auto-update toggle are backward compatible — existing config keys, the `pythinker update` command, and persisted session data are unaffected.

## 0.43.0 (2026-06-13)

No configuration or session-data breaking changes. One behavior change affects update handling:

- **Startup auto-updates are now on by default.** Managed and native installs check for and apply updates in the background at startup, then show a restart-to-apply notice — replacing the previous blocking prompt. To keep updates manual, set `auto_update = false` in `~/.pythinker/config.toml` or export `PYTHINKER_AUTO_UPDATE=0`. The in-app `pythinker update` command is unchanged.

## 0.42.0 (2026-06-12)

No configuration or session-data breaking changes. Two behavior changes may affect scripted or automated use:

- **Text-mode error diagnostics now go to stderr.** Previously they were interleaved on stdout. If you scrape `pythinker` stdout in text mode for error text, capture `2>&1` or switch to `--output-format stream-json`.
- **Project- and local-scope hooks now require trusting the project.** A cloned repo's `.pythinker/config.toml` hooks no longer auto-run at session start — run `/trust` once per project to record a durable decision (stored user-side in `trusted_projects.json`). Until then, those hooks are stripped with a warning naming the fix.

## 0.41.0 (2026-06-11)

No breaking changes. This release is compatible with 0.40.1 user configuration, native installs, and session data.

## 0.40.1 (2026-06-10)

No breaking changes. This release is compatible with 0.40.0 user configuration, native installs, and session data.

## 0.40.0 (2026-06-10)

**`pythinker web` / `pythinker vis` `-h` host flag renamed to `-H`.** The short flag `-h` is now a help alias on both subcommands (matching the root CLI); previously `-h <ip>` bound the host. Scripts using `-h 0.0.0.0` must switch to `-H <ip>` or `--host <ip>`.

## 0.39.0 (2026-06-09)

No breaking changes. This release is compatible with 0.38.0 user configuration, native installs, and session data.

## 0.38.0 (2026-06-08)

No breaking changes. This release is compatible with 0.37.0 user configuration, native installs, and session data.

## 0.37.0 (2026-06-07)

No breaking changes. This release is compatible with 0.36.0 user configuration, native installs, and session data.

## 0.36.0 (2026-06-05)

No breaking changes. This release is compatible with 0.35.0 user configuration, native installs, and session data.

## 0.35.0 (2026-06-04)

## 0.34.0 (2026-06-03)

No breaking changes. This release is compatible with 0.33.0 user configuration, native installs, and session data.

## 0.33.0 (2026-06-03)

No breaking changes. This release is compatible with 0.32.0 user configuration, native installs, and session data.

## 0.32.0 (2026-06-03)

No breaking changes. This release is compatible with 0.31.0 user configuration, native installs, and session data.

## 0.31.0 (2026-06-02)

No breaking changes. This release is compatible with 0.30.0 user configuration, native installs, and session data.

## 0.30.0 (2026-06-02)

No breaking changes. This release is compatible with 0.29.0 user configuration, native installs, and session data.

## 0.29.0 (2026-06-01)

No breaking changes. This release is compatible with 0.28.0 user configuration, native installs, and session data.

## 0.28.0 (2026-05-31)

**`.pythinker/AGENTS.md` is no longer loaded as project instructions.** Pythinker now merges only `AGENTS.md`/`agents.md` files from the project root down to the working directory. If you kept instructions solely in `.pythinker/AGENTS.md`, move them to a root or directory-level `AGENTS.md` or they will no longer apply. No action is needed if you did not use `.pythinker/AGENTS.md`.

Otherwise this release is compatible with 0.27.0 user configuration, native installs, and session data. The repository's move to the Pythoughts-labs GitHub org is handled transparently — the default `/feedback` repository auto-migrates and download/update URLs resolve to the new org.

## 0.27.0 (2026-05-31)

No breaking changes. This release is compatible with 0.26.0 user configuration, native installs, and session data.

## 0.26.0 (2026-05-30)

No breaking changes. This release is compatible with 0.25.0 user configuration, native installs, and session data.

## 0.25.0 (2026-05-29)

No breaking changes. This release is compatible with 0.24.0 user configuration, native installs, and session data.

## 0.24.0 (2026-05-28)

No breaking changes. This release is compatible with 0.23.0 user configuration, native installs, and session data.

## 0.23.0 (2026-05-28)

No breaking changes. This release is compatible with 0.22.0 user configuration, native installs, and session data.

## 0.22.0 (2026-05-28)

No breaking changes. This release is compatible with 0.21.0 user configuration, native installs, and session data.

## 0.21.0 (2026-05-28)

No breaking changes. This release is compatible with 0.20.0 user configuration, native installs, and session data.

## 0.20.0 (2026-05-28)

No breaking changes. This release is compatible with 0.19.0 user configuration, native installs, and session data.

## 0.19.0 (2026-05-27)

No breaking changes. This release is compatible with 0.18.0 user configuration, native installs, and session data.

## 0.18.0 (2026-05-27)

No breaking changes. This release is compatible with 0.17.0 user configuration, native installs, and session data.

## 0.17.0 (2026-05-25)

No breaking changes. This release is compatible with 0.16.0 user configuration, native installs, and session data.

## 1.0.0 (2026-05-06)

Initial release — no migration needed.
