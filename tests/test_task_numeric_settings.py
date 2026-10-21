"""Numeric settings reject ambiguous values and actually bound profile requests."""
import threading
import unittest
from unittest.mock import patch

import llm_analyzer as llm
import server
from pydantic import ValidationError


class TaskNumericSettingsTests(unittest.TestCase):
    def test_config_rejects_fraction_exponent_string_bool_and_out_of_range(self):
        fields = {"scan_interval": (2, 60), "write_interval": (1, 60),
                  "folder_merge_interval": (1, 60), "apply_batch": (1, 1000),
                  "analyze_batch": (1, 1000), "analyze_concurrency": (1, 4),
                  "profile_batch": (1, 1000), "profile_concurrency": (1, 4),
                  "profile_request_interval": (1, 60)}
        for field, (lo, hi) in fields.items():
            for value in (lo, hi):
                self.assertEqual(value, getattr(server.ConfigIn(**{field: value}), field))
            for value in (1.5, 2.0, "2", "1e2", True, lo - 1, hi + 1):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    server.ConfigIn(**{field: value})

    def test_new_profile_limits_are_saved(self):
        with patch.object(server, "load_config", return_value={}), \
             patch.object(server, "save_config") as save, \
             patch.object(server, "get_config", return_value={}):
            server.set_config(server.ConfigIn(profile_batch=75, profile_concurrency=3))
        self.assertEqual(75, save.call_args.args[0]["profile_batch"])
        self.assertEqual(3, save.call_args.args[0]["profile_concurrency"])

    def test_profile_batch_cap_applies_even_when_everything_fits_context(self):
        config = llm.LLMConfig(base_url="https://synthetic.invalid", api_key="", model="synthetic")
        sent = []
        def chat(config, system, user, *args):
            sent.append(user)
            if "items" in user:
                return {"themes": [], "notes": []}
            return {"summary": "合成画像", "topics": ["主题", "范围"], "typical_content": [], "out_of_scope": [], "coherence": "coherent", "confidence": 0.9}
        with patch.object(llm, "_profile_chat", side_effect=chat):
            result = llm.generate_folder_profile(config, {"title": "测试"},
                [{"title": str(i)} for i in range(5)], 5, batch_size=2)
        self.assertEqual([2, 2, 1], [len(u["items"]) for u in sent if "items" in u])
        self.assertEqual(5, result["sample_count"])
        self.assertEqual(5, sum(d["items_covered"] for d in sent[-1]["compressed_summaries"]))

    def test_parallel_profile_pages_run_together_and_keep_input_order(self):
        barrier = threading.Barrier(2)
        config = llm.LLMConfig(base_url="https://synthetic.invalid", api_key="", model="synthetic")
        def chat(config, system, user, *args):
            barrier.wait(timeout=5)
            return {"notes": [user["items"][0]["title"]]}
        with patch.object(llm, "_profile_chat", side_effect=chat):
            result = llm._compress_profile_pages(config, {"title": "测试"},
                [{"title": "第一"}, {"title": "第二"}], field="items", system="synthetic",
                output_tokens=256, input_budget=10000, context_window_tokens=32768,
                content_count=2, stage="test", request_interval=0, tpm_limit_tokens=20000,
                batch_size=1, concurrency=2)
        self.assertEqual(["第一", "第二"], [d["notes"][0] for d in result])


if __name__ == "__main__":
    unittest.main()
