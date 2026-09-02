---
id: memory
title: Memory System
sidebar_position: 6
slug: /guides/memory
---

# Memory System

FERAL's memory is a four-tier architecture stored in a single SQLite database (`~/.feral/memory.db`). Each tier serves a different retention and retrieval pattern. On top of the tiers sit hybrid search, diversity reranking, session compaction, wiki compilation, and P2P sync.

## Four Memory Tiers

### Working Memory

In-RAM context for the current session. Holds the conversation history, tool results, and scratch state. Cleared when the session ends.

```python
session.working_memory.append({
    "role": "user",
    "content": "What's the weather in NYC?",
})
```

Working memory is capped at a configurable token budget. When it overflows, the oldest messages are compacted into an episode (see [Session Compaction](#session-compaction)).

### Episodic Memory

Auto-generated summaries of past conversations. Each episode captures the key facts, decisions, and outcomes from a session.

```sql
-- Schema
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    summary TEXT,
    entities TEXT,        -- JSON array of extracted entities
    created_at REAL,
    embedding BLOB        -- 384-dim float32 vector
);
CREATE VIRTUAL TABLE episodes_fts USING fts5(summary, entities);
```

Episodes are created automatically when a session ends or when working memory overflows.

### Semantic Memory / Knowledge Graph

Persistent facts stored as subject-predicate-object triples. Extracted automatically from conversations or added explicitly via "remember X" commands.

```sql
CREATE TABLE knowledge_graph (
    id TEXT PRIMARY KEY,
    subject TEXT,
    predicate TEXT,
    object TEXT,
    confidence REAL,
    source_episode TEXT,
    created_at REAL
);
```

```bash
# User says: "Remember that my doctor's name is Dr. Chen"
# Extracted triple:
# subject=user, predicate=doctor_name, object="Dr. Chen", confidence=0.95
```

### Execution Log

An append-only log of every tool invocation, including arguments, results, latency, and success/failure status.

```sql
CREATE TABLE execution_log (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    tool_name TEXT,
    args TEXT,           -- JSON
    result TEXT,         -- JSON
    latency_ms INTEGER,
    success BOOLEAN,
    created_at REAL
);
```

Useful for debugging, skill auto-generation, and auditing what the agent actually did.

## Hybrid Search

Memory retrieval combines SQLite FTS5 (keyword) and vector similarity (semantic) to get the best of both worlds.

```python
from feral_core.memory import MemoryStore

store = MemoryStore()

results = await store.search(
    query="Dr. Chen appointment",
    top_k=10,
    strategy="hybrid",    # "fts", "vector", or "hybrid"
    alpha=0.6,            # weight: 0.0 = pure FTS, 1.0 = pure vector
)
```

**How hybrid scoring works:**

1. FTS5 returns top-N by BM25 score, normalized to `[0, 1]`.
2. Vector search returns top-N by cosine similarity, already in `[0, 1]`.
3. Scores are combined: `final = alpha * vector_score + (1 - alpha) * fts_score`.
4. Results are merged and deduplicated by ID.

Vector embeddings use `all-MiniLM-L6-v2` (384 dimensions) by default, computed locally via `sentence-transformers`. For larger deployments, swap in OpenAI `text-embedding-3-small` via config.

## MMR Diversity Reranking

After hybrid search, **Maximal Marginal Relevance** reranks results to reduce redundancy. Without MMR, the top-5 results might all describe the same event from different angles.

```python
results = await store.search(
    query="meeting notes",
    top_k=5,
    mmr=True,
    mmr_lambda=0.7,   # 1.0 = pure relevance, 0.0 = pure diversity
)
```

The algorithm iteratively selects the result that maximizes `lambda * relevance - (1 - lambda) * max_similarity_to_already_selected`.

## Session Compaction

When working memory exceeds its token budget, the compactor summarizes older messages into an episode and evicts them from the active context.

```json
// ~/.feral/settings.json — "memory" section
{
  "memory": {
    "working_memory_budget": 8000,
    "compaction_trigger": 0.85,
    "compaction_strategy": "summarize"
  }
}
```

The compaction flow:

1. Select messages beyond the budget.
2. Prompt the LLM to summarize them into a structured episode.
3. Insert the episode into `episodes` table with embedding.
4. Replace the compacted messages with a system note: `[Session compacted — N messages summarized]`.

## Wiki Compilation

The **Memory Wiki** compiles episodes, notes, and knowledge graph entries into durable, human-readable wiki pages organized by topic.

```bash
curl -X POST http://localhost:9090/api/wiki/compile
curl http://localhost:9090/api/wiki/list
curl http://localhost:9090/api/wiki/page/health
```

Wiki pages are stored in `~/.feral/wiki/` as Markdown files with YAML frontmatter tracking provenance:

```yaml
---
topic: health
sources:
  - episode:abc123
  - kg:triple_456
last_compiled: 2025-06-15T10:30:00Z
---
# Health

- Doctor: Dr. Chen (added 2025-03-10)
- Blood type: O+ (added 2025-01-22)
- Allergies: penicillin (added 2025-04-05)
```

Compilation runs automatically on a schedule or on-demand. New facts merge into existing pages; conflicts are flagged for user review.

## P2P Sync

For multi-device setups (laptop + phone + home server), FERAL supports peer-to-peer memory synchronization over the `/sync` WebSocket endpoint.

```json
// ~/.feral/settings.json — "sync" section
{
  "sync": {
    "enabled": true,
    "peers": [
      "ws://homeserver.local:9090/sync",
      "ws://phone.local:9090/sync"
    ],
  conflict_resolution: last_write_wins  # or manual
```

Sync uses **HLC (Hybrid Logical Clock)** timestamps with last-write-wins conflict resolution at the row level. Each peer maintains a write-ahead log (WAL) and exchanges deltas since the peer's last-seen HLC. There is no CRDT merge — the implementation is HLC + LWW + WAL replication.

```bash
# Check sync status
feral sync status

# Force sync now
feral sync now
feral sync peers
```

### Peer identity

A peer brain is a fuller principal than a paired device: it can write
AND delete into your store. Each peer therefore gets its own credential
rather than sharing one passphrase.

```bash
# On the brain that will ACCEPT the connection:
feral sync peer invite laptop        # prints the grant ONCE

# On the brain that will DIAL it, with the grant you just copied:
feral sync peer accept homeserver.local:9090 <grant>

feral sync peer list                 # roster + who is still on the shared secret
feral sync peer revoke <peer_row_id>
```

The grant is stored argon2id-hashed on the accepting side (the same
scheme as device pairing) and encrypted in the vault on the dialling
side. It binds to the dialling brain's `node_id` the first time it is
used, so the same grant presented by a second brain is refused. Invites
must be redeemed within an hour; a redeemed grant lives in a sliding
7-day window that renews on every successful sync, so a peer that stops
talking lapses without anyone having to revoke it.
`FERAL_SYNC_PEER_TTL_SECONDS` and `FERAL_SYNC_PEER_INVITE_TTL_SECONDS`
override both.

**Migration.** Existing installs keep working: the shared
`FERAL_SYNC_PASSPHRASE` is still accepted, every use of it is recorded,
and `feral sync status` names the brains that are still relying on it.
Until you set `FERAL_SYNC_REQUIRE_PEER_IDENTITY=1`, the identity mode
reads `mixed`, never `per_peer`, because the passphrase would still let
anything that knows it in. Enrol every brain in the straggler list, then
set that variable to retire the shared secret.

**What revoking does and does not do.** It stops future exchanges. It
cannot recall memory the peer has already replicated, and it cannot
delete their copy. Prefer letting a grant lapse over relying on
revocation for anything that has already synced.

### Scoped sharing

Enrolling a peer decides **whether** it may connect. A scope grant
decides **what** it gets, and the default is nothing.

Every replicated operation carries a scope. A peer receives an
operation only if its scope is in the set you granted that peer, and
your brain accepts an operation only if its scope is in that same set.
So two operators can pool one named feed without pooling anything else.

```bash
# Both operators run this, each naming the other brain's node_id.
# `feral sync status` prints your own node_id; `feral sync peer list`
# prints your peers'.
feral sync peer scope grant <node_id> robot-events

feral sync peer scope list
feral sync peer scope revoke <node_id> robot-events
```

Scope names are 1-64 characters of lowercase ASCII, digits, `-`, `_` or
`.`, starting and ending with a letter or digit. `private` is reserved.

**Pooling takes two grants.** Your roster is your whole policy toward a
peer and it governs both directions: what you send and what you are
willing to hold. If only one side grants a scope, nothing moves. That
is deliberate, because the alternative would let somebody else's roster
decide what lands in your store.

**Where a scope comes from.** The writer names it. `save_note` and
`episode_save` take a `scope=` argument, and anything that does not
name one is `private`. Scope is not derived from the table: doing that
would make the sharing boundary a property of the schema, so the next
table anyone adds would inherit some other table's posture silently. A
delete is the one exception. It inherits the scope of the row's newest
logged write, so a removal reaches exactly the peers the write reached
and no further.

**Everything ambiguous is private.** Unscoped writes, WAL rows that
predate this feature, scope names that do not parse, and operations
from a peer running an older build are all `private`, and `private`
never replicates and cannot be granted. A bug in this path produces
"shared too little", never "shared too much", because sharing too much
cannot be undone: the other brain is somebody else's.

**Memory written before you upgraded stays private, permanently.**
Nothing is retroactively classified. The `scope` column defaults to
`private` for every pre-existing row rather than guessing, so upgrading
never turns your existing history into something poolable. Re-scoping
existing memory would be a migration onto the source tables, and that
is not what this does.

**What revoking a scope does and does not do.** It stops future
replication in that scope, in both directions, from the next exchange
onward. It does **not** recall anything that already crossed. Those
operations are on a disk you do not control and no command here can
reach them. Revocation is not recall.

**No cross-peer caps.** There is deliberately no aggregate limit like
"share at most N operations across all peers". Kleppmann and Howard's
I-confluence result is that an invariant of that shape cannot be
enforced across independent runtimes without coordination, and a limit
that silently does not hold is worse than no limit. Any limit in this
code is local to a single enforcement point and is documented as such
where it appears.

## API Reference

| Endpoint | Method | Description |
|:---------|:-------|:------------|
| `/api/memory/search` | POST | Hybrid search across all tiers |
| `/api/memory/remember` | POST | Store a fact in the knowledge graph |
| `/api/memory/episodes` | GET | List recent episodes |
| `/api/memory/wiki` | GET | List wiki pages |
| `/api/memory/wiki/{topic}` | GET | Read a wiki page |
| `/api/memory/stats` | GET | Memory size, tier counts, index health |
| `/sync` | WebSocket | P2P memory sync between nodes |
| `/api/sync/roster` | GET | Peer identity roster + identity mode |
| `/api/sync/roster/invite` | POST | Mint a grant for one peer (returned once) |
| `/api/sync/roster/accept` | POST | Store a grant another brain issued us |
| `/api/sync/roster/{peer_row_id}` | DELETE | Revoke a peer's grant |
| `/api/sync/scopes` | GET | Per-peer scope grants (what each peer receives) |
| `/api/sync/scopes` | POST | Grant one scope to one peer by `node_id` |
| `/api/sync/scopes/{node_id}/{scope}` | DELETE | Revoke one scope from one peer |
