"""Multi-server LSP routing and text-document synchronization."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from pythinker_host import Host

from pythinker_code.config import LspServerConfig
from pythinker_code.lsp.instance import LspServerInstance, LspState
from pythinker_code.utils.logging import logger as default_logger


class LspServerManager:
    """Routes file operations to configured language servers."""

    def __init__(
        self,
        host: Host,
        servers: dict[str, LspServerConfig],
        *,
        workspace_folder: str,
        logger: Any = None,
    ) -> None:
        self._host = host
        self._workspace_folder = workspace_folder
        self._logger = logger or default_logger
        self._server_configs = servers
        self._instances: dict[str, LspServerInstance] = {}
        self._ext_map: dict[str, list[str]] = {}
        self._opened_files: dict[str, str] = {}
        self._doc_versions: dict[str, int] = {}

    async def initialize(self) -> None:
        errors: list[str] = []
        for server_name, config in self._server_configs.items():
            try:
                if not config.command:
                    raise ValueError(f"Server {server_name} missing required 'command' field")
                if not config.extension_to_language:
                    raise ValueError(
                        f"Server {server_name} missing required 'extension_to_language' field"
                    )

                for ext in config.extension_to_language:
                    normalized = ext.lower()
                    self._ext_map.setdefault(normalized, []).append(server_name)

                instance = LspServerInstance(
                    server_name,
                    config,
                    self._host,
                    workspace_folder=self._workspace_folder,
                    logger=self._logger,
                )
                instance.on_request("workspace/configuration", _workspace_configuration_handler)
                self._instances[server_name] = instance
            except Exception as exc:
                message = f"Failed to initialize LSP server {server_name}: {exc}"
                self._logger.error(message)
                errors.append(message)

        if errors:
            ok = len(self._instances)
            total = len(self._server_configs)
            self._logger.error(
                f"LSP manager initialized with {ok}/{total} servers; failures: {'; '.join(errors)}"
            )

    async def shutdown(self) -> None:
        to_stop = [
            (name, instance)
            for name, instance in self._instances.items()
            if instance.state in (LspState.RUNNING, LspState.ERROR)
        ]
        results = await asyncio.gather(
            *(instance.stop() for _, instance in to_stop),
            return_exceptions=True,
        )

        self._instances.clear()
        self._ext_map.clear()
        self._opened_files.clear()
        self._doc_versions.clear()

        stop_errors = [
            f"{to_stop[i][0]}: {result}"
            for i, result in enumerate(results)
            if isinstance(result, Exception)
        ]
        if stop_errors:
            raise RuntimeError(
                f"Failed to stop {len(stop_errors)} LSP server(s): {'; '.join(stop_errors)}"
            )

    def server_for_file(self, path: str) -> LspServerInstance | None:
        ext = Path(path).suffix.lower()
        server_names = self._ext_map.get(ext)
        if not server_names:
            return None
        return self._instances.get(server_names[0])

    async def ensure_started(self, path: str) -> LspServerInstance | None:
        server = self.server_for_file(path)
        if server is None:
            return None
        if server.state in (LspState.STOPPED, LspState.ERROR):
            # A (re)start spawns a fresh process with no open documents. Drop any
            # stale per-server open-file and version state so didOpen is re-sent
            # instead of being skipped as already-open on the new process.
            await server.start()
            self._clear_server_doc_state(server.name)
        return server

    def _clear_server_doc_state(self, server_name: str) -> None:
        stale_uris = [uri for uri, name in self._opened_files.items() if name == server_name]
        for uri in stale_uris:
            self._opened_files.pop(uri, None)
            self._doc_versions.pop(uri, None)

    async def send_request(self, path: str, method: str, params: Any) -> Any | None:
        server = await self.ensure_started(path)
        if server is None:
            return None
        return await server.send_request(method, params)

    def all_servers(self) -> dict[str, LspServerInstance]:
        return dict(self._instances)

    def is_file_open(self, path: str) -> bool:
        return _file_uri(path) in self._opened_files

    async def open_file(self, path: str, content: str) -> None:
        server = await self.ensure_started(path)
        if server is None:
            return

        file_uri = _file_uri(path)
        if self._opened_files.get(file_uri) == server.name:
            return

        ext = Path(path).suffix.lower()
        language_id = server.config.extension_to_language.get(ext, "plaintext")
        await server.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": file_uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": content,
                }
            },
        )
        self._opened_files[file_uri] = server.name
        self._doc_versions[file_uri] = 1

    async def change_file(self, path: str, content: str) -> None:
        server = self.server_for_file(path)
        if server is None or server.state != LspState.RUNNING:
            await self.open_file(path, content)
            return

        file_uri = _file_uri(path)
        if self._opened_files.get(file_uri) != server.name:
            await self.open_file(path, content)
            return

        version = self._doc_versions.get(file_uri, 1) + 1
        self._doc_versions[file_uri] = version
        await server.send_notification(
            "textDocument/didChange",
            {
                "textDocument": {"uri": file_uri, "version": version},
                "contentChanges": [{"text": content}],
            },
        )

    async def save_file(self, path: str) -> None:
        server = self.server_for_file(path)
        if server is None or server.state != LspState.RUNNING:
            return
        await server.send_notification(
            "textDocument/didSave",
            {"textDocument": {"uri": _file_uri(path)}},
        )

    async def close_file(self, path: str) -> None:
        server = self.server_for_file(path)
        if server is None or server.state != LspState.RUNNING:
            return

        file_uri = _file_uri(path)
        await server.send_notification(
            "textDocument/didClose",
            {"textDocument": {"uri": file_uri}},
        )
        self._opened_files.pop(file_uri, None)
        self._doc_versions.pop(file_uri, None)


def _file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _workspace_configuration_handler(params: dict[str, Any]) -> list[None]:
    items: Any = params.get("items", [])
    if not isinstance(items, list):
        return []
    return [None] * len(cast(list[Any], items))
