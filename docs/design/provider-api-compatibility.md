# Provider API compatibility and model limits

Audit date: 2026-10-07

The application currently implements OpenAI-compatible Chat Completions. It does
not implement the Anthropic Messages API or provider-specific non-chat APIs.

## Chat request compatibility

Every analysis path uses the same request builder. It identifies the provider from
the endpoint first, then the saved provider name, and uses the model ID to select
model-family behavior. A synced model's explicit capability metadata overrides
the built-in rules. JSON mode is only sent when the provider/model rule or the
catalog says `response_format` is supported.

| Provider / endpoint | Output limit field | JSON and reasoning behavior |
| --- | --- | --- |
| SiliconFlow | `max_tokens` | Sends JSON mode for chat models. Uses `enable_thinking` for Qwen3, DeepSeek V4, and supported GLM models; normalizes model-specific `reasoning_effort`. |
| Qwen Model Studio / Token Plan | `max_tokens` | Sends JSON mode for supported Qwen, DeepSeek, GLM, Kimi, and StepFun families. Qwen reasoning models use `enable_thinking`; thinking-only model IDs omit a conflicting `false` toggle and JSON mode. |
| DeepSeek | `max_tokens` | Sends JSON mode. DeepSeek V4 / `deepseek-flash` uses the DeepSeek `thinking.type` object. The documented request ceiling is 393,216 tokens. |
| MiMo | `max_completion_tokens` | Sends JSON mode and `thinking.type`. Thinking-mode requests omit `temperature` and `top_p`. |
| DigitalOcean Serverless Inference | `max_tokens` by default | Uses `supported_parameters` from the model catalog for output-field selection, JSON mode, and reasoning controls. Does not guess JSON support when metadata is missing. |
| AMD Radeon Cloud Token Factory | `max_tokens` by default | Uses model `supported_parameters`, `json_output` / `structured_outputs`, and reasoning levels when present; maps thinking controls to `reasoning_effort`. |
| OpenAI | `max_completion_tokens` for o-series and GPT-5 / GPT-6; otherwise `max_tokens` | Sends JSON mode for chat models. Sends `reasoning_effort` only for reasoning families or when model metadata explicitly lists it; maps “off” to `none` only for models that support it. |
| Gemini OpenAI compatibility | `max_tokens` | Sends JSON mode for Gemini chat models and retains `reasoning_effort` for Gemini 2.5 / 3 families. Maps “off” to `none` only for Gemini 2.5 models that can disable thinking. |
| Moonshot / Kimi | `max_tokens` | Sends JSON mode for Kimi chat models. Kimi K2.6 uses `thinking.type`; K2.7-Code is treated as always thinking; K3 uses `reasoning_effort`. |
| OpenRouter and other OpenAI-compatible gateways | `max_tokens` unless model metadata says otherwise | Uses catalog capabilities such as `supported_parameters` when available. Unknown models use the conservative OpenAI-compatible baseline and receive JSON instructions in the prompt without an assumed JSON-mode parameter. |
| Other custom endpoints | `max_tokens` | Must implement OpenAI-compatible Chat Completions and `/models`. Nonstandard thinking fields are omitted unless the model catalog lists them. |

The app's current provider transport is OpenAI-compatible Chat Completions. It
does not implement native Anthropic Messages or other non-Chat-Completions
transports. Mainstream OpenAI-compatible endpoints such as Groq, Together,
Fireworks, Cerebras, Mistral, xAI, Perplexity, NVIDIA NIM, and Hugging Face use
the same baseline unless their model catalog advertises additional capabilities.

The model-family table covers current request-relevant families, including
DeepSeek V4 / Flash and R1 / Reasoner, Qwen3 / QwQ, GLM, Kimi, StepFun, MiMo,
Gemini 2.5 / 3, and OpenAI o-series / GPT-5 / GPT-6. Models not recognized by family
name still use provider metadata where available and then the conservative
baseline.

Known provider hard caps are applied before a request is sent. They are distinct
from the model's context window and from the model metadata returned by a catalog.

## Model limit metadata

The model-list API is not a universal capability API. Most OpenAI-compatible
`/models` endpoints return IDs/ownership only. The application now records the
source of each context/output limit and leaves newly added limits unknown when
the remote API does not provide them. The model list and edit form show whether
a value came from the API, a manual entry, a documented hard cap, an older
configuration, or has no known source. Existing rows retain their prior numeric
values but are marked `legacy_unknown`; those values are not re-labeled as API
data.

| Provider API | Context window | Maximum output | Fetch behavior |
| --- | --- | --- | --- |
| MiMo `/v1/models` | Not returned | Not returned | Keep as unknown unless manually entered. The documented chat request does specify `max_completion_tokens`; the model catalog documents output caps, but not in the `/models` response. |
| DeepSeek `/models` | Not returned | Not returned | Keep as unknown unless manually entered. Apply the documented global `max_tokens` ceiling of 393,216. |
| SiliconFlow `/v1/models` | Not guaranteed by the documented Chat Completions API | Not guaranteed | Store only fields actually present in the model-list response; otherwise leave unknown. |
| DigitalOcean `/v1/models` + `/v2/gen-ai/models/catalog` | Catalog returns `context_window` | Catalog returns `max_output_tokens` | Fetch the catalog pages and match entries to inference model IDs. If the control-plane catalog cannot be read, show the limits as unavailable. |
| AMD Radeon Cloud `/v1/models` | Returns `context_length` | Not listed in the documented catalog schema | Save context from API and leave output unknown unless another source supplies it. |
| Alibaba Model Studio workspace catalog `/api/v1/models` | `model_info.context_window` | `model_info.max_output_tokens` | Fetch pages only for a workspace/region endpoint that exposes this catalog. Generic and Token Plan endpoints do not imply that this metadata endpoint is available. |

`context_tokens` and `max_output_tokens` displayed as unknown are not silently
replaced with fabricated model specifications. During inference, the application
may still use the global legacy fallback where necessary; the UI keeps that
separate from verified model metadata. Manual entries are tagged as manual.

## Reference documentation

- [MiMo Chat Completions](https://mimo.mi.com/docs/en-US/api/chat/openai-api)
- [MiMo Structured Output](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/text-generation/structured-output)
- [MiMo List Models](https://mimo.mi.com/docs/en-US/api/model/list-models)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek List Models](https://api-docs.deepseek.com/api/list-models/)
- [SiliconFlow Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post)
- [SiliconFlow JSON Mode](https://docs.siliconflow.cn/docs/userguide/guides/json-mode)
- [Alibaba Model Studio Structured Output](https://help.aliyun.com/en/model-studio/qwen-structured-output)
- [DigitalOcean Serverless Inference](https://docs.digitalocean.com/reference/api/reference/serverless-inference/)
- [DigitalOcean model catalog](https://docs.digitalocean.com/reference/pydo/reference/genai/list_model_catalog/)
- [AMD Radeon Cloud Chat Completions](https://amd-aim.github.io/radeon-cloud-docs/api/chat-completions/)
- [AMD Radeon Cloud List Models](https://amd-aim.github.io/radeon-cloud-docs/api/models/)
- [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat/create)
- [Gemini OpenAI Compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Moonshot Chat Completions and JSON Mode](https://platform.moonshot.ai/docs/api/chat)
- [Alibaba Cloud Model Studio List Models](https://help.aliyun.com/zh/model-studio/list-models)
