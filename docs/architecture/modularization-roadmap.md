# Modular architecture experiment (v0.209 baseline)

Branch: `feat/modular-architecture-task-manager`

## Scope and guardrails

This is an incremental **modular monolith** experiment, not a wholesale port of `new_arch`.
The production `server.py`, `store.py`, `static/app.js`, vault, and packaging remain
unchanged in the first slice. No production endpoint calls the new task manager yet.

### First slice (committed)

- `bili_fav/tasks/manager.py`: independent task runtime with persisted snapshots,
  exclusive resource keys, cooperative cancellation, and fail-closed interruption recovery.
- `static/modules/api-client.js`: reusable API transport for a future frontend migration.
- `static/modules/task-store.js`: DOM-independent reactive task state.
- `tests/test_task_manager.py`: isolated smoke/regression tests.

**Important:** JSON journaling is an experimental adapter, not the final store. It is
not integrated with vault locking, multi-process coordination, or the application's
SQLite transactions. Do not enable it for real remote mutations until those boundaries
are implemented and tested.

## Target structure

```text
bili_fav/
  api/             # FastAPI routers, schemas, request validation
  application/     # use cases: scan, analyze, plan, apply
  domain/          # entities and invariant rules, no network/database imports
  adapters/        # Bilibili, LLM providers, storage implementations
  tasks/           # task scheduling, progress, cancellation, recovery
  security/        # vault and credentials integration
static/
  modules/
    api-client.js  # transport
    task-store.js  # task state
    features/      # scan, analysis, planning, review, execution
    components/    # reusable UI components
```

## Migration phases

1. **Task manager foundation**: unit tests, state model, cancellation, interruption
   recovery. Add SQLite journal and vault lifecycle integration before production use.
2. **Backend module extraction**: extract pure business logic first; use thin routers
   and dependency injection; keep legacy routes and response schemas unchanged.
3. **Frontend module extraction**: move transport, state, and UI features into ES modules
   incrementally. Keep `app.js` as compatibility entry until feature parity is proven.
4. **Task integration**: adapt scan, analysis, profiling, plan, and apply one at a time.
   Remote write operations must use explicit reconciliation and no blind retries.
5. **Compatibility gates**: v0.208 -> v0.209 migration fixtures, vault lock/unlock and
   plaintext cleanup, Windows legacy and Electron shells, build/release workflows.

## Acceptance criteria

- Existing routes and payloads remain backward compatible.
- Existing data and vault are untouched by first-slice modules.
- A task interrupted during a remote write is marked unknown/interrupted, never replayed.
- Concurrent mutation-heavy jobs are serialized.
- Frontend modules are tested without requiring Electron.
- All original CI checks pass before any production wiring.
