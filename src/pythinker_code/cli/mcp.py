import contextlib
import json
import os
from pathlib import Path, PurePath
from typing import Annotated, Any, Literal, cast

import typer

cli = typer.Typer(help="Manage MCP server configurations.")

_CONTAINER_RUNTIMES = frozenset({"docker", "podman"})


def ensure_docker_rm(command: str, args: list[str]) -> list[str]:
    """Inject ``--rm`` into a docker/podman ``run`` invocation (mcpext-3).

    Killing the ``docker run`` client process does not remove the daemon-managed
    container, so a stdio MCP server launched as ``docker run …`` leaves a stopped
    container behind on teardown unless ``--rm`` is present. This adds it. No-op for
    other commands, non-``run`` subcommands, or when ``--rm`` is already there.
    """
    runtime = PurePath(command).name.lower()
    if runtime not in _CONTAINER_RUNTIMES:
        return args
    if not args or args[0] != "run" or "--rm" in args:
        return args
    return [args[0], "--rm", *args[1:]]


def apply_docker_rm_to_mcp_config_dict(config: dict[str, Any]) -> dict[str, Any]:
    """Inject ``--rm`` into stdio docker/podman servers when loading mcp.json (mcpext-3)."""
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return config
    typed_servers = cast(dict[str, Any], servers)
    for raw_server in typed_servers.values():
        if not isinstance(raw_server, dict):
            continue
        server = cast(dict[str, Any], raw_server)
        command = server.get("command")
        if not isinstance(command, str):
            continue
        raw_args = server.get("args")
        args: list[str] = []
        if isinstance(raw_args, list):
            args = [str(item) for item in cast(list[object], raw_args)]
        server["args"] = ensure_docker_rm(command, args)
    return config


def prepare_mcp_config_dict(config: dict[str, Any]) -> dict[str, Any]:
    """Apply portable MCP config fixes before validation (docker ``--rm``, name normalization)."""
    from pythinker_code.utils.mcp_names import normalize_mcp_servers_in_config

    apply_docker_rm_to_mcp_config_dict(config)
    return normalize_mcp_servers_in_config(config)


def get_global_mcp_config_file() -> Path:
    """Get the global MCP config file path."""
    from pythinker_code.share import get_share_dir

    return get_share_dir() / "mcp.json"


def _load_mcp_config() -> dict[str, Any]:
    """Load MCP config from global mcp config file."""
    from fastmcp.mcp_config import MCPConfig
    from pydantic import ValidationError

    mcp_file = get_global_mcp_config_file()
    if not mcp_file.exists():
        return {"mcpServers": {}}
    try:
        config = json.loads(mcp_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"Invalid JSON in MCP config file '{mcp_file}': {e}") from e

    try:
        MCPConfig.model_validate(config)
    except ValidationError as e:
        raise typer.BadParameter(f"Invalid MCP config in '{mcp_file}': {e}") from e

    return config


def _save_mcp_config(config: dict[str, Any]) -> None:
    """Save MCP config to the default file, owner-only (0600).

    Open and tighten the mode BEFORE writing any content so the file (which may
    carry MCP server secrets) is never briefly world-readable — the previous
    ``write_text`` then ``chmod`` left a 0644 window.
    """
    mcp_file = get_global_mcp_config_file()
    payload = json.dumps(config, indent=2, ensure_ascii=False)
    fd = os.open(mcp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        with contextlib.suppress(OSError):
            os.fchmod(fh.fileno(), 0o600)  # tighten a pre-existing 0644 file before writing
        fh.write(payload)


def _get_mcp_server(name: str, *, require_remote: bool = False) -> dict[str, Any]:
    """Get MCP server config by name."""
    config = _load_mcp_config()
    servers = config.get("mcpServers", {})
    if name not in servers:
        typer.echo(f"MCP server '{name}' not found.", err=True)
        raise typer.Exit(code=1)
    server = servers[name]
    if require_remote and "url" not in server:
        typer.echo(f"MCP server '{name}' is not a remote server.", err=True)
        raise typer.Exit(code=1)
    return server


def _parse_key_value_pairs(
    items: list[str], option_name: str, *, separator: str = "=", strip_whitespace: bool = False
) -> dict[str, str]:
    """Parse key/value pairs from CLI options."""
    parsed: dict[str, str] = {}
    for item in items:
        if separator not in item:
            typer.echo(
                f"Invalid {option_name} format: {item} (expected KEY{separator}VALUE).",
                err=True,
            )
            raise typer.Exit(code=1)
        key, value = item.split(separator, 1)
        if strip_whitespace:
            key, value = key.strip(), value.strip()
        if not key:
            typer.echo(f"Invalid {option_name} format: {item} (empty key).", err=True)
            raise typer.Exit(code=1)
        parsed[key] = value
    return parsed


Transport = Literal["stdio", "http"]


@cli.command(
    "add",
    epilog="""
    Examples:\n
      \n
      # Add streamable HTTP server:\n
      pythinker mcp add --transport http context7 https://mcp.context7.com/mcp --header \"CONTEXT7_API_KEY: ctx7sk-your-key\"\n
      \n
      # Add streamable HTTP server with OAuth authorization:\n
      pythinker mcp add --transport http --auth oauth linear https://mcp.linear.app/mcp\n
      \n
      # Add stdio server:\n
      pythinker mcp add --transport stdio chrome-devtools -- npx chrome-devtools-mcp@latest
    """.strip(),  # noqa: E501
)
def mcp_add(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to add."),
    ],
    server_args: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="TARGET_OR_COMMAND...",
            help="For http: server URL. For stdio: command to run (prefix with `--`).",
        ),
    ] = None,
    transport: Annotated[
        Transport,
        typer.Option(
            "--transport",
            "-t",
            help="Transport type for the MCP server. Default: stdio.",
        ),
    ] = "stdio",
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help="Environment variables in KEY=VALUE format. Can be specified multiple times.",
        ),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header",
            "-H",
            help="HTTP headers in KEY:VALUE format. Can be specified multiple times.",
        ),
    ] = None,
    auth: Annotated[
        str | None,
        typer.Option(
            "--auth",
            "-a",
            help="Authorization type (e.g., 'oauth').",
        ),
    ] = None,
):
    """Add an MCP server."""
    from pythinker_code.exception import MCPConfigError
    from pythinker_code.utils.mcp_names import normalize_mcp_server_name

    config = _load_mcp_config()
    server_args = server_args or []
    try:
        stored_name = normalize_mcp_server_name(name)
    except MCPConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if stored_name != name:
        typer.echo(f"Normalized MCP server name '{name}' to '{stored_name}'.")

    if transport not in {"stdio", "http"}:
        typer.echo(f"Unsupported transport: {transport}.", err=True)
        raise typer.Exit(code=1)

    if transport == "stdio":
        if not server_args:
            typer.echo(
                "For stdio transport, provide the command to start the MCP server after `--`.",
                err=True,
            )
            raise typer.Exit(code=1)
        if header:
            typer.echo("--header is only valid for http transport.", err=True)
            raise typer.Exit(code=1)
        if auth:
            typer.echo("--auth is only valid for http transport.", err=True)
            raise typer.Exit(code=1)
        command, *command_args = server_args
        server_config: dict[str, Any] = {
            "command": command,
            "args": ensure_docker_rm(command, command_args),
        }
        if env:
            server_config["env"] = _parse_key_value_pairs(env, "env")
    else:
        if env:
            typer.echo("--env is only supported for stdio transport.", err=True)
            raise typer.Exit(code=1)
        if not server_args:
            typer.echo("URL is required for http transport.", err=True)
            raise typer.Exit(code=1)
        if len(server_args) > 1:
            typer.echo(
                "Multiple targets provided. Supply a single URL for http transport.",
                err=True,
            )
            raise typer.Exit(code=1)
        server_config = {"url": server_args[0], "transport": "http"}
        if header:
            server_config["headers"] = _parse_key_value_pairs(
                header, "header", separator=":", strip_whitespace=True
            )
        if auth:
            server_config["auth"] = auth

    if "mcpServers" not in config:
        config["mcpServers"] = {}
    if stored_name in config["mcpServers"]:
        typer.echo(f"MCP server '{stored_name}' already exists.", err=True)
        raise typer.Exit(code=1)
    config["mcpServers"][stored_name] = server_config
    _save_mcp_config(config)
    typer.echo(f"Added MCP server '{stored_name}' to {get_global_mcp_config_file()}.")


@cli.command("remove")
def mcp_remove(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to remove."),
    ],
):
    """Remove an MCP server."""
    _get_mcp_server(name)
    config = _load_mcp_config()
    del config["mcpServers"][name]
    _save_mcp_config(config)
    typer.echo(f"Removed MCP server '{name}' from {get_global_mcp_config_file()}.")


def _oauth_token_storage(server_url: str) -> Any:
    from fastmcp.client.auth import oauth as fastmcp_oauth

    file_token_storage = getattr(fastmcp_oauth, "FileTokenStorage", None)
    if file_token_storage is not None:
        return file_token_storage(server_url=server_url)
    provider: Any = fastmcp_oauth.OAuth(mcp_url=server_url)
    return provider.token_storage_adapter


def _has_oauth_tokens(server_url: str) -> bool:
    """Check if OAuth tokens exist for the server."""
    import asyncio

    async def _check() -> bool:
        try:
            storage = _oauth_token_storage(server_url)
            tokens = await storage.get_tokens()
            return tokens is not None
        except Exception:
            return False

    return asyncio.run(_check())


@cli.command("list")
def mcp_list():
    """List all MCP servers."""
    config_file = get_global_mcp_config_file()
    config = _load_mcp_config()
    servers: dict[str, Any] = config.get("mcpServers", {})

    typer.echo(f"MCP config file: {config_file}")
    if not servers:
        typer.echo("No MCP servers configured.")
        return

    for name, server in servers.items():
        if "command" in server:
            cmd = server["command"]
            cmd_args = " ".join(server.get("args", []))
            line = f"{name} (stdio): {cmd} {cmd_args}".rstrip()
        elif "url" in server:
            transport = server.get("transport") or "http"
            if transport == "streamable-http":
                transport = "http"
            line = f"{name} ({transport}): {server['url']}"
            if server.get("auth") == "oauth" and not _has_oauth_tokens(server["url"]):
                line += " [authorization required - run: pythinker mcp auth " + name + "]"
        else:
            line = f"{name}: {server}"
        typer.echo(f"  {line}")


@cli.command("auth")
def mcp_auth(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to authorize."),
    ],
):
    """Authorize with an OAuth-enabled MCP server."""
    import asyncio

    server = _get_mcp_server(name, require_remote=True)
    if server.get("auth") != "oauth":
        typer.echo(f"MCP server '{name}' does not use OAuth. Add with --auth oauth.", err=True)
        raise typer.Exit(code=1)

    async def _auth() -> None:
        import fastmcp

        typer.echo(f"Authorizing with '{name}'...")
        typer.echo("A browser window will open for authorization.")

        client = fastmcp.Client({"mcpServers": {name: server}})
        try:
            async with client:
                tools = await client.list_tools()
                typer.echo(f"Successfully authorized with '{name}'.")
                typer.echo(f"Available tools: {len(tools)}")
        except Exception as e:
            typer.echo(f"Authorization failed: {type(e).__name__}: {e}", err=True)
            raise typer.Exit(code=1) from None

    asyncio.run(_auth())


@cli.command("reset-auth")
def mcp_reset_auth(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to reset authorization."),
    ],
):
    """Reset OAuth authorization for an MCP server (clear cached tokens)."""
    server = _get_mcp_server(name, require_remote=True)

    try:
        import asyncio

        async def _clear() -> None:
            storage = _oauth_token_storage(server["url"])
            result = storage.clear()
            if hasattr(result, "__await__"):
                await result

        asyncio.run(_clear())
        typer.echo(f"OAuth tokens cleared for '{name}'.")
    except ImportError:
        typer.echo("OAuth support not available.", err=True)
        raise typer.Exit(code=1) from None
    except Exception as e:
        typer.echo(f"Failed to clear tokens: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1) from None


@cli.command("test")
def mcp_test(
    name: Annotated[
        str,
        typer.Argument(help="Name of the MCP server to test."),
    ],
):
    """Test connection to an MCP server and list available tools."""
    import asyncio

    server = _get_mcp_server(name)

    async def _test() -> None:
        import fastmcp

        typer.echo(f"Testing connection to '{name}'...")
        client = fastmcp.Client({"mcpServers": {name: server}})

        try:
            async with client:
                tools = await client.list_tools()
                typer.echo(f"✓ Connected to '{name}'")
                typer.echo(f"  Available tools: {len(tools)}")
                if tools:
                    typer.echo("  Tools:")
                    for tool in tools:
                        desc = tool.description or ""
                        if len(desc) > 50:
                            desc = desc[:47] + "..."
                        typer.echo(f"    - {tool.name}: {desc}")
        except Exception as e:
            typer.echo(f"✗ Connection failed: {type(e).__name__}: {e}", err=True)
            raise typer.Exit(code=1) from None

    asyncio.run(_test())
