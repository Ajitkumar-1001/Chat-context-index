# Privacy and Deletion Limitations

`cci` treats conversation content as sensitive by default and gives you an explicit deletion
operation, but neither claim is unconditional — read this before relying on either for a
compliance requirement.

## What `clear_history()` actually does

`clear_history(expected_history_id)` atomically deletes the live history/index/FTS data in the
authoritative store, rotates `cache_generation` so no future library call can reach pre-clear
memo entries (even ones still physically present under the old scope), deletes the local memo
cache, and attempts a scoped external (Redis) purge.

**What it does not do**: prove erasure from database backups, filesystem free pages, a Redis
instance's own snapshots/replicas, upstream model-provider systems, or any response your
application already returned to a user before the clear. If the Redis purge attempt cannot
complete, `clear_history()` reports `cache_purge_pending: true` truthfully — it never claims a
completed purge it did not perform.

## What `stats()` exposes and excludes

By default, `stats()` returns only counts, timings, and outcome codes (e.g. `provider_calls`,
`memo_hits`, `history_message_count`) — identifiers and content hashes, never prompt text,
message bodies, or credentials. There is no verbose/payload-tracing opt-in in this release; the
default posture is the only posture.

## Trust boundaries (constitution Principle VI)

- A history ID or cache namespace is **not** an authentication credential — your application is
  responsible for authorizing a user before selecting or opening a history.
- Stored conversation content (including system-role messages and tool outputs) is treated as
  **untrusted data** when it reaches a model: `ask()`'s synthesis prompt and `index()`'s
  classification/summary prompts render retrieved text through one shared, delimited
  evidence-rendering component, never concatenated as if it were an instruction. This reduces
  prompt-injection risk; it is not a claim that injection is impossible.
- Tool calls recorded in old messages are stored as data and are never re-executed.
- "Local" or "offline" must not be claimed once a remote model provider or Redis is enabled —
  both are genuinely remote calls at that point.
- Non-local Redis connections should use TLS and least-privilege, key-scoped credentials; this
  is a deployment responsibility, not something the library enforces for you.
