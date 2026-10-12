# New architecture implementation status

Branch: `feat/modular-architecture-task-manager` (draft, do not merge yet).

## Implemented
- `bili_fav/tasks`: concurrent task runtime, exclusive resources, cooperative cancellation, JSON/SQLite journal, interrupted-on-restart semantics.
- `bili_fav/application/task_bridge.py`: bridge for six legacy background workflows.
- `bili_fav/api/tasks.py`: read-only task listing/detail and normalized legacy state endpoints.
- `static/modules/api-client.js`, `task-store.js`, `task-center.js`: standalone frontend transport, state store and task-center panel.
- Backend package boundaries established: `application`, `domain`, `adapters`, `security`.

## Not yet implemented — required before declaring full modularization
- Extract scan/analyze/apply/folder orchestration logic out of `server.py` into application services and routers.
- Extract existing UI features out of the 5k-line `static/app.js` into feature modules, not only a new task-center widget.
- Connect cancellation events to legacy stop flags and implement consistent task progress and result reporting.
- Integrate task journal with vault lock/unlock, data-at-rest encryption policy, and shutdown semantics.
- Ensure remote mutations are never auto-replayed and preserve idempotency/confirmation gates.
- End-to-end functional and packaging validation.

## Compatibility rules
- Preserve old API routes and response fields during migration.
- Keep `main`, `master`, and `new_arch` untouched.
- Task journal stores metadata only; do not store cookies, tokens, prompts or other credentials in it.
- The new task-center is intentionally read-only.
