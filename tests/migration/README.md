# Legacy data migration compatibility tests

The suite synthesizes old installations with the real `store.py` from a Git ref, then runs the current code from a copied application tree with temporary `LOCALAPPDATA`, home, and temp paths. The historical ref defaults to `v0.208`; set `BFO_LEGACY_REF` to exercise another available tag. For the pinned v0.208 baseline, the generator verifies both the tag commit and `store.py` blob identity before creating fixtures.

Install the test dependencies with `python -m pip install -r requirements-test.txt`, then run the complete project suite, including these tests, from one command:

```powershell
python tools/run_isolated_tests.py
```

To print fixture-versus-current table counts, integrity checks and logical SHA-256 evidence without running unrelated tests, use `python tools/legacy_fixture_evidence.py`.

To run only the migration compatibility suite, use `python tools/run_isolated_tests.py --migration-only`. To run one unittest by ID for diagnosis, use `python tools/run_isolated_tests.py --test migration.test_vault_migration.VaultMigrationTests.test_MIG_010_close_and_reunlock_restores_encrypted_data`.

The runner copies source into a temporary checkout and excludes `.git`, local credentials, build outputs, `tests/.test-data`, and caches. Migration tests do not import the live checkout's `store.py`. Synthetic Cookie and API key values are deliberately invalid; outbound proxies point to a closed local port. No Bilibili or paid LLM endpoint is called.

Each migration test uses a fresh temporary profile. The fixture seed freezes historical `time.strftime()` inside its subprocess so core logical hashes remain stable across runs. The three immutable fixtures are generated lazily and independently:

- `legacy-empty`: v0.208-created schema with no business rows.
- `legacy-normal`: 10 folders, 500 videos, two memberships per video, profiles, analysis/plans, scan history, provider/model, and synthetic credentials.
- `legacy-edge`: empty folders, duplicate alias/reference, interrupted scan and staged row, invalid marker, partial legacy settings, and a captured committed WAL sidecar.

SQLite validation records `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, table counts, and canonical logical-row SHA-256. The database file hash is not compared across encryption. The tests compare normalized business rows after decrypting an encrypted snapshot.

The current test suite includes assertions for expected safety behavior that expose defects in the code under test. A red result is evidence for investigation; do not remove or weaken the assertion to make CI green. Windows Hello and installed Electron installer behavior must stay `NOT_RUN` unless exercised on a suitable Windows host.

For a retained unpacked Electron package, `python tools/run_isolated_packaged_smoke.py <workspace> --profile-name launch-profile-check` launches it with a new profile inside that workspace and runs the shell page/exit checks. The helper rejects workspaces outside the repository's `.bfo-package-smoke-*` directories. This does not replace testing the traditional installer, NSIS installation, Windows Hello, or both shells against the same migrated fixture.

| ID | Test method |
| --- | --- |
| MIG-001–008 | `test_legacy_migration.py` empty/normal/edge, integrity, retention, idempotence |
| MIG-009–013 | `test_vault_migration.py` encryption, restore, wrong password, recovery, corruption |
| MIG-014 | `test_failure_recovery.py` copy interruption, partial encryption, reinitialization |
| MIG-015–018 | `test_legacy_migration.py` shell path contract, rollback, fresh install, repeated/concurrent access |

If `BFO_LEGACY_REF` is changed, confirm the ref exists in the repository and update the fixture expectations only after reviewing that historical version's schema and actual store initialization logic.
