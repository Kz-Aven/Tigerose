"""Connector contracts shared by lifecycle management and runtime resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ConnectorRuntime = Literal["cli", "mcp", "hybrid"]
Health = Literal["healthy", "degraded", "unhealthy", "unknown"]


@dataclass(frozen=True)
class SkillSpec:
    source: str = ""
    layout: str = "multi"
    allowlist_prefix: str = ""
    min_cli_version: str = ""
    max_cli_version: str = ""


@dataclass(frozen=True)
class CliRuntimeSpec:
    executable: str
    package: str = ""
    registry: str = ""
    writable_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExposedCapabilities:
    cli_execution: bool = False
    mcp_tools: bool = False
    cli_auth_only: bool = False
    operation_preference: str = "mcp"


@dataclass(frozen=True)
class ConnectorManifest:
    connector_id: str
    display_name: str
    runtime: ConnectorRuntime
    cli: CliRuntimeSpec | None = None
    skills: SkillSpec | None = None
    exposed: ExposedCapabilities = field(default_factory=ExposedCapabilities)


@dataclass
class ConnectorStatus:
    connector_id: str
    display_name: str
    installed: bool = False
    authenticated: bool = False
    enabled: bool = False
    health: Health = "unknown"
    active_version: str = ""
    executable: str = ""
    app_id: str = ""
    app_name: str = ""
    account_name: str = ""
    corp_name: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        return asdict(self)
