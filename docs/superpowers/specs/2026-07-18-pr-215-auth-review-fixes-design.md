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

Out of scope: provider protocol redesigns, replacing DigitalOcean's provider-required implicit grant, Snowflake Cortex chat transforms, global config merge semantics outside these auth transactions, and new dependencies.

## Design

### Credential filenames

Keep the legacy filename for canonical lowercase `oauth/<safe-segment>` keys so existing credentials remain readable. Encode every other complete key as lowercase hex beneath a dedicated `credentials/v2/` directory, and apply the same mapping to lock files. This keeps identities injective on case-insensitive filesystems without changing released flat-key paths.

Keyring migration reads and validates the credential, writes the file copy, confirms keyring deletion, and only then changes the config reference to file storage. A missing credential leaves the keyring reference unchanged. Backend read/delete errors are safe typed failures; failed cleanup rolls back the file copy and reports a combined failure if rollback also fails.

### Persistence transaction

Expose async persistence helpers to provider login/logout callers. Each helper offloads one complete synchronous transaction to a worker thread. The transaction acquires a bounded, fail-closed inter-process lock derived from the default config path before it snapshots credentials/config, mutates state, writes, or rolls back. Login, logout, replacement, migration, and refresh also share sorted per-credential locks beneath the config lock.

Cancellation uses an atomic phase handshake: cancellation before mutation leaves no effects; cancellation after mutation waits for the owned worker transaction to settle before re-raising. Lock contention is a typed failure and never falls back to an unlocked write.

Config writes use a temporary file in the target directory, flush and fsync it, apply private permissions, and atomically replace the destination. A failed write therefore leaves the previous config file intact.

Login order remains token write then config write. On failure, restore the previous token (or remove the new one), restore the in-memory snapshot, and re-raise. If rollback also fails, raise an explicit persistence error carrying both failure contexts and log the rollback failure without secrets.

Logout writes the config removal first, then deletes credentials. If deletion fails, restore and persist the previous config before raising. It must never emit success after failed credential deletion.

The config-scoped lock covers snapshot through rollback, so a failed concurrent login cannot restore a token observed before another successful transaction.

The transaction reloads the authoritative default config after acquiring the lock, applies the requested provider mutation to that fresh object, persists it, and only then synchronizes the caller's in-memory config. This prevents two serialized sessions with stale `Config` objects from silently replacing each other's provider changes.

An account-scoped provider replacement supplies its provider key to the login transaction, which resolves the previous OAuth reference from the authoritative config while holding the lock. Equal credential keys skip cleanup even if storage metadata differs. After the new token and config commit, the transaction removes a genuinely replaced credential; rollback ordering preserves at least one loadable config/credential pair before surfacing an explicit persistence error.

Background model discovery works on a deep copy, records the provider identity used for each request, and applies collected results through the same authoritative config transaction. Results are skipped if the target provider was logged out or replaced while discovery was in flight; unrelated concurrent provider changes are preserved. Snowflake logout likewise resolves the current account credential from the authoritative locked provider entry rather than trusting a stale caller.

### Active-provider refresh

Runtime refreshes derive the OAuth reference from the active model's provider and never iterate unrelated configured providers. Provider-catalog refresh and title generation pass their already-selected provider reference explicitly. Untargeted compatibility callers retain the existing aggregate behavior only when they provide neither a runtime nor a reference.

### Token response validation

`OAuthToken.from_response()` is the shared trust boundary for provider token responses. It requires a non-empty string access token, accepts an omitted lifetime as unknown, rejects supplied boolean, negative, non-finite, or otherwise malformed lifetimes, and rejects non-string refresh tokens. Provider iterators translate those typed errors into safe error events before any persistence or success event.

### OAuth callback validation

For implicit callbacks, parse `state` and compare it with the expected value before interpreting `error`. RFC 6749 requires the original state on both successful and error responses. A wrong-state error is rejected as `OAuthStateMismatch`, not accepted as a user denial.

### Provider messaging and fallback visibility

DigitalOcean continues to persist a valid OAuth token when router discovery is empty or unavailable, matching the approved PR scope. Its terminal success text distinguishes “router configured” from “credentials saved with no routers configured.”

Router discovery treats only an actually empty router list as authoritative empty data. An all-invalid non-empty list is malformed; a mixed list is partial and keeps valid router names while emitting an explicit degraded-status event.

The implicit loopback callback rejects malformed or negative body lengths and returns `413 Payload Too Large` before reading any body above 64 KiB. Empty and whitespace-only access tokens fail before catalog access, persistence, or success output.

Snowflake supplies its ten-minute default lifetime only when `expires_in` is absent or `None`; explicit falsy values retain the shared token validator's normal semantics.

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
- inactive configured OAuth providers cannot abort refresh of the active runtime provider;
- independently loaded configs preserve both serialized provider updates;
- Snowflake account replacement removes the prior account credential transactionally;
- malformed shared token responses fail before persistence or success;
- malformed and partial DigitalOcean router payloads remain distinguishable;
- oversized implicit callback bodies fail before allocation or blocking reads.
- lock contention and cancellation cannot produce late or unlocked persistence;
- credential and lock paths remain injective across prefixed, nested, case-variant, and unsafe keys;
- keyring read/delete/migration/rollback failures leave config and credentials truthful;
- stale model discovery cannot resurrect a logged-out or replaced provider;
- Snowflake logout deletes the authoritative account credential even from a stale caller;
- blank implicit callback tokens fail before DigitalOcean persistence;
- Snowflake expiry defaulting preserves explicit falsy values for shared validation.

After focused tests, run `make check-pythinker-code`, `make test-pythinker-code`, and `git diff --check`.

## GitHub review completion

Push the verified commit to `feat/auth-login-providers`. Reply inside each CodeRabbit thread with the specific fix and test evidence. For the already-satisfied OpenCode Go coverage thread, cite the existing unavailable-catalog test. Resolve threads only after the pushed head and checks reflect the fixes.
