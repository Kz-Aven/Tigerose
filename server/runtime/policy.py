"""Assistant permission policy (two-axis) from config_meta."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FILE_ACCESS_WORKSPACE = "workspace"
FILE_ACCESS_ASK = "ask"
FILE_ACCESS_FULL = "full"

DANGER_DENY = "deny"
DANGER_ASK = "ask"

DEFAULT_DANGER_RULES: tuple[str, ...] = ("dangerous_bash",)

PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "excel_read",
        "excel_write",
    }
)


@dataclass(frozen=True)
class PermissionPolicy:
    file_access: str = FILE_ACCESS_ASK
    danger_policy: str = DANGER_DENY
    danger_rules: tuple[str, ...] = field(default_factory=lambda: DEFAULT_DANGER_RULES)

    def monitors_dangerous_bash(self) -> bool:
        return "dangerous_bash" in self.danger_rules


def parse_permission_policy(meta: Any) -> PermissionPolicy:
    if not isinstance(meta, dict):
        meta = {}
    fa = str(meta.get("file_access") or FILE_ACCESS_ASK).strip().lower()
    if fa not in (FILE_ACCESS_WORKSPACE, FILE_ACCESS_ASK, FILE_ACCESS_FULL):
        fa = FILE_ACCESS_ASK
    dp = str(meta.get("danger_policy") or DANGER_DENY).strip().lower()
    if dp not in (DANGER_DENY, DANGER_ASK):
        dp = DANGER_DENY
    raw_rules = meta.get("danger_rules")
    if raw_rules is None:
        rules: tuple[str, ...] = DEFAULT_DANGER_RULES
    elif isinstance(raw_rules, (list, tuple)):
        rules = tuple(str(x).strip() for x in raw_rules if str(x).strip())
    else:
        rules = DEFAULT_DANGER_RULES
    return PermissionPolicy(file_access=fa, danger_policy=dp, danger_rules=rules)


def policy_to_meta_patch(
    *,
    file_access: str | None = None,
    danger_policy: str | None = None,
    danger_rules: list[str] | None = None,
) -> dict[str, Any]:
    """Validate and return keys to merge into config_meta."""
    out: dict[str, Any] = {}
    if file_access is not None:
        fa = str(file_access).strip().lower()
        if fa not in (FILE_ACCESS_WORKSPACE, FILE_ACCESS_ASK, FILE_ACCESS_FULL):
            raise ValueError("file_access must be workspace|ask|full")
        out["file_access"] = fa
    if danger_policy is not None:
        dp = str(danger_policy).strip().lower()
        if dp not in (DANGER_DENY, DANGER_ASK):
            raise ValueError("danger_policy must be deny|ask")
        out["danger_policy"] = dp
    if danger_rules is not None:
        if not isinstance(danger_rules, list):
            raise ValueError("danger_rules must be a list")
        out["danger_rules"] = [str(x).strip() for x in danger_rules if str(x).strip()]
    return out
