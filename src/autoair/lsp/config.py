"""Language-server settings (``AUTOAIR_LSP__*``): the IDE-grade IntelliSense behind
``features.ide_language_server``.

Each editor session gets its own language-server process (basedpyright or python-lsp-server
for Python, bash-language-server for shell) in a temporary workspace; the browser talks LSP
JSON-RPC to it over ``/api/lsp/ws/{language}``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PythonServer = Literal["basedpyright", "pylsp"]


class LspSettings(BaseModel):
    """Language servers proxied to the code editor (``features.ide_language_server``)."""

    model_config = ConfigDict(extra="forbid")

    python_server: PythonServer = Field(
        default="basedpyright",
        description="Python language server: basedpyright (pip install basedpyright; bundles "
        "Node) or pylsp (pip install 'autoair[lsp]': python-lsp-server + python-lsp-ruff).",
    )
    basedpyright_command: list[str] = Field(
        default_factory=lambda: ["basedpyright-langserver", "--stdio"],
        description="Command starting basedpyright in stdio mode (first item looked up on "
        "PATH and next to the running Python).",
    )
    pylsp_command: list[str] = Field(
        default_factory=lambda: ["pylsp"],
        description="Command starting python-lsp-server in stdio mode.",
    )
    bash_command: list[str] = Field(
        default_factory=lambda: ["bash-language-server", "start"],
        description="Command starting bash-language-server (npm i -g bash-language-server); "
        "when missing, the editor keeps its built-in completions + ShellCheck.",
    )
    max_sessions: int = Field(
        default=16, ge=1, le=1024, description="Language-server processes alive at once."
    )
    max_sessions_per_user: int = Field(
        default=4, ge=1, le=1024, description="Language-server processes per user."
    )
    idle_timeout: float = Field(
        default=600, gt=0, description="Seconds without a message before a session is closed."
    )
    start_timeout: float = Field(
        default=30, gt=0, description="Seconds a language server may take to start."
    )
    memory_limit_mb: int | None = Field(
        default=2048,
        ge=64,
        description="Address-space limit (MB) of each language-server process (POSIX "
        "RLIMIT_AS; ignored where unsupported). Empty for no limit.",
    )
    cpu_limit_seconds: int | None = Field(
        default=3600,
        ge=1,
        description="CPU-time limit (s) of each language-server process (POSIX RLIMIT_CPU). "
        "Empty for no limit.",
    )
    max_message_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        description="Largest JSON-RPC message accepted from the browser or the server.",
    )


__all__ = ["LspSettings", "PythonServer"]
