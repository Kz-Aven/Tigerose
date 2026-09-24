"""Built-in connector manifests. Marketplace delivery can extend this catalog later."""

from __future__ import annotations

from server.connectors.contracts import (
    CliRuntimeSpec,
    ConnectorManifest,
    ExposedCapabilities,
    SkillSpec,
)

_MANIFESTS = {
    "dingtalk": ConnectorManifest(
        connector_id="dingtalk",
        display_name="钉钉",
        runtime="cli",
        cli=CliRuntimeSpec(
            executable="dws",
            package="dingtalk-workspace-cli",
            registry="https://registry.npmmirror.com",
            writable_paths=("~/.dws",),
        ),
        skills=SkillSpec(
            source="cli-bundle",
            layout="multi",
            allowlist_prefix="dingtalk-",
            min_cli_version="1.0.7",
        ),
        exposed=ExposedCapabilities(cli_execution=True, operation_preference="cli"),
    ),
    "lark": ConnectorManifest(
        connector_id="lark",
        display_name="飞书",
        runtime="cli",
        cli=CliRuntimeSpec(
            executable="lark-cli",
            package="@larksuite/cli",
            registry="https://registry.npmjs.org",
        ),
        skills=SkillSpec(
            source="cli-bundle",
            layout="flat",
            allowlist_prefix="lark-",
        ),
        exposed=ExposedCapabilities(cli_execution=True, operation_preference="cli"),
    ),
}


def get_manifest(connector_id: str) -> ConnectorManifest | None:
    return _MANIFESTS.get((connector_id or "").strip())


def list_manifests() -> list[ConnectorManifest]:
    return list(_MANIFESTS.values())
