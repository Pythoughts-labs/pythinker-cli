# Environment Variables

Pythinker Code supports overriding configuration or controlling runtime behavior through environment variables. This page lists all supported environment variables.

For detailed information on how environment variables override configuration files, see [Config Overrides](./overrides.md).

## Pythinker environment variables

The following environment variables take effect when using `pythinker` type providers, used to override provider and model configuration.

| Environment Variable | Description |
| --- | --- |
| `PYTHINKER_BASE_URL` | API base URL |
| `PYTHINKER_API_KEY` | API key |
| `PYTHINKER_MODEL_NAME` | Model identifier |
| `PYTHINKER_MODEL_MAX_CONTEXT_SIZE` | Maximum context length (in tokens) |
| `PYTHINKER_MODEL_CAPABILITIES` | Model capabilities, comma-separated (e.g., `thinking,image_in`) |
| `PYTHINKER_MODEL_TEMPERATURE` | Generation parameter `temperature` |
| `PYTHINKER_MODEL_TOP_P` | Generation parameter `top_p` |
| `PYTHINKER_MODEL_MAX_TOKENS` | Generation parameter `max_tokens` |
| `PYTHINKER_MODEL_THINKING_KEEP` | Pythinker AI `thinking.keep` switch for preserved thinking (only applied when thinking mode is active) |

### `PYTHINKER_BASE_URL`

Overrides the provider's `base_url` field in the configuration file.

```sh
export PYTHINKER_BASE_URL="https://api.pythinker-ai.cn/v1"
```

### `PYTHINKER_API_KEY`

Overrides the provider's `api_key` field in the configuration file. Used to inject API keys without modifying the configuration file, suitable for CI/CD environments.

```sh
export PYTHINKER_API_KEY="sk-xxx"
```

### `PYTHINKER_MODEL_NAME`

Overrides the model's `model` field in the configuration file (the model identifier used in API calls).

```sh
export PYTHINKER_MODEL_NAME="pythinker-ai-thinking"
```

### `PYTHINKER_MODEL_MAX_CONTEXT_SIZE`

Overrides the model's `max_context_size` field in the configuration file. Must be a positive integer.

```sh
export PYTHINKER_MODEL_MAX_CONTEXT_SIZE="262144"
```

### `PYTHINKER_MODEL_CAPABILITIES`

Overrides the model's `capabilities` field in the configuration file. Multiple capabilities are comma-separated, supported values are `thinking`, `always_thinking`, `image_in`, and `video_in`.

```sh
export PYTHINKER_MODEL_CAPABILITIES="thinking,image_in"
```

### `PYTHINKER_MODEL_TEMPERATURE`

Sets the generation parameter `temperature`, controlling output randomness. Higher values produce more random output, lower values produce more deterministic output.

```sh
export PYTHINKER_MODEL_TEMPERATURE="0.7"
```

### `PYTHINKER_MODEL_TOP_P`

Sets the generation parameter `top_p` (nucleus sampling), controlling output diversity.

```sh
export PYTHINKER_MODEL_TOP_P="0.9"
```

### `PYTHINKER_MODEL_MAX_TOKENS`

Sets the generation parameter `max_tokens`, limiting the maximum tokens per response.

```sh
export PYTHINKER_MODEL_MAX_TOKENS="4096"
```

### `PYTHINKER_MODEL_THINKING_KEEP`

Forwards the value verbatim to the Pythinker AI API as `thinking.keep`, enabling Preserved Thinking (see the [Pythinker AI docs](https://platform.pythinker.com/docs/guide/use-pythinker-ai-thinking-model#preserved-thinking)). Setting it to `all` causes the provider to preserve the reasoning content of previous assistant turns across requests. The value is passed through unchanged, no validation or case normalization is performed.

```sh
export PYTHINKER_MODEL_THINKING_KEEP="all"
```

Empty string or unset means the field is omitted from the request (current default behavior). The override only applies when the model is actually in thinking mode; it is ignored for non-thinking runs so the API never receives a `thinking.keep` without the companion `thinking.type`.

This parameter only takes effect on Pythinker AI models that support Preserved Thinking (e.g., `pythinker-ai` / `pythinker-ai-thinking`). Passing it to other models has no effect or may be rejected by the API; the CLI does not validate the model.

::: warning Cost
`thinking.keep=all` instructs the API to retain historical reasoning content across turns, which increases input tokens and therefore API cost. Only enable it when the preserved thinking behavior is required.
:::

## OpenAI-compatible environment variables

The following environment variables take effect when using `openai_legacy`, `openai_responses`, or `openai_codex` type providers.

| Environment Variable | Description |
| --- | --- |
| `OPENAI_BASE_URL` | API base URL |
| `OPENAI_API_KEY` | API key |
| `OPENAI_ADMIN_KEY` | OpenAI Admin API key for usage and cost data |

### `OPENAI_BASE_URL`

Overrides the provider's `base_url` field in the configuration file.

```sh
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

### `OPENAI_API_KEY`

Overrides the provider's `api_key` field in the configuration file.

```sh
export OPENAI_API_KEY="sk-xxx"
```

### `OPENAI_ADMIN_KEY`

Provides an OpenAI Admin API key for `/usage` cost data. If unset, `/usage` falls back to rate-limit header data when available.

```sh
export OPENAI_ADMIN_KEY="sk-admin-xxx"
```

## Z.AI environment variables

Z.AI Coding Plan and Z.AI API use separate credential variables because they are independent
routes:

| Environment Variable | Route | Base URL |
| --- | --- | --- |
| `ZAI_CODING_API_KEY` | Z.AI Coding Plan | `https://api.z.ai/api/coding/paas/v4` |
| `ZAI_API_KEY` | Z.AI API | `https://api.z.ai/api/paas/v4` |

```sh
export ZAI_CODING_API_KEY="your-coding-plan-key"
pythinker login --z-ai-coding

export ZAI_API_KEY="your-api-key"
pythinker login --z-ai-api
```

Each login reads only its route's variable. Pythinker does not infer the route from the key,
fall back to the other variable, migrate credentials, or retry requests across routes. Both
variables may be set when both routes are configured.

## Other environment variables

| Environment Variable | Description |
| --- | --- |
| `PYTHINKER_SHARE_DIR` | Customize the share directory path (default: `~/.pythinker`) |
| `PYTHINKER_CLI_NO_AUTO_UPDATE` | Hard kill-switch: disable silent auto-install, update checks, and startup update notices |
| `PYTHINKER_AUTO_UPDATE` | Toggle silent startup auto-update (`auto_update` config field); the hard kill-switch `PYTHINKER_CLI_NO_AUTO_UPDATE` overrides it |
| `PYTHINKER_CLI_PASTE_CHAR_THRESHOLD` | Character threshold for folding pasted text (default: `200`) |
| `PYTHINKER_CLI_PASTE_LINE_THRESHOLD` | Line threshold for folding pasted text (default: `5`) |

### `PYTHINKER_SHARE_DIR`

Customize the share directory path for Pythinker Code. The default path is `~/.pythinker`, where configuration, sessions, logs, and other runtime data are stored.

```sh
export PYTHINKER_SHARE_DIR="/path/to/custom/pythinker"
```

See [Data Locations](./data-locations.md) for details.

::: warning Note
`PYTHINKER_SHARE_DIR` does not affect [Agent Skills](../customization/skills.md) search paths. Skills are cross-tool shared capability extensions (compatible with Claude, Codex, etc.), which is a different type of data from application runtime data. To override Skills paths, use the `--skills-dir` flag.
:::

### `PYTHINKER_CLI_NO_AUTO_UPDATE`

When set to `1`, `true`, `t`, `yes`, or `y` (case-insensitive), disables proactive update checks and startup update notices. Pythinker startup remains non-blocking; run `pythinker update` or `/update` when you want to check manually.

```sh
export PYTHINKER_CLI_NO_AUTO_UPDATE="1"
```

::: tip
If you installed Pythinker Code via Nix or other package managers, this environment variable is typically set automatically since updates are handled by the package manager.
:::

### `PYTHINKER_AUTO_UPDATE`

Set to `0`/`false`/`no` to disable silent startup auto-updates, or `1`/`true`/`yes`
to enable them (default). This flips the `auto_update` config field.

```sh
export PYTHINKER_AUTO_UPDATE="false"
```

::: warning Hard kill-switch wins
`PYTHINKER_CLI_NO_AUTO_UPDATE` takes precedence: when it is set, `PYTHINKER_AUTO_UPDATE=1`
cannot re-enable updates, and Pythinker shows no update activity at all.
:::

#### Per-channel behavior

When enabled and a newer installable release exists, Pythinker installs it in a
background task and shows a one-line `Updated X → Y. Restart Pythinker to apply.`
notice — the running session continues on the old version until you restart.

- **Windows** (native installer / pip): the process exits so the installer can
  replace the binary.
- **Managed channels** (Docker/Nix/Scoop/WinGet): no binary swap — Pythinker
  shows a channel-native upgrade hint instead.
- **Source checkouts**: never auto-update.

### `PYTHINKER_CLI_PASTE_CHAR_THRESHOLD`

In Agent mode, when pasted text exceeds this character count, it is folded into a placeholder (e.g., `[Pasted text #1 +10 lines]`) and expanded to full content on submit. Default: `200`.

```sh
export PYTHINKER_CLI_PASTE_CHAR_THRESHOLD="200"
```

### `PYTHINKER_CLI_PASTE_LINE_THRESHOLD`

In Agent mode, when pasted text reaches this line count, it is folded into a placeholder. Default: `5`.

```sh
export PYTHINKER_CLI_PASTE_LINE_THRESHOLD="5"
```

::: tip
Some terminals (e.g., XShell over SSH) may break CJK input methods (Chinese/Japanese/Korean IME) after pasting multiline text. Symptoms include the IME candidate window not appearing or input becoming unresponsive until Ctrl+C is pressed.

This happens because multiline text in the input buffer can confuse the terminal's cursor position tracking, which affects IME composition window placement. You can work around this by lowering the line threshold to fold multiline pastes into single-line placeholders:

```sh
export PYTHINKER_CLI_PASTE_LINE_THRESHOLD="2"
```

With this setting, any paste containing a newline will be automatically folded, preventing multiline text from entering the input buffer. Single-line pastes (URLs, short commands, etc.) are not affected.

Note: The two thresholds use OR logic (character count **or** line count), so lowering only the line threshold is sufficient. Avoid setting the character threshold to a very small value (e.g., `1`), as that would fold all non-empty pastes including single-line short text.
:::
