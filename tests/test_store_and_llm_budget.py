"""store 迁移 / 分析 stale 生命周期 / LLM 上下文拆批。"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import llm_analyzer
import store


class _TempStoreMixin:
    def _open_temp_store(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old = (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
                     store.LEGACY_DATA_DIR)
        root = Path(self._tmp.name)
        store.DATA_DIR = root / "data"
        store.DB_FILE = store.DATA_DIR / "library.sqlite3"
        store.LEGACY_DB_FILE = root / "missing" / "library.sqlite3"
        store.LEGACY_DATA_DIR = root / "missing-legacy"
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        store._initialize()

    def _close_temp_store(self):
        store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE, store.LEGACY_DATA_DIR = self._old
        self._tmp.cleanup()


class StoreSchemaMigrationTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._open_temp_store()

    def tearDown(self):
        self._close_temp_store()

    def test_fresh_database_reaches_current_schema_version(self):
        with closing(store._connect()) as conn:
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(store._SCHEMA_VERSION, int(row[0]))

    def test_analyses_table_has_status_and_dependent_ids(self):
        with closing(store._connect()) as conn:
            columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(analyses)")}
        self.assertIn("status", columns)
        self.assertIn("dependent_ids", columns)

    def test_reinitialize_is_idempotent(self):
        store._initialize()
        with closing(store._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertGreaterEqual(count, store._SCHEMA_VERSION)

    def test_legacy_empty_db_gets_default_schema_without_error(self):
        # 再次初始化不应抛错，也不应重复导入 legacy bundle
        store.clear_analysis()
        store._initialize()
        self.assertEqual(store.load_analysis_rows(), {})


class AnalysisStaleLifecycleTests(_TempStoreMixin, unittest.TestCase):
    def setUp(self):
        self._open_temp_store()

    def tearDown(self):
        self._close_temp_store()

    def test_save_analysis_defaults_to_current_with_dependents(self):
        store.save_analysis(
            {"BV1": {"target": "夹A", "action": "move_to_existing"}},
            meta={"BV1": {"status": "current", "dependent_ids": ["10", "20"]}},
        )
        rows = store.load_analysis_rows()
        self.assertEqual("current", rows["BV1"]["status"])
        self.assertEqual(["10", "20"], rows["BV1"]["dependent_ids"])
        self.assertEqual("夹A", rows["BV1"]["record"]["target"])

    def test_mark_all_analyses_stale_preserves_dependents(self):
        store.save_analysis(
            {"BV1": {"target": "夹A"}},
            meta={"BV1": {"status": "current", "dependent_ids": ["10"]}},
        )
        store.mark_all_analyses_stale()
        rows = store.load_analysis_rows()
        self.assertEqual("stale", rows["BV1"]["status"])
        self.assertEqual(["10"], rows["BV1"]["dependent_ids"])

    def test_set_analysis_statuses_overwrites_selected_rows(self):
        store.save_analysis(
            {"BV1": {"a": 1}, "BV2": {"a": 2}},
            meta={"BV1": {"status": "current"}, "BV2": {"status": "current"}},
        )
        store.set_analysis_statuses({"BV1": ("stale", ["99"])})
        statuses = store.analysis_status_map()
        self.assertEqual("stale", statuses["BV1"])
        self.assertEqual("current", statuses["BV2"])
        self.assertEqual(["99"], store.load_analysis_rows()["BV1"]["dependent_ids"])

    def test_upsert_replaces_record_and_status(self):
        store.save_analysis({"BV1": {"v": 1}},
                            meta={"BV1": {"status": "current", "dependent_ids": ["1"]}})
        store.save_analysis({"BV1": {"v": 2}},
                            meta={"BV1": {"status": "stale", "dependent_ids": ["2"]}})
        rows = store.load_analysis_rows()
        self.assertEqual(2, rows["BV1"]["record"]["v"])
        self.assertEqual("stale", rows["BV1"]["status"])
        self.assertEqual(["2"], rows["BV1"]["dependent_ids"])


class LLMContextBudgetSplitTests(unittest.TestCase):
    def _config(self, **kw):
        return llm_analyzer.LLMConfig(
            base_url=kw.get("base_url", "http://model.test/v1"),
            model=kw.get("model", "test-model"),
            max_tokens=kw.get("max_tokens", 1024),
            context_window_tokens=kw.get("context_window_tokens", 4096),
        )

    def _videos(self, n, title_len=20):
        return [{"bvid": f"BV{i}", "aid": i, "title": "题" * title_len,
                 "folder_ids": ["10"]} for i in range(1, n + 1)]

    def test_prepare_call_requests_split_when_batch_exceeds_context(self):
        analyzer = llm_analyzer.LLMAnalyzer(
            self._config(context_window_tokens=4096),
            folders=["夹A"],
            folder_profiles=[{
                "id": "10", "name": "夹A",
                "summary": "很长的画像" * 200,
                "topics": ["t"] * 50,
            }],
        )
        videos = self._videos(30, title_len=80)
        with self.assertRaises(llm_analyzer.ContextBatchNeedsSplit):
            analyzer._prepare_call(videos)

    def test_prepare_call_refuses_single_item_that_cannot_fit(self):
        analyzer = llm_analyzer.LLMAnalyzer(
            self._config(context_window_tokens=4096),
            folders=["夹A"],
            folder_profiles=[{
                "id": "10", "name": "夹A",
                "summary": "很长的画像" * 400,
            }],
        )
        with self.assertRaises(llm_analyzer.ContextBudgetError) as ctx:
            analyzer._prepare_call(self._videos(1, title_len=120))
        self.assertNotIsInstance(ctx.exception, llm_analyzer.ContextBatchNeedsSplit)

    def test_analyze_batch_splits_and_merges_when_needs_split(self):
        analyzer = llm_analyzer.LLMAnalyzer(self._config(), folders=["夹A"])
        videos = self._videos(4)
        calls = []

        def fake_prepare(batch):
            calls.append(len(batch))
            if len(batch) > 1:
                raise llm_analyzer.ContextBatchNeedsSplit(1, 1, 1)
            return {"packed_videos": [], "system": "s", "user_text": "u",
                    "max_tokens": 16, "prompt_tokens": 1, "safety_tokens": 1}

        def fake_call(batch, prepared=None):
            return ({v["bvid"]: {"ok": True} for v in batch}, [], "ok")

        with patch.object(analyzer, "_prepare_call", side_effect=fake_prepare), \
             patch.object(analyzer, "_call", side_effect=fake_call):
            got = analyzer.analyze_batch(videos)

        self.assertEqual(4, len(got))
        self.assertTrue(all(got[f"BV{i}"]["ok"] for i in range(1, 5)))
        # 至少拆过分
        self.assertTrue(any(n < 4 for n in calls))

    def test_truncated_response_triggers_split_not_adoption(self):
        analyzer = llm_analyzer.LLMAnalyzer(self._config(), folders=["夹A"])
        videos = self._videos(2)

        def fake_call(batch, prepared=None):
            if len(batch) > 1:
                return {}, list(batch), "truncated"
            return ({batch[0]["bvid"]: {"ok": True}}, [], "ok")

        with patch.object(analyzer, "_prepare_call",
                          return_value={"packed_videos": [], "system": "s",
                                        "user_text": "u", "max_tokens": 16,
                                        "prompt_tokens": 1, "safety_tokens": 1}), \
             patch.object(analyzer, "_call", side_effect=fake_call):
            got = analyzer.analyze_batch(videos)

        self.assertEqual(2, len(got))
        self.assertEqual({"ok": True}, got["BV1"])
        self.assertEqual({"ok": True}, got["BV2"])

    def test_length_finish_reason_is_reported_as_truncated(self):
        analyzer = llm_analyzer.LLMAnalyzer(self._config(), folders=["夹A"])
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]
        }
        session = MagicMock()
        session.post.return_value = resp
        prepared = {"packed_videos": [], "system": "s", "user_text": "u",
                    "max_tokens": 16, "prompt_tokens": 1, "safety_tokens": 1}
        with patch.object(analyzer, "_session", return_value=session):
            got, missing, reason = analyzer._call(self._videos(2), prepared)
        self.assertEqual("truncated", reason)
        self.assertEqual({}, got)
        self.assertEqual(2, len(missing))


if __name__ == "__main__":
    unittest.main()
