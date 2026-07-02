<div align="center">

# <img src="https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/docs/media/logo.png" alt="Pythinker logo" width="42" align="top"> Pythinker Code

### *Think first, then code. Your terminal-native review-first AI engineering agent.*

**Code reviewer · Security & vulnerability scanner · Root-cause debugger — then code creator.**
**Pythinker reads your repo, audits it, and only writes code after the analysis. All from the shell you already live in.**

<br />

[![PyPI](https://img.shields.io/pypi/v/pythinker-code?style=for-the-badge&logo=pypi&logoColor=white&color=2563eb&label=pythinker-code&cacheSeconds=60)](https://pypi.org/project/pythinker-code/)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776ab?style=for-the-badge&logo=python&logoColor=white)](https://github.com/Pythoughts-labs/pythinker-code/blob/main/pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-16a34a.svg?style=for-the-badge)](https://github.com/Pythoughts-labs/pythinker-code/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/Pythoughts-labs/pythinker-code/ci-pythinker-cli.yml?branch=main&label=CI&style=for-the-badge&logo=githubactions&logoColor=white)](https://github.com/Pythoughts-labs/pythinker-code/actions/workflows/ci-pythinker-cli.yml?query=branch%3Amain)

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/pythinker-code?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/pythinker-code)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-f59e0b.svg?style=flat-square&logo=ruff&logoColor=white)](https://docs.astral.sh/ruff/)
[![ACP ready](https://img.shields.io/badge/ACP-ready-7c3aed.svg?style=flat-square)](https://github.com/agentclientprotocol/agent-client-protocol)
[![MCP tools](https://img.shields.io/badge/MCP-tools-0891b2.svg?style=flat-square)](https://modelcontextprotocol.io/)
[![Homepage](https://img.shields.io/badge/home-pythinker.com-ec4899.svg?style=flat-square)](https://pythinker.com)

<br />

<a href="https://pythinker.com">🌐 Website</a> &nbsp;·&nbsp;
<a href="#-quick-start">⚡ Quick Start</a> &nbsp;·&nbsp;
<a href="#-features">✨ Features</a> &nbsp;·&nbsp;
<a href="#-ide-integration-via-acp">🧩 IDE Integration</a> &nbsp;·&nbsp;
<a href="#-mcp-tooling">🔌 MCP</a> &nbsp;·&nbsp;
<a href="#-privacy--telemetry">🔐 Privacy</a> &nbsp;·&nbsp;
<a href="#-development">🛠️ Develop</a>

<br /><br />

<img src="https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/docs/media/pythinker_cli.gif" alt="Pythinker Code terminal demo" width="860">

</div>

---

## 💡 What is Pythinker?

**Pythinker Code** is an open-source, **review-first** AI engineering agent that lives in your terminal. Before it writes a single line, it reads yours — auditing diffs, scanning for vulnerabilities, and root-causing failures. Unlike chat assistants that jump straight to code, Pythinker leads with **code review, security scanning, and root-cause diagnosis**, and only edits files once the analysis points at a fix.

It ships with first-class subagents for each role — `code-reviewer` for severity-scored diff critique, `security-reviewer` for validated vulnerability findings, `debugger` for failure root-causing, and `coder`/`implementer` for the scoped edits that follow. All running in a single iterative loop, driven by the model of your choice, with full access to **your repo, the shell, the web, and MCP tools**.

It speaks the [**Agent Client Protocol (ACP)**](https://github.com/agentclientprotocol/agent-client-protocol), so it slots cleanly into ACP-aware editors like Zed and JetBrains. It loads [**Model Context Protocol (MCP)**](https://modelcontextprotocol.io/) servers, so the same tools your other agents use just work. And it's hackable: subagents, skills, hooks, and plugins are all first-class extension points.

> 🎯 **Review · Secure · Diagnose · then Create.** One agent, one shell, one workflow. No tab-switching. No context loss. No magic.

---

## 🆕 What's New in 0.56.0

- **Windows shell UI recovers from mid-session console blanking.** Child processes spawned by the Shell tool, background tasks, and `!` commands no longer attach to the interactive console, and the prompt renderer forces an absolute repaint after terminal resizes and failed scrollback handoffs instead of diffing against a stale frame.
- **Workflow progress rendering and lifecycle fixes.** Progress display no longer duplicates agents truncated by the per-phase display cap, leaked `agent()` coroutines are closed when `parallel()` rejects its arguments, and a 1000-agent lifetime backstop now guards against runaway workflow loops.
- **Stale update-success notice cleared after downgrading.** Restarting into an older Homebrew install no longer leaves a permanent "Restart to apply" banner.

Upgrade with `pythinker update`, `pip install --upgrade pythinker-code==0.56.0`, or use the native installer for your platform from the [Releases page](https://github.com/Pythoughts-labs/pythinker-code/releases/latest).


---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### 🖥️ Terminal-First

Plan, edit, run, and verify without leaving your shell. Every action is visible, scriptable, and auditable.

</td>
<td width="50%" valign="top">

### ⚡ Shell Command Mode

Press `Ctrl-X` to drop into a direct shell prompt inside the agent. Run commands, then snap back into AI mode with full context preserved.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧩 ACP IDE Integration

Run `pythinker acp` and any [Agent Client Protocol](https://github.com/agentclientprotocol/agent-client-protocol) editor — Zed, JetBrains, and more — gets a full Pythinker session inline.

</td>
<td width="50%" valign="top">

### 🔌 MCP Tool Loading

Manage stdio and HTTP MCP servers with `pythinker mcp`. OAuth-backed servers, persistent config, ad-hoc files — all supported.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🤖 Subagents & Skills

Delegate focused work to built-in subagents. Load reusable instructions via `/skill:<name>` and bundled prompt flows via `/flow:<name>`. Use `pythinker skill list`, `pythinker skill lock`, and `pythinker skill verify-lock` to inspect project skills and pin their hashes in `skills-lock.json`.

</td>
<td width="50%" valign="top">

### 🪝 Hooks & Plugins

Observe or block tool execution with hook events. Install community extensions with `pythinker plugin`.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🌐 Web & Visualization UIs

Optional web frontend and visualization frontend ship alongside the CLI for richer inspection workflows.

</td>
<td width="50%" valign="top">

### 🤖 Bring Your Own Model

Swap providers and models per-session: `--model openai/gpt-5.5`, hosted Pythinker models, or your own keys.

</td>
</tr>
</table>

> [!NOTE]
> Built-in shell commands such as `cd` are not yet supported in shell command mode.

<div align="center">
<img src="https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/docs/media/shell-mode.gif" alt="Shell command mode demo" width="860">
</div>

---

## ⚡ Quick Start

Pythinker ships **native installers for every platform**. Pick the row that
matches your OS — no Python, Node, or `uv` prerequisite.

| Platform | Recommended install | Artifact source |
|---|---|---|
| **🪟 Windows** | `irm https://pythinker.com/install.ps1 \| iex` | `PythinkerSetup-0.56.0.exe` from [Releases](https://github.com/Pythoughts-labs/pythinker-code/releases/latest) |
| **<img src="https://img.shields.io/badge/-macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS"> / <img src="https://img.shields.io/badge/-Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">** | `curl -fsSL https://pythinker.com/install.sh \| bash` | native tarball from [Releases](https://github.com/Pythoughts-labs/pythinker-code/releases/latest) |
| **<img src="https://img.shields.io/badge/-macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS"> — Homebrew** | `brew install Pythoughts-labs/pythinker/pythinker-code` | auto-published Homebrew tap |
| **🐳 Docker** | `docker run --rm -it ghcr.io/pythoughts-labs/pythinker-code` | GHCR multi-arch image |
| **🪟 Windows — Scoop** | `scoop bucket add pythinker https://github.com/Pythoughts-labs/scoop-pythinker && scoop install pythinker-code` | auto-published Scoop bucket |
| **❄️ Nix** | `nix run github:Pythoughts-labs/pythinker-code` | flake `apps.default` |
| **<img src="https://img.shields.io/badge/-Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux"> — system package** | Download the `.deb` or `.rpm` for your distro below | [Releases](https://github.com/Pythoughts-labs/pythinker-code/releases/latest) |
| **🐍 Python fallback** | `pip install pythinker-code` | PyPI |

Every artifact ships with a matching `.sha256` file — verify before install on
any platform with `sha256sum`, `shasum -a 256`, or `Get-FileHash`.

After install, on any OS:

```sh
pythinker --version            # confirm install
pythinker login                # (optional) authenticate a hosted provider
pythinker                      # start the interactive TUI
```

> **In-app updates** — `pythinker update` queries the GitHub Releases API and
> re-runs the right installer for your build with SHA-256 verification. Set
> `PYTHINKER_CLI_NO_AUTO_UPDATE=1` to disable the proactive startup check.

---

### 🪟 Windows — native installer

`PythinkerSetup-0.56.0.exe` is a signed* Inno Setup wizard. Installs per-user
into `%LOCALAPPDATA%\Programs\Pythinker`, registers `pythinker` on your user
PATH (`HKCU\Environment`), broadcasts `WM_SETTINGCHANGE` so new shells see
the change. **No UAC prompt.**

```powershell
# One-line install (downloads the native .exe, verifies SHA-256, runs per-user)
irm https://pythinker.com/install.ps1 | iex

# Or manually download the installer + checksum from the Releases page,
# verify with Get-FileHash, then run:
.\PythinkerSetup-0.56.0.exe

# Open a fresh PowerShell
pythinker --version
```

**Per-machine install** (IT-managed boxes): `.\PythinkerSetup-0.56.0.exe /ALLUSERS`
installs to `%ProgramFiles%\Pythinker` and writes PATH to HKLM (requires admin).

**Upgrade:** `pythinker update` from inside the running app — it downloads
the newest installer, verifies SHA-256, and launches the signed Inno installer
with visible progress (`/SILENT /NORESTART /CURRENTUSER /CLOSEAPPLICATIONS /NORESTARTAPPLICATIONS`).

**Uninstall:** Apps & Features → *Pythinker Code* → Uninstall reverts both
the files and the PATH edit.

> 🛡 **First-launch SmartScreen warning** — \*until the Authenticode cert is
> provisioned in CI, the installer ships unsigned and Windows shows
> *"Windows protected your PC."* Click **More info → Run anyway**. Use the
> published `.sha256` as your integrity check until signing comes online.

---

### <img src="https://img.shields.io/badge/-macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS"> — Homebrew tap

```sh
# 1. Install
brew install Pythoughts-labs/pythinker/pythinker-code
#    Homebrew ≥ 5 may refuse the untrusted tap; trust it once, then re-run:
#      brew trust pythoughts-labs/pythinker

# 2. Verify
pythinker --version
which pythinker          # -> /opt/homebrew/bin/pythinker (Apple Silicon)
                         #    or /usr/local/bin/pythinker (Intel)
```

Works on **Apple Silicon and Intel** from the same native GitHub Release
tarballs used by the curl installer. The tap auto-publishes a fresh formula on
every Pythinker release, so `brew upgrade pythinker-code` always finds the
latest version.

**Upgrade:** `brew upgrade pythinker-code` (Homebrew packages don't
auto-update; run this whenever you want the latest).

> **Untrusted-tap refusal** — Homebrew ≥ 5 (with `HOMEBREW_REQUIRE_TAP_TRUST`)
> refuses third-party taps until you trust them once:
> `brew trust pythoughts-labs/pythinker`. The in-app updater detects the
> refusal and offers to run it for you.

**Uninstall:** `brew uninstall pythinker-code && brew untap Pythoughts-labs/pythinker`.

> The tap repo is [Pythoughts-labs/homebrew-pythinker](https://github.com/Pythoughts-labs/homebrew-pythinker)
> — auto-maintained, do not hand-edit.

---

### <img src="https://img.shields.io/badge/-Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux"> — system packages

Native `.deb` and `.rpm` packages for both `x86_64` and `aarch64` are
attached to every GitHub Release.

```sh
# Debian / Ubuntu (x86_64)
sudo dpkg -i pythinker-code_0.56.0_amd64.deb
sudo apt-get install -f       # only if dpkg reports missing deps

# Debian / Ubuntu (ARM64)
sudo dpkg -i pythinker-code_0.56.0_arm64.deb

# Fedora / RHEL / openSUSE (x86_64)
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.56.0/pythinker-code-0.56.0.x86_64.rpm
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.56.0/pythinker-code-0.56.0.x86_64.rpm.sha256
sha256sum -c pythinker-code-0.56.0.x86_64.rpm.sha256
# Fedora / RHEL:
sudo dnf install ./pythinker-code-0.56.0.x86_64.rpm
# openSUSE:
sudo zypper install ./pythinker-code-0.56.0.x86_64.rpm

# Fedora / RHEL (aarch64)
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.56.0/pythinker-code-0.56.0.aarch64.rpm
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.56.0/pythinker-code-0.56.0.aarch64.rpm.sha256
sha256sum -c pythinker-code-0.56.0.aarch64.rpm.sha256
sudo dnf install ./pythinker-code-0.56.0.aarch64.rpm
```

Both packages drop a small `/usr/bin/pythinker` launcher that execs the real
binary under `/usr/lib/pythinker/`, so your `$PATH` stays tidy.

**Verify before install:**

```sh
sha256sum -c pythinker-code_0.56.0_amd64.deb.sha256        # Debian/Ubuntu
sha256sum -c pythinker-code-0.56.0.x86_64.rpm.sha256       # Fedora/RHEL
```

**Upgrade:** download the new `.deb`/`.rpm` from Releases and `dpkg -i` /
`dnf install` over it. Or run `pythinker update` from inside the running
app — it'll fetch the matching new package and prompt for sudo to install.

**Uninstall:**

```sh
sudo dpkg -r pythinker-code                                # Debian/Ubuntu
sudo rpm -e pythinker-code                                 # Fedora/RHEL
```

---

### <img src="https://img.shields.io/badge/-macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS"> / <img src="https://img.shields.io/badge/-Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux"> — curl-bash native installer

For containers, fresh VMs, or any host without a system package manager.
The canonical `https://pythinker.com/install.sh` endpoint is backed by
[`scripts/install-native.sh`](./scripts/install-native.sh). It serves shell
script content directly, detects your OS + arch, downloads the matching
PyInstaller-frozen tarball, verifies its SHA-256, and lands the single binary
at `~/.local/bin/pythinker`.

```sh
# Latest release
curl -fsSL https://pythinker.com/install.sh | bash

# Pin a specific version
curl -fsSL https://pythinker.com/install.sh | bash -s -- --version 0.27.0

# Custom prefix (defaults to $HOME/.local)
curl -fsSL https://pythinker.com/install.sh | bash -s -- --prefix /opt/pythinker
```

Host `/install.sh` with:

```http
Content-Type: text/x-shellscript; charset=utf-8
Cache-Control: public, max-age=300, s-maxage=900, stale-if-error=86400
```

Use long immutable caching only for versioned release artifact URLs, for
example `Cache-Control: public, max-age=31536000, immutable` on fixed-tag
assets.

Supported targets:

| `uname -s / -m`             | Tarball asset                                            |
|---|---|
| Linux / x86_64              | `pythinker-<version>-x86_64-unknown-linux-gnu.tar.gz`        |
| Linux / aarch64             | `pythinker-<version>-aarch64-unknown-linux-gnu.tar.gz`       |
| Darwin / arm64              | `pythinker-<version>-aarch64-apple-darwin.tar.gz`            |
| Darwin / x86_64             | `pythinker-<version>-x86_64-apple-darwin.tar.gz`             |

The script prints PATH guidance if `~/.local/bin` isn't already on your
`$PATH`.

**Uninstall:** `rm ~/.local/bin/pythinker`.

### 🐍 Python fallback

Use the Python package only when a native installer is not available for your
environment:

```sh
pip install pythinker-code
```

Native installs remain the supported path for new users and for in-app updates.

### 🔐 Authenticate (optional)

For hosted Pythinker models or ACP terminal auth:

```sh
pythinker login
```

### 💬 Try it out

```sh
# Interactive session
pythinker

# One-shot prompt
pythinker --prompt "summarize this repository and suggest the next test to add"

# Pick a specific model
pythinker --model openai/gpt-5.5

# Inline config override
pythinker --config '{"default_thinking": true}'
```

---

## 🏠 Using Local Models (LM Studio & Ollama)

Run Pythinker entirely on your own machine — no API key, no cloud. Pythinker speaks each runtime's OpenAI-compatible API, so tools, streaming, JSON mode, vision, and `reasoning_effort` all work the same as with hosted providers.

### LM Studio

**1. Set up LM Studio.**
- Install [LM Studio](https://lmstudio.ai/) and download at least one chat model.
- In the LM Studio app, open the model and **raise its Context Length** (gear icon → Context Length). See [Context length matters](#context-length-matters) below.
- Start the server: **Developer → Status: Running** (or `lms server start --port 1234`).

**2. Connect Pythinker.**
```sh
pythinker login --lm-studio
```

This auto-discovers every chat-capable model loaded in LM Studio, registers each as `lm-studio/<model-id>`, and picks the largest-context one as your default. Embedding models are filtered out.

**3. Use it.**
```sh
# Default LM Studio model
pythinker -p "explain quicksort"

# Specific model
pythinker -m lm-studio/qwen/qwen3-coder-next -p "write a python http server"

# Interactive shell, then switch models with /model
pythinker
```

**4. Disconnect.**
```sh
pythinker logout --lm-studio
```

### Ollama

```sh
# 1. start the server in one terminal
ollama serve

# 2. pull a model
ollama pull llama3.1:8b

# 3. connect Pythinker
pythinker login --ollama

# 4. use it
pythinker -p "explain monad transformers"
pythinker -m ollama/llama3.1:8b -p "..."
pythinker logout --ollama
```

Discovery uses Ollama's `/api/tags` for the model list and `/api/show` per model to read the real context window.

### Remote LM Studio / Ollama (LAN host or alternate port)

```sh
pythinker login --lm-studio --base-url http://192.168.1.10:1234/v1
pythinker login --ollama    --base-url http://lan-box:11434/v1
```

The override is saved in your config and used by every subsequent run.

### From inside the interactive shell

The same wiring is available as slash commands:

```
/login lm-studio        # or  /login lmstudio  (no dash also accepted)
/login ollama
/logout lm-studio
/logout ollama
/login                  # opens a chooser; entries 9 and 10 are the local providers
/model lm-studio/google/gemma-4-e4b   # switch model mid-session
```

### <a id="context-length-matters"></a>⚠️ Context length matters (a common gotcha)

Pythinker's agent prompt — system instructions + tool schemas + skills + your message + recent history — is large. **Tens of thousands of tokens before you've even sent your first message.**

LM Studio loads a model with a small default context window (often `4096`). If you start chatting against that, you'll see:

```
LLM provider error: Error: The number of tokens to keep from the initial
prompt is greater than the context length (n_keep: 16690 >= n_ctx: 4096).
```

The shell now prints a friendly recovery hint when this happens, but **the cure is in LM Studio**:

1. In LM Studio, open the model in the **Chat** tab and click the **gear/settings** icon (or **My Models → Edit**).
2. Set **Context Length** to at least **`32768`**, and prefer **`131072`** if your VRAM allows. *Practical experience: 64k still triggers errors during longer sessions; 128k is a safer floor.*
3. Reload the model (LM Studio prompts you).
4. Restart Pythinker so it picks up the new state (`Ctrl+D` then `pythinker`, or `pythinker -r <session-id>` to resume).

**Tip:** the bigger you set the context, the more VRAM the model uses. If you OOM, try a smaller quantization (e.g., Q4_K_M instead of Q8_0) or a smaller model variant.

Ollama configures context per-request and Pythinker reads the model's max from `/api/show`, so this gotcha is mostly LM-Studio-specific.

### VRAM-friendly model picks

Local models vary wildly in memory use. Rough guide on a 16 GB GPU (e.g., RTX 5080 mobile):

| Model size | Quant | Approx. VRAM | Fits 16 GB? |
|------------|-------|--------------|-------------|
| 2-4 B      | Q4-Q8 | 2-4 GB       | Yes, easily |
| 7-8 B      | Q4    | 5-6 GB       | Yes |
| 7-8 B      | Q8    | 8-9 GB       | Yes |
| 13-14 B    | Q4    | 8-10 GB      | Yes |
| 27-31 B    | Q4    | 17-20 GB     | Tight / no |
| 27-31 B    | Q8    | 30-35 GB     | No |

If LM Studio errors with `Failed to load model`, you've exceeded VRAM — pick a smaller model or lower-bit quantization.

### Environment variables

These override the defaults at both login and runtime:

| Variable | Purpose |
|----------|---------|
| `LM_STUDIO_BASE_URL` | Override `http://localhost:1234/v1` |
| `LM_STUDIO_API_KEY`  | Set if you've enabled token auth in LM Studio |
| `OLLAMA_BASE_URL`    | Override `http://localhost:11434/v1` |
| `OLLAMA_API_KEY`     | Rarely needed (Ollama is unauthenticated by default) |

Example:
```sh
LM_STUDIO_BASE_URL=http://workstation.lan:1234/v1 pythinker -p "..."
```

### Refreshing the model list

If you load/unload models in LM Studio (or `ollama pull/rm`), re-run login to refresh:

```sh
pythinker login --lm-studio    # or --ollama
```

(Pythinker intentionally does NOT auto-refresh local providers in the background — login owns that state, so manual edits to your config aren't silently overwritten.)

---

## 🧩 IDE Integration via ACP

Pythinker speaks [**Agent Client Protocol**](https://github.com/agentclientprotocol/agent-client-protocol) natively. Point your ACP-compatible editor at `pythinker acp` and you get a multi-session agent server inside your IDE.

<details>
<summary><b>📝 Configuration for Zed / JetBrains</b></summary>

```json
{
  "agent_servers": {
    "Pythinker Code": {
      "type": "custom",
      "command": "pythinker",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

</details>

The ACP server provides:

| Capability | Description |
|---|---|
| 🔑 **Terminal auth** | `pythinker login` flow exposed to the IDE |
| 📂 **Session listing & resume** | Pick up where you left off |
| 🔄 **Hot model swap** | Change models for a running ACP session |

<div align="center">
<img src="https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/docs/media/acp-integration.gif" alt="ACP IDE integration demo" width="860">
</div>

---

## 🔌 MCP Tooling

Pythinker loads [Model Context Protocol](https://modelcontextprotocol.io/) tools from persistent config or ad-hoc files. Same tools, every agent — no rewriting.

### 🛠️ Manage persistent MCP servers

```sh
# 📚 Context7 stdio server (positional form: NAME -- COMMAND)
pythinker mcp add context7 -- npx -y @upstash/context7-mcp --api-key YOUR-API-KEY
# Added MCP server 'context7' to ~/.pythinker/mcp.json

# 🌐 Streamable HTTP server with API key
pythinker mcp add --transport http docs https://example.com/mcp \
  --header "API_KEY: your-key"

# 🔐 Streamable HTTP server with OAuth
pythinker mcp add --transport http --auth oauth linear https://mcp.linear.app/mcp

# 💻 stdio server with explicit transport
pythinker mcp add --transport stdio chrome-devtools -- npx chrome-devtools-mcp@latest

# 📋 List, authorize, test, and remove
pythinker mcp list
pythinker mcp auth linear
pythinker mcp test chrome-devtools
pythinker mcp remove chrome-devtools
```

### 📄 Use an ad-hoc MCP config file

```json
{
  "mcpServers": {
    "context7": {
      "url": "https://mcp.context7.com/mcp",
      "headers": {
        "CONTEXT7_API_KEY": "YOUR_API_KEY"
      }
    },
    "chrome-devtools": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@latest"]
    }
  }
}
```

```sh
pythinker --mcp-config-file /path/to/mcp.json
```

---

## 🧬 Extensibility

Pythinker is a small, extensible runtime — not a monolith. Build on it.

| Extension Point | What it does | Where to look |
|---|---|---|
| 🤖 **Agents & subagents** | YAML specs define tools, prompts, and built-in subagent types | `src/pythinker_code/agents/` |
| 🎓 **Skills** | `/skill:<name>` loads reusable instructions on demand | bundled & user-defined |
| 🌊 **Flows** | `/flow:<name>` executes bundled prompt flows | bundled & user-defined |
| 🪝 **Hooks** | Observe or block tool execution; integrate policy or automation | hook events API |
| 🧩 **Plugins** | Installable extension packages | `pythinker plugin` |

---

## 🏗️ Architecture

<div align="center">
<img src="https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/docs/media/Architecture.webp" alt="Pythinker Code architecture diagram" width="860">
</div>

---

## 🔐 Privacy & Telemetry

Pythinker is the **agent framework**, not the LLM. You bring your own API key
(OpenAI, Anthropic, your local LM Studio model, etc.); your prompts and the
model's responses go directly between your terminal and the model provider you
configured. Pythinker never sees, stores, or forwards them.

If you opt in, Pythinker can collect a small amount of **diagnostic
telemetry** about how the agent runs to improve the framework itself. It's
strictly anonymous, never includes your prompts, model output, file contents,
file paths, or any user-identifying data. Two channels:

| Channel | What lands there | Endpoint |
|---|---|---|
| **Errors** (Sentry-protocol) | Unhandled exceptions and crash stack traces, with absolute paths above `site-packages/` rewritten to `<env>/` so home directories don't leak | `errors.pythinker.com` (self-hosted Bugsink) |
| **Traces + structured logs** (OpenTelemetry) | Lifecycle events (`session_started`, `started`, `model_switch`), agent-loop spans (`pythinker.turn` / `pythinker.llm` / `pythinker.tool`), and per-event counters | `otel.pythinker.com` (self-hosted SigNoz) |

### What we collect

- **Lifecycle events**: session start, command-line flags actually used (booleans only), startup timing, model name (just the identifier, e.g. `claude-opus-4-7`), thinking-mode toggle, plan-mode toggle.
- **Agent-loop spans**: turn duration, step count, stop reason (`no_tool_calls` / `max_steps` / `error`), tool name (`Read`, `Bash`, `Edit`, …), tool success/failure, tool duration, LLM call duration, input/output token *counts* (numbers — never the content).
- **Crashes**: exception class name, scrubbed stack trace, library versions. We do **not** send local variable values.
- **Static context**: pythinker version, OS family, Python version, terminal type (`TERM_PROGRAM`), CI flag (`CI` env var presence), locale.
- **A persistent, random `device_id`** so we can count "how many distinct installs" without identifying a person.

### What we never collect

- Your prompts, the model's responses, or any conversation content
- File contents, file paths, working directory names, or workspace structure
- Your API keys, OAuth tokens, environment variables
- Your real name, email, IP address, hostname (host name field is dropped at the edge collector)
- Tool arguments (e.g. what file you read, what command you ran)

### Turning it off

Telemetry is on by default. To turn it off:

```sh
# 1. Per-invocation CLI flag
pythinker --no-telemetry

# 2. Environment variable (works in shells, .env files, CI configs)
export PYTHINKER_DISABLE_TELEMETRY=1
pythinker

# 3. Permanently in your config file (~/.pythinker/config.toml)
[default]
telemetry = false
```

When telemetry is disabled, Pythinker short-circuits Sentry initialization,
OTel exporter creation, and the in-process event sink. No network requests are
made to the telemetry endpoints.

### Pointing telemetry at your own infrastructure

If you operate pythinker for a team and want telemetry routed to your own
SigNoz / Bugsink instead, override the endpoints via environment variables:

```sh
export PYTHINKER_SENTRY_DSN="https://<key>@your-bugsink.example.com/<project>"
export PYTHINKER_OTEL_ENDPOINT="https://your-otel-collector.example.com"
export PYTHINKER_OTEL_TOKEN="<your bearer token>"
```

The defaults point at infrastructure operated by the pythinker maintainers and
are used automatically unless you turn telemetry off.

---

## 🛠️ Development

### 🏁 Prepare the workspace

```sh
git clone https://github.com/Pythoughts-labs/pythinker-code.git
cd pythinker-code
make prepare
```

### 🧰 Common commands

<table>
<tr>
<td valign="top">

**▶️ Run & iterate**
```sh
uv run pythinker          # CLI from source
make format               # format all packages
make check                # lint + type-check
```

</td>
<td valign="top">

**🧪 Test**
```sh
make test                 # all unit + e2e tests
make ai-test              # AI-driven tests
make test-pythinker-code   # CLI only
make test-pythinker-core  # Core only
make test-pythinker-host  # Host only
make test-pythinker-sdk   # SDK only
```

</td>
</tr>
<tr>
<td valign="top">

**🌐 Frontends**
```sh
make web-back             # web backend
make web-front            # web frontend
make dashboard-back       # dashboard backend
make dashboard-front      # dashboard frontend
```

</td>
<td valign="top">

**📦 Build**
```sh
make build                # Python packages
make build-bin            # standalone binary
make help                 # all targets
```

</td>
</tr>
</table>

> 💡 `make build` and `make build-bin` build and embed the web and visualization frontends before packaging.

---

## 🗂️ Project Layout

```
pythinker-code/
├── 📦 src/pythinker_code/         CLI runtime · tools · UIs · ACP · MCP · hooks · plugins · skills · web · dashboard backends
├── 🧱 packages/
│   ├── pythinker-core/           Provider-agnostic message, tool, and chat-provider abstractions
│   ├── pythinker-host/           Local/remote host filesystem and command execution
│   └── pythinker-code/           Console-script distribution package
├── 🧰 sdks/pythinker-sdk/        Python SDK
└── 🧪 tests/ · tests_e2e/ · tests_ai/   Unit · wire/CLI e2e · AI-driven test suites
```

---

## 🤝 Contributing

Contributions are warmly welcome — bug reports, PRs, plugins, skills, and docs all help.

- 📖 Start with [`CONTRIBUTING.md`](https://github.com/Pythoughts-labs/pythinker-code/blob/main/CONTRIBUTING.md)
- 🔐 See [`SECURITY.md`](https://github.com/Pythoughts-labs/pythinker-code/blob/main/SECURITY.md) for responsible disclosure
- 📜 Skim [`AGENTS.md`](https://github.com/Pythoughts-labs/pythinker-code/blob/main/AGENTS.md) for the agent design notes

If Pythinker helps you, **a ⭐ on GitHub goes a long way.**

---

## 📜 License

Distributed under the **Apache-2.0 License**. See [`LICENSE`](https://github.com/Pythoughts-labs/pythinker-code/blob/main/LICENSE) for the full text and [`NOTICE`](https://github.com/Pythoughts-labs/pythinker-code/blob/main/NOTICE) for attributions.

<br />

<div align="center">

**Built with ❤️ for engineers who live in the terminal.**

[🌐 pythinker.com](https://pythinker.com) &nbsp;·&nbsp;
[📦 PyPI](https://pypi.org/project/pythinker-code/) &nbsp;·&nbsp;
[🐙 GitHub](https://github.com/Pythoughts-labs/pythinker-code) &nbsp;·&nbsp;
[🧩 ACP](https://github.com/agentclientprotocol/agent-client-protocol) &nbsp;·&nbsp;
[🔌 MCP](https://modelcontextprotocol.io/)

<p align="center">
  <em>Thanks for visiting ✨ Pythinker! Viewing this README does not trigger telemetry.</em>
</p>

</div>
