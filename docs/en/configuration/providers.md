# Providers and Models

Pythinker Code supports multiple LLM platforms, which can be configured via configuration files or the `/login` command.

## Platform selection

The easiest way to configure is to run the `/login` command (alias `/setup`) in shell mode and follow the wizard to select platform and model:

1. Select an API platform
2. Enter your API key
3. Select a model from the available list

After configuration, Pythinker Code will automatically save settings to `~/.pythinker/config.toml` and reload.

`/login` currently supports the following platforms:

| Platform | Description |
| --- | --- |
| Pythinker | Pythinker platform, supports search and fetch services |
| OpenAI API | Official OpenAI API |
| OpenAI ChatGPT Codex | OpenAI managed account login |
| Pythinker AI Open Platform (pythinker-ai.cn) | China region API endpoint |
| Pythinker AI Open Platform (pythinker-ai.ai) | Global region API endpoint |
| Z.AI Coding Plan | Subscription route at `api.z.ai/api/coding/paas/v4` |
| Z.AI API | Pay-as-you-go route at `api.z.ai/api/paas/v4` |
| LM Studio | Local models served via LM Studio |
| Ollama | Local models served via Ollama |

For other platforms, please manually edit the configuration file.

## Provider types

The `type` field in `providers` configuration specifies the API provider type. Different types use different API protocols and client implementations.

| Type | Description |
| --- | --- |
| `pythinker` | Pythinker API |
| `openai_legacy` | OpenAI Chat Completions API |
| `openai_responses` | OpenAI Responses API |
| `openai_codex` | OpenAI Responses API with managed account login (configured via `/login`, not by hand) |
| `anthropic` | Anthropic Claude API |
| `gemini` | Google Gemini API |
| `vertexai` | Google Vertex AI |

All provider types support adding custom HTTP headers via the `custom_headers` field. See [Configuration files](./config-files.md) for details.

### `pythinker`

For connecting to Pythinker API, including Pythinker and Pythinker AI Open Platform.

```toml
[providers.pythinker-for-coding]
type = "pythinker"
base_url = "https://api.pythinker.com/coding/v1"
api_key = "sk-xxx"
```

### `openai_legacy`

For platforms compatible with OpenAI Chat Completions API, including the official OpenAI API and various compatible services.

```toml
[providers.openai]
type = "openai_legacy"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### Managed Z.AI routes

Z.AI Coding Plan and Z.AI API are independent managed routes. Configure the route that owns
your key; Pythinker does not infer a route from the credential, migrate credentials between
routes, or retry a request against the other endpoint.

| Route | Login | Provider key | Model prefix | Base URL | Environment variable |
| --- | --- | --- | --- | --- | --- |
| Coding Plan | `pythinker login --z-ai-coding` | `managed:z-ai-coding` | `z-ai-coding/` | `https://api.z.ai/api/coding/paas/v4` | `ZAI_CODING_API_KEY` |
| API | `pythinker login --z-ai-api` | `managed:z-ai-api` | `z-ai-api/` | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY` |

The same routes are available in the interactive selector as `/login z-ai-coding` and
`/login z-ai-api`. They may coexist in one config; login, catalog refresh, logout, default-model
repair, and cached rate-limit headers remain scoped to the selected route. `/usage` shows a
route-specific note because Z.AI does not document a route-wide usage endpoint; after a chat
request, captured rate-limit headers are displayed for that route when available.

Pythinker applies a provider compatibility profile to its curated GLM catalog:

| Model | Context tokens | Maximum output tokens | Thinking | Streamed tool calls |
| --- | ---: | ---: | --- | --- |
| `glm-5.2` | 1,000,000 | 131,072 | Tiered (`high` / `max`) | Yes |
| `glm-5.1` | 204,800 | 131,072 | Binary | Yes |
| `glm-5` | 204,800 | 131,072 | Binary | Yes |
| `glm-5-turbo` | 204,800 | 131,072 | Binary | Yes |
| `glm-4.7` | 204,800 | 131,072 | Binary | Yes |
| `glm-4.5-air` | 131,072 | 98,304 | Binary | No |

On these OpenAI-compatible routes, the full-context model id is plain `glm-5.2`;
`glm-5.2[1m]` is not an alias. Unknown Z.AI models keep conservative request defaults until
they are curated. Z.AI reasoning replay uses
only reasoning content the provider returned; Pythinker does not synthesize missing reasoning.

### `openai_responses`

For OpenAI Responses API (newer API format).

```toml
[providers.openai-responses]
type = "openai_responses"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### `anthropic`

For connecting to Anthropic Claude API.

```toml
[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key = "sk-ant-xxx"
```

### `gemini`

For connecting to Google Gemini API.

```toml
[providers.gemini]
type = "gemini"
base_url = "https://generativelanguage.googleapis.com"
api_key = "xxx"
```

### `vertexai`

For connecting to Google Vertex AI. Requires setting necessary environment variables via the `env` field.

```toml
[providers.vertexai]
type = "vertexai"
base_url = "https://xxx-aiplatform.googleapis.com"
api_key = ""
env = { GOOGLE_CLOUD_PROJECT = "your-project-id" }
```

## Model capabilities

The `capabilities` field in model configuration declares the capabilities supported by the model. This affects feature availability in Pythinker Code.

| Capability | Description |
| --- | --- |
| `thinking` | Supports thinking mode (deep reasoning), can be toggled |
| `always_thinking` | Always uses thinking mode (cannot be disabled) |
| `image_in` | Supports image input |
| `video_in` | Supports video input |

```toml
[models.gemini-3-pro-preview]
provider = "gemini"
model = "gemini-3-pro-preview"
max_context_size = 262144
capabilities = ["thinking", "image_in"]
```

### `thinking`

Declares that the model supports thinking mode. When enabled, the model performs deeper reasoning before answering, suitable for complex problems. In shell mode, you can use the `/model` command to switch models and thinking mode, or control it at startup with `--thinking` / `--no-thinking` flags.

### `always_thinking`

Indicates the model always uses thinking mode and cannot be disabled. For example, models with "thinking" in their name like `pythinker-ai-thinking` typically have this capability. When using such models, the `/model` command won't prompt for thinking mode toggle.

### `image_in`

When image input capability is enabled, you can paste images in conversations (`Ctrl-V`).

### `video_in`

When video input capability is enabled, you can send video content in conversations.

## Search and fetch services

The `SearchWeb` and `FetchURL` tools depend on external services, currently only provided by the Pythinker platform.

When selecting the Pythinker platform using `/login`, search and fetch services are automatically configured.

| Service | Corresponding tool | Behavior when not configured |
| --- | --- | --- |
| `pythinker_ai_search` | `SearchWeb` | Tool unavailable |
| `pythinker_ai_fetch` | `FetchURL` | Falls back to local fetching |

When using other platforms, the `FetchURL` tool is still available but will fall back to local fetching.
