# PR #215 auth review fixes design

## Goal

Resolve every validated review defect on PR #215 without broad auth or configuration refactoring. Preserve existing provider behavior except where it is unsafe, misleading, or violates the repository's failure-truthfulness and async-runtime contracts.

## Scope

1. Prevent nested OAuth references such as `oauth/snowflake-cortex/<account>` from sharing credential or lock files with flat references such as `oauth/xai`.
2. Make the new login/logout persistence unit:
   - serialized across processes at the shared config-file boundary;
   - executed outside the asyncio event loop;
   - safe against partial config-file writes;
   - explicit when rollback or credential deletion fails;
   - unable to report logout success while credentials remain undeleted.
3. Validate the implicit-flow `state` before accepting either success or error payloads.
4. Keep DigitalOcean's intentional degraded credential persistence, but never claim that an unrelated or empty default model is a configured DigitalOcean model.
5. Log curated-model fallback both when catalog status is non-authoritative and when an authoritative catalog yields no usable provider models.
6. Preserve the existing OpenCode Go unavailable-catalog regression test and resolve its stale review thread with evidence.

Out of scope: provider protocol redesigns, replacing DigitalOcean's provider-required implicit grant, Snowflake Cortex chat transforms, global config merge semantics unrelated to these auth transactions, and new dependencies.

## Design

### Credential filenames

Keep the legacy filename for ordinary one-segment OAuth keys so existing credentials remain readable. Encode the complete relative key for multi-segment keys into a deterministic, filesystem-safe filename, and apply the same mapping to lock files. This makes the mapping injective without changing released flat-key paths.

### Persistence transaction

Expose async persistence helpers to provider login/logout callers. Each helper offloads one complete synchronous transaction to a worker thread. The transaction acquires a bounded, fail-closed inter-process lock derived from the default config path before it snapshots credentials/config, mutates state, writes, or rolls back.

Config writes use a temporary file in the target directory, flush and fsync it, apply private permissions, and atomically replace the destination. A failed write therefore leaves the previous config file intact.

Login order remains token write then config write. On failure, restore the previous token (or remove the new one), restore the in-memory snapshot, and re-raise. If rollback also fails, raise an explicit persistence error carrying both failure contexts and log the rollback failure without secrets.

Logout writes the config removal first, then deletes credentials. If deletion fails, restore and persist the previous config before raising. It must never emit success after failed credential deletion.

The config-scoped lock covers snapshot through rollback, so a failed concurrent login cannot restore a token observed before another successful transaction.

### OAuth callback validation

For implicit callbacks, parse `state` and compare it with the expected value before interpreting `error`. RFC 6749 requires the original state on both successful and error responses. A wrong-state error is rejected as `OAuthStateMismatch`, not accepted as a user denial.

### Provider messaging and fallback visibility

DigitalOcean continues to persist a valid OAuth token when router discovery is empty or unavailable, matching the approved PR scope. Its terminal success text distinguishes “router configured” from “credentials saved with no routers configured.”

Copilot, xAI, and Snowflake log when they use curated models because the catalog is non-authoritative or because authoritative conversion is empty. Logs include status/source only, never credentials.

## Tests

Use red-green TDD for each behavior:

- nested Snowflake/xAI credential and lock paths differ;
- concurrent failed login cannot overwrite a successful token;
- failed config save preserves prior file bytes;
- rollback/delete failures are surfaced and logout does not report success;
- async callers do not run persistence I/O on the event-loop thread;
- wrong-state implicit error yields `OAuthStateMismatch`;
- DigitalOcean degraded success does not name an unrelated model;
- empty authoritative catalogs emit fallback logs;
- unavailable OpenCode Go catalog coverage remains green.

After focused tests, run `make check-pythinker-code`, `make test-pythinker-code`, and `git diff --check`.

## GitHub review completion

Push the verified commit to `feat/auth-login-providers`. Reply inside each CodeRabbit thread with the specific fix and test evidence. For the already-satisfied OpenCode Go coverage thread, cite the existing unavailable-catalog test. Resolve threads only after the pushed head and checks reflect the fixes.
