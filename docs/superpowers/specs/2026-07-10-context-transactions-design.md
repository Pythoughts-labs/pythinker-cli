# Transactional context persistence design

**Status:** Approved as phase 4 of the agent core deepening program

## Problem

`Context` owns JSONL restoration and append operations, but pruning, compaction, revert, and clear callers orchestrate destructive rotation followed by incremental rebuilding. Those callers duplicate compensating rollback logic. Cancellation can bypass `Exception` handlers, rollback can fail, and normal append methods update memory before persistence succeeds.

The module must make a committed context generation truthful: memory and the live JSONL file either remain at the old state or advance coherently to the new state.

## Goals

- Put full-history replacement behind one semantic `Context` interface.
- Preserve existing JSONL record shapes and restoration compatibility.
- Preserve numbered rotation archives.
- Make normal append, checkpoint, and usage operations disk-first.
- Serialize concurrent operations within one `Context` instance.
- Remove duplicated clear/rebuild/rollback logic from the soul.
- Propagate cancellation without leaving split state.

## Non-goals

- Supporting multiple processes writing one session concurrently.
- Introducing a second persistence backend.
- Rewriting every normal append through a full-file replacement.
- Persisting repaired synthetic tool pairs unless they are part of the intended semantic history.
- Moving compaction model calls, hooks, telemetry, or wire events into `Context`.
- Expanding persistence beyond the current local `Path` filesystem seam.

## Module and interface

`Context` remains the deep module. It gains a semantic replacement interface while existing methods delegate during migration.

```python
@dataclass(frozen=True, slots=True)
class ContextReplacement:
    system_prompt: str | None
    messages: tuple[Message, ...]
    token_count: int
    create_checkpoint: bool
    checkpoint_user_marker: bool = False

@dataclass(frozen=True, slots=True)
class ContextCommit:
    checkpoint_id: int | None
    rotated_file: Path | None
    message_count: int

class ContextPersistenceError(OSError):
    operation: str
    category: str

class Context:
    async def replace_history(
        self,
        replacement: ContextReplacement,
    ) -> ContextCommit: ...

    async def append_messages(
        self,
        messages: Sequence[Message],
    ) -> None: ...
```

The concrete names may adapt to repository conventions during planning. Callers provide intended semantic state, not JSONL records or file-operation sequences.

## Internal state model

A private state reducer computes the complete post-commit in-memory state before any live mutation:

- History.
- System prompt.
- Authoritative token count.
- Pending token estimate.
- Next checkpoint ID.
- Torn-tail repair state.

The same record serializer and reducer are used by restore, replacement, and compatibility methods. This avoids parallel logic that could encode different JSONL semantics.

An instance-level `asyncio.Lock` serializes all mutating methods. It prevents two tasks sharing one `Context` from interleaving commits. It does not claim cross-process protection.

## Full-history replacement algorithm

1. Validate the semantic replacement without touching live state.
2. Acquire the context mutation lock.
3. Reconfirm any generation assumptions established before the lock.
4. Derive the new immutable in-memory state and compatible JSONL records.
5. Create a restrictive same-directory temporary file with a unique name.
6. Write the complete record sequence in a synchronous local-filesystem helper executed through `asyncio.to_thread`.
7. Flush and call `os.fsync` on the temporary file before replacement.
8. Reserve and write the numbered rotation archive from the old live file.
9. If archival fails, clean the temporary file and leave old disk and memory unchanged.
10. Atomically replace the live file with the prepared temporary file.
11. Swap the precomputed in-memory state without an intervening await.
12. On POSIX, open and synchronize the parent directory after replacement. On platforms that do not support directory synchronization, document visibility atomicity without claiming equivalent power-loss durability.
13. Release the lock and return `ContextCommit`.

The replace plus in-memory swap is a minimal cancellation-shielded critical section. Cancellation before commit removes temporary state and propagates with old state intact. Cancellation arriving during commit is re-raised only after disk and memory become coherent.

If supported directory synchronization fails after atomic replacement, the operation reports a categorized durability error while retaining the new coherent visible generation. It must not attempt a destructive compensating rollback. The error message distinguishes visible commit from uncertain power-loss durability. A known unsupported platform capability is not reported as a failed commit and is covered by a platform-specific test.

## Rotation archives

Numbered archives remain observable recovery artifacts. Replacement writes the archive before replacing the live file. Archive failure blocks commit.

The archive contains the exact pre-commit live bytes, including any recoverable torn tail. It is not reconstructed from in-memory repaired history. This preserves forensic and manual recovery value.

Archive placeholders and temporary files are cleaned under `BaseException`. Cleanup failure is logged with the original failure retained as the primary cause.

## Disk-first append operations

Normal conversation growth does not rewrite the complete file. `append_messages`:

1. Validates and serializes the entire batch before acquiring the lock.
2. Acquires the mutation lock.
3. Appends the serialized batch in one local-file open/write operation.
4. Flushes before reporting success; normal append retains the existing torn-tail recovery contract rather than claiming full batch crash atomicity.
5. Updates the precomputed in-memory state only after the append succeeds.

A process crash may still leave a torn final JSONL record; existing restore repair remains the recovery contract. A returned successful append guarantees that the implementation observed a successful host write before memory advanced.

Checkpoint plus optional user marker is serialized as one batch. Token-count records are persisted before authoritative and pending counters change. A failed append leaves memory unchanged and raises `ContextPersistenceError`.

## Pruning and compaction

Pruning and compaction continue to prepare semantic replacement messages in `PythinkerSoul`. Model calls, hooks, restore reminders, telemetry, and wire begin/end events stay outside `Context`.

Only after all required preparation succeeds does the soul call `replace_history` once. Hook failure before that call leaves the old context untouched. The soul no longer calls clear, checkpoint, append, and update-token-count as a replacement protocol and no longer performs a compensating rebuild.

Compaction cancellation before commit leaves the old generation. Cancellation during the commit follows the coherent-commit rule above. Wire completion remains in the existing `finally` block.

## Revert and clear

`revert_to` prepares the target semantic history and commits it through the same replacement implementation. The selected checkpoint and marker semantics remain unchanged.

Clear becomes a semantic reset that includes the current system prompt in one replacement. `/clear` no longer performs clear followed by a separate system-prompt write.

Existing public methods remain compatibility adapters during migration. They cannot retain a second destructive implementation.

## Error contract

`ContextPersistenceError` preserves the causal exception and classifies:

- Temporary creation.
- Serialization.
- Write or flush.
- Synchronization.
- Rotation archive.
- Atomic replacement.
- Cleanup.
- Visible commit with uncertain power-loss durability.

Errors contain the operation and a safely rendered session-relative `Path` when available. They do not include message content, prompt text, credentials, or stack traces in user-facing output.

Invalid semantic input raises a validation error before filesystem work. Unsupported cross-process conflict is documented rather than silently treated as safe.

## Test design

Tests are written before implementation and must first fail for the intended missing behavior.

### Compatibility

- `Context.file_backend` remains a local `Path`; no host abstraction or remote backend is introduced.
- Existing system-prompt, checkpoint, message, and usage records restore unchanged.
- Legacy files without system prompt remain readable.
- Malformed and truncated final records retain current repair behavior.
- Tool-call pairing repair remains in memory and does not rewrite source unexpectedly.
- Rotation naming and archive bytes remain compatible.

### Failure injection

Inject failures at temporary creation, each record write, flush, local synchronization, archive creation, atomic replacement, directory synchronization, and cleanup.

For every pre-commit failure, assert exact old live bytes and exact old memory. For a successful commit, assert exact new live bytes and derived memory. For post-replace synchronization failure, assert coherent new visible state plus the categorized durability error.

### Cancellation and concurrency

- Cancel before temporary write, before archive, before replace, and during the shielded commit.
- Assert either complete old or complete new state, never partial state.
- Start concurrent append and replacement operations behind deterministic barriers and assert serialization.
- Cancel a queued writer and prove the lock and later writes recover.
- Verify checkpoint IDs do not skip after failed persistence.

### Soul flows

- Pruning failure and cancellation preserve exact JSONL bytes and memory.
- Compaction failure and cancellation preserve exact JSONL bytes and memory.
- Successful pruning and compaction produce expected records with no duplicate rollback path.
- `/clear` writes a coherent empty generation with system prompt.
- Revert uses one semantic replacement.

Focused context, pruning, compaction, slash, and wire-session tests run before the full Pythinker Code gate.

## Migration and deletion

1. Characterize current record order, rotation, revert, clear, pruning, and compaction behavior.
2. Extract private serializer and state reducer used by restore.
3. Add disk-first append, checkpoint, and usage behavior.
4. Add the mutation lock and concurrency tests.
5. Add `replace_history` with atomic same-directory replacement.
6. Migrate pruning.
7. Migrate compaction.
8. Migrate revert and clear.
9. Delete compensating soul rollback blocks and direct clear/rebuild sequences.
10. Remove compatibility methods only if they are not public and repository search confirms no callers.

The deletion test passes when removing the replacement implementation would force serialization, archive, commit, derived-state, cancellation, and rollback knowledge back into pruning, compaction, revert, and clear callers.

## Rollback

Revert this phase if exact JSONL compatibility, cancellation coherence, or rotation recovery fails. Do not add a runtime flag that chooses between transactional and destructive replacement paths. The old generation and archives provide data recovery, while version control provides code rollback.
