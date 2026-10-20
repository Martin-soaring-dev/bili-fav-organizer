# Historical database fixtures

`v0209_schema7.sqlite3` was initialized by the `store.py` from the `v0.209`
tag (commit `e47000b747b4387a527216cc1431eb516443977d`) and populated only with
synthetic records. It contains no account data, cookies, credentials, or user
profile files. The fixture is used to verify the real schema 7 source layout
before the current code migrates it to schema 8.

SHA-256: `511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273`

`v0200_schema5.sqlite3` and `v0203_schema6.sqlite3` follow the same rule: each
one was created by running that release's own `store.py` (`_initialize()`), then
filled with synthetic folders/videos/analyses/plans. They exist because the
supported upgrade floor was decided to be schema 5 — the oldest ledger any
released version ever wrote. Every released tag was checked: v0.100–v0.200 are
schema 5, v0.203 is schema 6, v0.205–v0.209 are schema 7. **No released version
ever produced a schema 1 database, so no schema 1 fixture exists or is invented
here.**

- `v0200_schema5.sqlite3` — tag `v0.200`, commit `ce8259407e4f8a228910b860559efbba49bcd9c5`
  SHA-256: `2613f5b9315af1b36c60a0c311d76b1ea3ebed5ac7fc9ea190bd8cf325cb95d5`
- `v0203_schema6.sqlite3` — tag `v0.203`, commit `2f1644a0a303c19e4a7854ec6748197a6c841b87`
  SHA-256: `2a5a31ac3b9d248a19f7f049af3a076dc8614e51647d14bebb438d3b70a53a9d`

All three fixtures contain only synthetic data: 收藏夹 `OLD-FOLDER-A`/`OLD-FOLDER-B`,
videos `BVOLD0001`–`BVOLD0003`, two analyses, one plan. Rebuild them by running the
matching tag's `store.py` in an isolated `BILI_FAV_ORGANIZER_DATA_DIR`; never point
that at a real profile.
