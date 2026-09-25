# Provider API compatibility and model limits

Audit date: 2026-09-25

The application currently implements OpenAI-compatible Chat Completions. It does
not implement the Anthropic Messages API or provider-specific non-chat APIs.

## Chat request compatibility

| Provider | Completion limit parameter | Notes |
| --- | --- | --- |
| SiliconFlow | `max_tokens` | Supports JSON response format and selected reasoning controls; parameters such as `reasoning_effort` are model-specific. |
| Qwen Model Studio / Token Plan | `max_tokens` | OpenAI-compatible Chat Completions. The Token Plan URL and key must belong to the same plan. |
| DeepSeek | `max_tokens` | The documented request ceiling is 393,216 tokens; the total input plus output must still fit the selected model context. |
| MiMo | `max_completion_tokens` | `thinking.type` controls thinking. In thinking mode, temperature and top_p are not configurable for the listed MiMo models. The request builder translates the application settings to this schema. |
| DigitalOcean Serverless Inference | `max_tokens` | OpenAI-compatible inference endpoint; its ordinary `/v1/models` list does not contain the catalog limits. |
| AMD Radeon Cloud Token Factory | `max_tokens` | The configured Radeon Cloud endpoint documents `max_tokens`, `reasoning_effort`, and OpenAI-compatible Chat Completions. |
| Other | `max_tokens` | Custom endpoints must implement the OpenAI-compatible Chat Completions request and `/models` list used by the app. |

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
- [MiMo List Models](https://mimo.mi.com/docs/en-US/api/model/list-models)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [DeepSeek List Models](https://api-docs.deepseek.com/api/list-models/)
- [SiliconFlow Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post)
- [DigitalOcean Serverless Inference](https://docs.digitalocean.com/reference/api/reference/serverless-inference/)
- [DigitalOcean model catalog](https://docs.digitalocean.com/reference/pydo/reference/genai/list_model_catalog/)
- [AMD Radeon Cloud Chat Completions](https://amd-aim.github.io/radeon-cloud-docs/api/chat-completions/)
- [AMD Radeon Cloud List Models](https://amd-aim.github.io/radeon-cloud-docs/api/models/)
- [Alibaba Cloud Model Studio List Models](https://help.aliyun.com/zh/model-studio/list-models)
