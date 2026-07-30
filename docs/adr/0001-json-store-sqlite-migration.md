# ADR-0001: Move `harness_profile_store` and `extension_store` off single-file JSON

## Status
Approved. `harness_profile_store` implemented. `extension_store` not yet implemented.

## Impact
Medium-High (touches two core durable stores + the primary/worker sync protocol; no external API/contract change).

## Context
Both stores persist one JSON file under `ba_home()` via `json_store.read_json`/`write_json`:

- `backend/harness_profile_store.py` → `harness_profiles.json`, one dict entry per profile (`{id → profile}`). `SCHEMA_VERSION`, no migrations — a version mismatch raises and the store must be wiped. Exposes `export_harness_sync_state`/`import_harness_sync_state` for whole-store replication to worker nodes.
- `backend/extension_store.py` → one store file, one dict entry per installed extension (manifest, versions, enabled state, settings/config overlays, MCP grants, instructions, dependency state). `STORE_SCHEMA_VERSION`, has a real v1→v2 migration (`_migrate_store_v1_to_v2`). Exposes the equivalent `export_extension_sync_state`/`import_extension_sync_state`.

Both follow the identical anti-pattern: every write reads the **entire** file, deep-copies/validates, then does an atomic full-file rewrite (tmp + `os.replace`), serialized behind one process-wide `threading.RLock`. Every access pattern in both modules is single-record: get-by-id, list-all, upsert-by-id, delete-by-id. There is no cross-record query, filter, or join anywhere in either module.

As profile count / extension count / settings payload size grows, every single-field edit pays an O(total store size) read+serialize+write cost, and concurrent editors (multi-agent, multi-session — the standing operating assumption in this repo) queue behind the one lock for the whole store, not just the record being touched. That's the reported symptom: harness-profile edits "take a lot of time" under concurrent load.

## Decision
Move both stores onto SQLite, but keep it a **document-per-row** substrate, not a normalized relational schema:

```sql
CREATE TABLE profiles (
  id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  revision TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  data TEXT NOT NULL   -- json.dumps(profile), same shape as today's dict
);
```

(same pattern, same column set, for `extensions`; `extension_store` additionally gets a couple of narrow secondary tables — see below).

- One SQLite file per store, opened WAL mode (`PRAGMA journal_mode=WAL`), under `ba_home()` exactly where the JSON file lives today.
- A record write is `INSERT OR REPLACE ... WHERE id = ?` inside a short transaction — no other row is read or rewritten. WAL mode lets readers proceed concurrently with the single in-flight writer, and writers no longer serialize on total store size, only on actual write duration (now O(1 record), not O(all records)).
- The Python-side `RLock` becomes a much narrower critical section (or is replaced by relying on SQLite's own transaction isolation + `BEGIN IMMEDIATE` for the read-modify-write upsert pattern); still process-local, but no longer gates unrelated-record writes against each other.
- `_normalized_payload`, all `_validate_*`/`_clean_*` functions, `_revision` hashing, `base_profile_id` chain validation, extension manifest validation, dependency-plan logic — **all untouched**. They already operate on plain dicts; the storage layer swaps under them.
- `export_*_sync_state` / `import_*_sync_state` keep their existing JSON wire shape (`{schema_version, profiles: {...}}` / equivalent for extensions) — sync payloads stay JSON over the wire; only the on-disk representation on each host becomes SQLite. Import stays "replace this host's store" (already documented as no-partial-apply), implemented as `DELETE FROM profiles; INSERT ...` in one transaction.
- Schema version policy is unchanged: a stored `schema_version` that doesn't match current code still means "incompatible, refuse" for `harness_profile_store` (no migration support, by existing rule) — that rule lives in the app-level check on load, not in the SQL layer, so it carries over verbatim. `extension_store`'s existing v1→v2 dict-shape migration continues to run in Python after `SELECT data FROM extensions WHERE id = ?`, before validation — no new SQL-level migration engine is introduced for that.
- `extension_store`'s two write-heavy side files that are actually per-extension counters/logs, not whole-store state — `_slow_calls_path()` (rolling call-latency history) — move to their own `slow_calls` table (`extension_id, ts, duration_ms`) rather than staying a second full-file JSON blob, since that file has the same "append means rewrite everything" shape and is a natural fit for row-per-event.
- `_fingerprint_store_file`/`store_fingerprint()` (used elsewhere to detect "did the store change since I last read it") is reimplemented as a `meta(key, value)` table with a monotonic `store_version` integer bumped in the same transaction as every write — cheaper and more precise than the current `(mtime_ns, size)` file-stat tuple, and immune to the coarse mtime granularity some filesystems have.

### Rejected alternative: full relational normalization
Break profiles/extensions into proper columns (name, description, per-override rows in child tables with foreign keys, etc.) so SQL can filter/join on individual fields.

Rejected because: no code path anywhere in either module ever queries "profiles where X" or joins across records — every read is by primary key or "give me all of them" for a list view that already re-validates/re-serializes each record in Python. Normalizing would mean rewriting the ~600 lines of nested-dict validation in `harness_profile_store` and the multi-thousand-line manifest/settings/capability/MCP validation in `extension_store` to assemble/disassemble across tables, for zero query-pattern benefit — pure cost, no win. Revisit only if a real cross-record query need shows up (e.g. "find all profiles overriding extension X" at DB level instead of in-process filter over `list_profiles()`).

### Rejected alternative: keep JSON, just narrow the lock / avoid full rewrite
Shard `harness_profiles.json` into one file per profile (already close to per-extension for `extension_store`?) and take a per-file lock, or diff-patch the file in place.

Rejected because: still leaves "list all profiles" as N file opens instead of 1 query, still needs hand-rolled crash-safety per file (today's atomic-write helper is doing real work), and doesn't get WAL's concurrent-read-during-write property or the monotonic-version-for-cache-invalidation primitive for free. SQLite is a proven, already-vendored (stdlib `sqlite3`) building block that solves all three (atomicity, concurrency, versioning) at once; hand-rolling the same guarantees on top of many small JSON files is more code for a worse result.

## Quality attributes
- **Performance/scalability** (primary driver): O(1 record) writes instead of O(store size); this is the whole point.
- **Reliability**: SQLite WAL gives the same crash-safety guarantee `json_store`'s atomic tmp+replace gives today, for less custom code.
- **Simplicity**: deliberately deprioritizing "queryability" (the relational-normalization alternative) to keep the validation/business-logic layer at zero net new complexity.
- **Maintainability**: storage swap is isolated to the `_load`/`_save`/`_path` functions in each module; the ~90% of each file that is validation/normalization logic does not move.
- **Security**: no change in trust boundary — same process, same `ba_home()` confinement, no new external input surface. `extension_store`'s Ed25519 signature verification and manifest validation logic is untouched.

## Consequences / follow-ups
- Test isolation (`paths.engage_test_home`) needs no change — the SQLite file lives under the same `ba_home()`-resolved directory a tempdir override already redirects.
- `store_fingerprint()` callers (anything polling "did this store change") get a cheaper, more precise signal — worth grep-auditing call sites during implementation to confirm none depend on the literal `(mtime_ns, size)` tuple shape. (Verified for `harness_profile_store`: no caller in the codebase uses it — `harness_profile_resolver.py` explicitly excludes it. `extension_store.store_fingerprint()` is used and unaffected by this ADR.)
- `extension_store.py` additionally has gzip/tar handling for extension package install — that is package-file I/O, unrelated to the store-record persistence being changed here, and is out of scope.
- Rollout order: do `harness_profile_store` first (smaller, no existing migration code) to validate the pattern, then apply the same pattern to `extension_store` (bigger, has to also carry the v1→v2 migration and the `slow_calls` side-table split).

## Scope boundary (explicit)
A repo-wide survey turned up ~14 other JSON-backed `*_store.py`/`stores/*.py` modules with a similar dict-of-records write pattern (`worker_store`, `task_store`, `chat_store`, `session_organization_store`, `inbox_store`, `node_registry_store`, `team_store`, etc.). Those are **out of scope for this ADR by design, not oversight**: they hold session/runtime/execution state (high write churn tied to live sessions and workers, different retention and consistency requirements) rather than user-authored config records (low write churn per record, edited deliberately). Config-record stores and session-runtime stores are different problem categories and should not share one migration decision. If a session-runtime store's write pattern becomes a proven bottleneck later, it gets its own ADR evaluated on its own terms — not folded into this one.

Also evaluated and rejected as candidates: `extension_incident_outbox.py` (transient TTL'd outbox, self-trims to ≤1000 short entries, not a durable config record) and `extension_jobs.py` (already one file per job — never had the full-file-rewrite problem this ADR fixes).

## Recommendation
Proceed with the document-per-row SQLite design above for both stores, `harness_profile_store` first.

## Implementation status
- `harness_profile_store.py`: **done.** SQLite (`harness_profiles.db`, WAL mode), `profiles` table (id/schema_version/revision/updated_at/data) + `meta` table (schema_version guard, monotonic `store_version` counter for `store_fingerprint()`). Row-level write choke points: `_write_profile_row` (single-profile upsert, used by `_commit_profile`), `_delete_profile_row`, `_replace_all_profiles` (full-store replace, used only by `import_harness_sync_state` — inherent O(n) for that operation's "replace this host's store" contract). All validation/normalization/base-chain logic untouched. Tests updated/added in `backend/scripts/test_harness_profile_store.py` (schema-mismatch-refuses via direct meta-table corruption, `store_fingerprint` bump-on-write/delete, write-touches-only-its-own-row, WAL mode assertion) and `backend/scripts/test_harness_profile_resolver_default.py` (raw-write test helper migrated from `_save(data)` to `_write_profile_row(profile)`). All runnable tests pass under `backend/.venv`.
- `extension_store.py`: **not yet implemented.** Deferred to a follow-up turn given its size (~8600 lines) relative to remaining session budget.
