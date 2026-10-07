import unittest

import llm_analyzer


class ChatPayloadAdapterTests(unittest.TestCase):
    def payload(self, base_url, model, *, provider_name="", params=None,
                capabilities=None, structured=True, max_tokens=256):
        config = llm_analyzer.LLMConfig(
            base_url=base_url,
            model=model,
            provider_name=provider_name,
            params=params or {},
            capabilities=capabilities or {},
            max_tokens=max_tokens,
        )
        return llm_analyzer.build_chat_completion_payload(
            config, [{"role": "user", "content": "Return JSON."}], max_tokens,
            temperature=0.2, structured_output=structured)

    def test_all_current_provider_presets_are_identified(self):
        cases = (
            ("https://api.siliconflow.cn/v1", "SiliconFlow", "siliconflow"),
            ("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "千问 Token Plan", "dashscope"),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "千问按量付费", "dashscope"),
            ("https://api.deepseek.com/v1", "DeepSeek", "deepseek"),
            ("https://token-plan-cn.xiaomimimo.com/v1", "MiMo Token Plan", "mimo"),
            ("https://api.xiaomimimo.com/v1", "MiMo 按量付费", "mimo"),
            ("https://inference.do-ai.run/v1", "Digital Ocean", "digitalocean"),
            ("https://developer.amd.com.cn/radeon/api/v1", "AMD Token Factory", "amd"),
        )
        for base_url, provider_name, expected in cases:
            with self.subTest(provider_name=provider_name):
                config = llm_analyzer.LLMConfig(base_url=base_url,
                                                provider_name=provider_name)
                self.assertEqual(llm_analyzer.request_provider(config), expected)

    def test_response_content_normalizes_text_blocks_before_json_parsing(self):
        content = llm_analyzer.extract_chat_completion_content({
            "choices": [{"message": {"content": [
                {"type": "text", "text": '{"ok":'},
                {"type": "text", "text": "true}"},
            ]}}]})
        self.assertEqual(content, '{"ok":\ntrue}')
        self.assertEqual(llm_analyzer.extract_json_object(content), {"ok": True})

    def test_deepseek_flash_uses_direct_deepseek_dialect(self):
        payload = self.payload("https://api.deepseek.com/v1", "deepseek-flash")
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("enable_thinking", payload)

    def test_siliconflow_deepseek_v4_uses_provider_toggle(self):
        for model in (
            "deepseek-ai/DeepSeek-V4-Flash",
            "deepseek-v4-flash",
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "deepseek-v4.1-flash",
        ):
            with self.subTest(model=model):
                payload = self.payload("https://api.siliconflow.cn/v1", model)
                self.assertEqual(payload["response_format"], {"type": "json_object"})
                self.assertIs(payload["enable_thinking"], False)
                self.assertNotIn("thinking", payload)

    def test_siliconflow_glm_effort_is_limited_to_documented_model(self):
        model_52 = self.payload(
            "https://api.siliconflow.cn/v1", "Pro/zai-org/GLM-5.2",
            params={"reasoning_effort": "medium"})
        self.assertEqual(model_52["reasoning_effort"], "high")
        model_51 = self.payload(
            "https://api.siliconflow.cn/v1", "Pro/zai-org/GLM-5.1",
            params={"reasoning_effort": "high"})
        self.assertNotIn("reasoning_effort", model_51)

    def test_dashscope_qwen3_uses_non_thinking_json_mode(self):
        payload = self.payload(
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "qwen3-8b", provider_name="千问 Token Plan")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIs(payload["enable_thinking"], False)

    def test_dashscope_thinking_only_qwen_omits_incompatible_json_controls(self):
        payload = self.payload(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen3-235b-a22b-thinking-2507",
            params={"enable_thinking": False})
        self.assertNotIn("response_format", payload)
        self.assertNotIn("enable_thinking", payload)

    def test_mimo_uses_json_mode_completion_tokens_and_thinking_object(self):
        payload = self.payload("https://api.xiaomimimo.com/v1", "mimo-v2.6-pro")
        self.assertEqual(payload["max_completion_tokens"], 256)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)

    def test_amd_model_catalog_controls_json_and_reasoning(self):
        payload = self.payload(
            "https://developer.amd.com.cn/radeon/api/v1", "qwen3.8-flash-next",
            params={"enable_thinking": False},
            capabilities={
                "supported_parameters": ["max_tokens", "response_format", "reasoning_effort"],
                "json_output": True,
                "reasoning_effort_levels": ["none", "low", "medium", "high"],
            })
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertNotIn("enable_thinking", payload)

    def test_model_catalog_can_disable_json_even_for_known_provider(self):
        payload = self.payload(
            "https://developer.amd.com.cn/radeon/api/v1", "deepseek-v4-flash",
            capabilities={"supported_parameters": ["max_tokens"], "json_output": False})
        self.assertNotIn("response_format", payload)

    def test_explicit_json_capability_overrides_incomplete_parameter_list(self):
        payload = self.payload(
            "https://developer.amd.com.cn/radeon/api/v1", "qwen3.8-27b",
            capabilities={"supported_parameters": ["max_tokens"], "json_output": True})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_digitalocean_uses_exact_catalog_fields_and_falls_back_without_metadata(self):
        known = self.payload(
            "https://inference.do-ai.run/v1", "deepseek-v4-flash",
            capabilities={"supported_parameters": [
                "max_completion_tokens", "response_format", "reasoning_effort"]})
        self.assertIn("max_completion_tokens", known)
        self.assertEqual(known["response_format"], {"type": "json_object"})
        self.assertNotIn("enable_thinking", known)

        unknown = self.payload("https://inference.do-ai.run/v1", "llama-3.3-70b")
        self.assertIn("max_tokens", unknown)
        self.assertNotIn("response_format", unknown)

    def test_openai_reasoning_model_uses_completion_tokens_and_json(self):
        payload = self.payload(
            "https://api.openai.com/v1", "gpt-5.1",
            params={"enable_thinking": True, "reasoning_effort": "high"})
        self.assertIn("max_completion_tokens", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("enable_thinking", payload)

        current = self.payload("https://api.openai.com/v1", "gpt-6.1-sol",
                               params={"reasoning_effort": "max"})
        self.assertIn("max_completion_tokens", current)
        self.assertEqual(current["reasoning_effort"], "max")

        can_disable = self.payload("https://api.openai.com/v1", "gpt-5.1",
                                   params={"enable_thinking": False})
        self.assertEqual(can_disable["reasoning_effort"], "none")
        cannot_disable = self.payload("https://api.openai.com/v1", "gpt-6.1-sol",
                                      params={"enable_thinking": False})
        self.assertNotIn("reasoning_effort", cannot_disable)

    def test_gemini_uses_openai_compatible_json_and_reasoning_fields(self):
        payload = self.payload(
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-3.8-flash", params={"enable_thinking": True,
                                         "reasoning_effort": "low"})
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("enable_thinking", payload)

        gemini25 = self.payload(
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-flash", params={"enable_thinking": False})
        self.assertEqual(gemini25["reasoning_effort"], "none")
        gemini25_pro = self.payload(
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-pro", params={"enable_thinking": False})
        self.assertNotIn("reasoning_effort", gemini25_pro)

    def test_moonshot_kimi_uses_json_and_model_specific_thinking_fields(self):
        k26 = self.payload("https://api.moonshot.ai/v1", "kimi-k2.6")
        self.assertEqual(k26["response_format"], {"type": "json_object"})
        self.assertEqual(k26["thinking"], {"type": "disabled"})

        k3 = self.payload("https://api.moonshot.ai/v1", "kimi-k3",
                          params={"enable_thinking": True,
                                  "reasoning_effort": "medium"})
        self.assertEqual(k3["reasoning_effort"], "high")
        self.assertNotIn("enable_thinking", k3)
        self.assertNotIn("thinking", k3)

        k27_code = self.payload("https://api.moonshot.ai/v1", "kimi-k2.7-code",
                                params={"enable_thinking": False})
        self.assertNotIn("enable_thinking", k27_code)
        self.assertNotIn("thinking", k27_code)

    def test_custom_openai_compatible_endpoint_uses_conservative_baseline(self):
        payload = self.payload(
            "https://llm.example.test/v1", "some-chat-model",
            provider_name="OpenAI-compatible custom gateway",
            params={"enable_thinking": True, "reasoning_effort": "high"})
        self.assertEqual(payload["max_tokens"], 256)
        self.assertNotIn("response_format", payload)
        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_non_chat_model_families_do_not_receive_json_mode(self):
        payload = self.payload("https://api.siliconflow.cn/v1", "Qwen-Embedding-V4")
        self.assertNotIn("response_format", payload)

    def test_custom_catalog_metadata_overrides_baseline(self):
        payload = self.payload(
            "https://gateway.example.test/v1", "vendor/model",
            params={"enable_thinking": True, "reasoning_effort": "high"},
            capabilities={"supported_parameters": ["max_completion_tokens",
                                                       "response_format",
                                                       "enable_thinking",
                                                       "reasoning_effort"]})
        self.assertIn("max_completion_tokens", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIs(payload["enable_thinking"], True)
        self.assertEqual(payload["reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
