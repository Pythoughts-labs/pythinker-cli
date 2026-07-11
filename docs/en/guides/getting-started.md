# Getting Started

## What is Pythinker Code

Pythinker Code is an AI agent that runs in the terminal, helping you complete software development tasks and terminal operations. It can read and edit code, execute shell commands, search and fetch web pages, and autonomously plan and adjust actions during execution.

Pythinker Code is suited for:

- **Writing and modifying code**: Implementing new features, fixing bugs, refactoring code
- **Understanding projects**: Exploring unfamiliar codebases, answering architecture and implementation questions
- **Automating tasks**: Batch processing files, running builds and tests, executing scripts

Pythinker Code supports the following usage modes:

- **[Interactive CLI (`pythinker`)](../reference/pythinker-command.md)**: Chat with AI in the terminal using natural language or execute shell commands directly
- **[Browser UI (`pythinker web`)](../reference/pythinker-web.md)**: Open a graphical interface in your local browser, with session management, file references, code highlighting, and more
- **[Agent integration (`pythinker acp`)](../reference/pythinker-acp.md)**: Run as a service and integrate with [IDEs](./ides.md) and other local agent clients via the [Agent Client Protocol]

::: info Tip
If you encounter issues or have suggestions, please provide feedback on [GitHub Issues](https://github.com/Pythoughts-labs/pythinker-code/issues).
:::

[Agent Client Protocol]: https://agentclientprotocol.com/

## Installation

Run the native installation script to complete the installation. The canonical endpoint serves the shell script directly and installs the PyInstaller-built binary by default:

```sh
# Linux / macOS
curl -fsSL https://pythinker.com/install.sh | bash

# Pin a specific version
curl -fsSL https://pythinker.com/install.sh | bash -s -- --version 0.51.0

# Custom prefix (defaults to $HOME/.local)
curl -fsSL https://pythinker.com/install.sh | bash -s -- --prefix /opt/pythinker
```

On Windows, run the PowerShell bootstrap. It downloads the native installer, verifies its SHA-256 file, and runs the per-user install:

```powershell
# Windows (PowerShell)
irm https://pythinker.com/install.ps1 | iex
```

You can also download `PythinkerSetup-0.58.0.exe` manually from the [latest release](https://github.com/Pythoughts-labs/pythinker-code/releases/latest).

Verify the installation:

```sh
pythinker --version
```

::: tip
Due to macOS security checks, the first run of the `pythinker` command may take longer. You can add your terminal application in "System Settings → Privacy & Security → Developer Tools" to speed up subsequent launches.
:::

If you need the legacy Python package fallback, use:

```sh
pip install pythinker-code
```

## Upgrade and uninstall

Upgrade to the latest version:

```sh
pythinker update
```

Uninstall a curl-bash install:

```sh
rm ~/.local/bin/pythinker
```

If you installed through a platform package manager, uninstall with the same tool: Homebrew (`brew uninstall pythinker-code`), Debian/Ubuntu (`sudo dpkg -r pythinker-code`), or RPM (`sudo rpm -e pythinker-code`).

## First run

Run the `pythinker` command in the project directory where you want to work to start Pythinker Code:

```sh
cd your-project
pythinker
```

On first launch, you need to configure your API source. Enter the `/login` command to start configuration:

```
/login
```

After execution, first select a platform. We recommend **Pythinker**, which automatically opens a browser for OAuth authorization; selecting other platforms requires entering an API key. After configuration, Pythinker Code will automatically save the settings and reload. See [Providers](../configuration/providers.md) for details.

Now you can chat with Pythinker Code directly using natural language. Try describing a task you want to complete, for example:

```
Show me the directory structure of this project
```

::: tip
If the project doesn't have an `AGENTS.md` file, you can run the `/init` command to have Pythinker Code analyze the project and generate this file, helping the AI better understand the project structure and conventions.
:::

Enter `/help` to view all available [slash commands](../reference/slash-commands.md) and usage tips.
