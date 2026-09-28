"""Structured, fail-closed Bash planning and task-scoped capability grants."""

from __future__ import annotations

import hashlib
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

CommandRisk = Literal["verified_read", "unknown", "mutation", "high_risk"]
BashAction = Literal["allow", "ask", "deny"]

_READ_PROGRAMS = frozenset({"ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "which", "type", "dirname", "basename", "rg", "grep", "find", "git", "printenv", "env"})
_MUTATION_PROGRAMS = frozenset({"touch", "mkdir", "cp", "mv", "truncate", "tee"})
_NETWORK_PROGRAMS = frozenset({"curl", "wget", "ssh", "scp", "rsync", "nc"})
_HARD_PROGRAMS = frozenset({"mkfs", "dd", "shutdown", "reboot", "halt", "poweroff"})
_SENSITIVE_MARKERS = (".ssh", "id_rsa", "id_ed25519", ".aws", ".gnupg", "keychain")
_SYSTEM_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/system", "/library", "/dev")
_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_REDIRECT_TOKENS = frozenset({">", ">>", "<", "<<", "<<<", "2>", "2>>", "&>"})
_MAX_COMMAND_BYTES = 64 * 1024


@dataclass(frozen=True)
class CommandEffect:
    capability: str
    target: str = ""
    risk: str = "R0"
    known: bool = True
    reason: str = ""


@dataclass(frozen=True)
class CommandSegment:
    index: int
    argv: tuple[str, ...]
    executable: str = ""
    effects: tuple[CommandEffect, ...] = ()
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandPlan:
    command: str
    argv_groups: tuple[tuple[str, ...], ...]
    risk: CommandRisk
    segments: tuple[CommandSegment, ...] = ()
    controls: tuple[str, ...] = ()
    parse_status: Literal["complete", "partial", "invalid"] = "complete"
    digest: str = ""

    @property
    def grant_group(self) -> str:
        # Compatibility only. Unknown programs must never receive reusable v2 grants.
        return {"mutation": "command.mutation"}.get(self.risk, "")

    @property
    def can_run_without_shell(self) -> bool:
        return self.risk == "verified_read" and bool(self.argv_groups) and self.parse_status == "complete"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted({effect.capability for segment in self.segments for effect in segment.effects}))

    @property
    def unknowns(self) -> tuple[str, ...]:
        return tuple(item for segment in self.segments for item in segment.unknown)


@dataclass(frozen=True)
class BashEvaluation:
    plan: CommandPlan
    action: BashAction
    risk_level: str | None
    reason_codes: tuple[str, ...]
    grant_eligible: bool
    approval_choices: tuple[str, ...]
    model_layer: str = "rules"
    model_confidence: float | None = None

    def to_gate(self) -> dict[str, Any] | None:
        if self.action == "allow":
            return None
        detail = (
            f"Bash 计划{'被拒绝' if self.action == 'deny' else '需要确认'}。"
            f"风险：{self.risk_level or '未确定'}；依据：{', '.join(self.reason_codes)}。\n"
            + "\n".join(_segment_display(segment) for segment in self.plan.segments[:12])
        )
        return {
            "action": self.action,
            "reason": "scope_bash_high_risk" if self.risk_level in {"R3", "R4"} or self.action == "deny" else "scope_bash",
            "detail": detail,
            "domain": "runtime.bash",
            "approval_choices": ",".join(self.approval_choices),
            "plan_digest": self.plan.digest,
            "risk_level": str(self.risk_level or ""),
            "reason_codes": ",".join(self.reason_codes),
            "command_segments": [
                {
                    "index": segment.index,
                    "argv": list(segment.argv),
                    "effects": [
                        {
                            "capability": effect.capability,
                            "target": effect.target,
                            "risk": effect.risk,
                            "known": effect.known,
                            "reason": effect.reason,
                        }
                        for effect in segment.effects
                    ],
                    "unknown": list(segment.unknown),
                }
                for segment in self.plan.segments
            ],
        }


def classify_bash_command(command: str) -> CommandPlan:
    """Parse a conservative shell subset without evaluating dynamic syntax."""
    raw = (command or "").strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not raw or len(raw.encode("utf-8")) > _MAX_COMMAND_BYTES:
        return CommandPlan(raw, (), "unknown", parse_status="invalid", digest=digest)
    if any(marker in raw for marker in ("`", "$(", "${", "eval ", "source ")):
        return _partial_plan(raw, digest, "DYNAMIC_SHELL_SYNTAX")
    try:
        lexer = shlex.shlex(raw, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return CommandPlan(raw, (), "unknown", parse_status="invalid", digest=digest)
    if not tokens:
        return CommandPlan(raw, (), "unknown", parse_status="invalid", digest=digest)
    groups: list[list[str]] = [[]]
    controls: list[str] = []
    redirects: list[tuple[int, str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _CONTROL_TOKENS:
            if not groups[-1]:
                return _partial_plan(raw, digest, "INVALID_CONTROL_SEQUENCE")
            controls.append(token)
            groups.append([])
            i += 1
            continue
        if token in _REDIRECT_TOKENS or token.startswith((">", "<")):
            if not groups[-1] or i + 1 >= len(tokens):
                return _partial_plan(raw, digest, "UNRESOLVED_REDIRECTION")
            redirects.append((len(groups) - 1, token, tokens[i + 1]))
            i += 2
            continue
        if token in {"(", ")", "{", "}"}:
            return _partial_plan(raw, digest, "NESTED_SHELL_SYNTAX")
        groups[-1].append(token)
        i += 1
    if any(not group for group in groups):
        return _partial_plan(raw, digest, "INVALID_CONTROL_SEQUENCE")
    segments = tuple(_segment(index, tuple(group), tuple((op, target) for idx, op, target in redirects if idx == index)) for index, group in enumerate(groups))
    risks = [_legacy_risk(segment) for segment in segments]
    risk: CommandRisk = "high_risk" if "high_risk" in risks else "mutation" if "mutation" in risks else "verified_read" if risks and all(item == "verified_read" for item in risks) and all(control in {"&&", "||"} for control in controls) else "unknown"
    if _has_secret_upload_flow(segments, controls):
        segments = segments + (CommandSegment(len(segments), (), effects=(CommandEffect("network.upload", "", "R4", reason="SECRET_EXFILTRATION"),)),)
        risk = "high_risk"
    argv_groups = tuple(segment.argv for segment in segments) if risk == "verified_read" else ()
    return CommandPlan(raw, argv_groups, risk, segments, tuple(controls), "complete", digest)


def _partial_plan(raw: str, digest: str, unknown: str) -> CommandPlan:
    return CommandPlan(raw, (), "unknown", (CommandSegment(0, (), unknown=(unknown,)),), parse_status="partial", digest=digest)


def _segment(index: int, argv: tuple[str, ...], redirects: tuple[tuple[str, str], ...]) -> CommandSegment:
    program = Path(argv[0]).name if argv else ""
    effects: list[CommandEffect] = []
    unknown: list[str] = []
    targets = tuple(arg for arg in argv[1:] if not arg.startswith("-"))
    if program in _HARD_PROGRAMS:
        effects.append(CommandEffect("system.destructive", " ".join(targets), "R4", reason="HARD_DESTRUCTIVE_PROGRAM"))
    elif program == "rm":
        target = targets[-1] if targets else ""
        if target in {"/", "~", "."} or (target.startswith("/") and _is_system_path(target)):
            effects.append(CommandEffect("filesystem.delete", target, "R4", reason="DESTRUCTIVE_SYSTEM_TARGET"))
        else:
            known = not _dynamic_target(target)
            effects.append(CommandEffect("filesystem.delete", target, "R3", known, "FILE_DELETE" if known else "DYNAMIC_DELETE_TARGET"))
            if not known:
                unknown.append("DYNAMIC_DELETE_TARGET")
    elif program in _MUTATION_PROGRAMS:
        target = targets[-1] if targets else ""
        known = not _dynamic_target(target)
        capability = {
            "mkdir": "filesystem.directory.create",
            "touch": "filesystem.file.create",
        }.get(program, "filesystem.write")
        effects.append(CommandEffect(capability, target, "R2", known, "FILESYSTEM_MUTATION"))
        if not known:
            unknown.append("DYNAMIC_WRITE_TARGET")
    elif program == "git":
        _git_effects(argv, effects, unknown)
    elif program in _NETWORK_PROGRAMS:
        _network_effects(program, argv, effects, unknown)
    elif program in {"kill", "pkill", "killall"}:
        effects.append(CommandEffect("process.kill", " ".join(targets), "R3", reason="PROCESS_TERMINATION"))
    elif program in {"sudo", "su", "doas"}:
        effects.append(CommandEffect("privilege.escalate", "", "R3", reason="PRIVILEGE_ESCALATION"))
    elif program in {"echo", "printf", "true", "false", "test", "["}:
        effects.append(CommandEffect("shell.output", "", "R0", reason="SHELL_BUILTIN"))
    elif program in _READ_PROGRAMS:
        _read_effects(program, argv, effects, unknown)
    elif program in {"npm", "pnpm", "yarn", "pip", "uv", "poetry", "brew", "apt", "apt-get"}:
        _package_effects(argv, effects, unknown)
    else:
        effects.append(CommandEffect("process.execute", program, "R3", False, "UNKNOWN_PROGRAM"))
        unknown.append("UNKNOWN_PROGRAM")
    for operator, target in redirects:
        if operator.startswith(">") or operator == "&>":
            known = not _dynamic_target(target)
            effects.append(CommandEffect("filesystem.write", target, "R2", known, "SHELL_REDIRECTION"))
            if not known:
                unknown.append("DYNAMIC_REDIRECTION_TARGET")
        elif operator in {"<", "<<", "<<<"}:
            effects.append(CommandEffect("filesystem.read", target, "R0", not _dynamic_target(target), "SHELL_INPUT"))
    return CommandSegment(index, argv, program, tuple(effects), tuple(dict.fromkeys(unknown)))


def _git_effects(argv: tuple[str, ...], effects: list[CommandEffect], unknown: list[str]) -> None:
    if len(argv) < 2:
        unknown.append("GIT_SUBCOMMAND_MISSING")
        effects.append(CommandEffect("git.execute", "", "R3", False, "GIT_SUBCOMMAND_MISSING"))
        return
    subcommand = argv[1]
    if subcommand in {"status", "log", "diff", "show", "ls-files", "rev-parse", "blame", "grep", "check-ignore"} and "--ext-diff" not in argv and not any(arg.startswith("--output") for arg in argv):
        effects.append(CommandEffect("git.read", "", "R0", reason="GIT_READ"))
    elif subcommand in {"branch", "remote", "tag"} and not any(arg in {"-D", "-d", "--delete", "add", "set-url", "rename"} for arg in argv[2:]):
        effects.append(CommandEffect("git.read", "", "R0", reason="GIT_LIST"))
    elif subcommand in {"add", "mv", "rm", "stash"}:
        effects.append(CommandEffect("git.write", "", "R2", reason="GIT_WORKTREE_WRITE"))
    elif subcommand in {"restore", "checkout", "switch", "reset", "clean", "commit", "merge", "rebase", "push"}:
        effects.append(CommandEffect("git.destructive" if subcommand in {"restore", "reset", "clean"} else "git.write", "", "R3", reason=f"GIT_{subcommand.upper()}"))
    else:
        unknown.append("UNSUPPORTED_GIT_VARIANT")
        effects.append(CommandEffect("git.execute", subcommand, "R3", False, "UNSUPPORTED_GIT_VARIANT"))


def _network_effects(program: str, argv: tuple[str, ...], effects: list[CommandEffect], unknown: list[str]) -> None:
    joined = " ".join(argv).lower()
    upload = any(arg in {"-d", "--data", "--data-binary", "-F", "-T", "--upload-file"} or arg.startswith("--data=") for arg in argv)
    if upload and any(marker in joined for marker in _SENSITIVE_MARKERS):
        effects.append(CommandEffect("network.upload", "", "R4", reason="SECRET_EXFILTRATION"))
        return
    effects.append(CommandEffect("network.upload" if upload or program in {"scp", "rsync"} else "network.fetch", "", "R3" if upload else "R2", False, "NETWORK_OPERATION"))
    unknown.append("NETWORK_DESTINATION_OR_DATA")


def _read_effects(program: str, argv: tuple[str, ...], effects: list[CommandEffect], unknown: list[str]) -> None:
    if program == "find" and any(arg in {"-exec", "-execdir", "-delete", "-ok", "-okdir"} for arg in argv[1:]):
        unknown.append("FIND_EXEC")
        effects.append(CommandEffect("process.execute", "", "R3", False, "FIND_EXEC"))
        return
    if program == "rg" and "--pre" in argv:
        unknown.append("RG_PREPROCESSOR")
        effects.append(CommandEffect("process.execute", "", "R3", False, "RG_PREPROCESSOR"))
        return
    if program in {"env", "printenv"} and any(any(key in arg.lower() for key in ("token", "secret", "password", "key")) for arg in argv[1:]):
        effects.append(CommandEffect("secret.read", "", "R3", reason="SENSITIVE_ENVIRONMENT"))
        return
    for target in (arg for arg in argv[1:] if not arg.startswith("-")):
        if _is_sensitive_path(target):
            effects.append(CommandEffect("secret.read", target, "R3", reason="SENSITIVE_PATH"))
        elif _dynamic_target(target):
            unknown.append("DYNAMIC_READ_TARGET")
        else:
            effects.append(CommandEffect("filesystem.read", target, "R0", reason="READ_TARGET"))
    effects.append(CommandEffect("filesystem.read", "", "R0", reason="READ_ONLY_PROGRAM"))


def _package_effects(argv: tuple[str, ...], effects: list[CommandEffect], unknown: list[str]) -> None:
    if any(arg in {"install", "ci", "add", "sync"} for arg in argv[1:]):
        effects.extend((CommandEffect("filesystem.write", "project_environment", "R2", reason="PACKAGE_INSTALL"), CommandEffect("network.fetch", "package_registry", "R2", reason="PACKAGE_INSTALL")))
        if "--ignore-scripts" not in argv:
            unknown.append("PACKAGE_LIFECYCLE_SCRIPT")
            effects.append(CommandEffect("process.execute", "lifecycle_script", "R3", False, "PACKAGE_LIFECYCLE_SCRIPT"))
    else:
        unknown.append("UNSUPPORTED_PACKAGE_OPERATION")
        effects.append(CommandEffect("process.execute", argv[0], "R3", False, "UNSUPPORTED_PACKAGE_OPERATION"))


def _legacy_risk(segment: CommandSegment) -> CommandRisk:
    risks = {effect.risk for effect in segment.effects}
    if "R4" in risks:
        return "high_risk"
    if segment.unknown:
        return "unknown"
    if "R3" in risks:
        return "high_risk"
    if "R2" in risks:
        return "mutation"
    return "verified_read" if not segment.unknown else "unknown"


def _has_secret_upload_flow(segments: tuple[CommandSegment, ...], controls: list[str]) -> bool:
    """A pipe carries stdin from a sensitive read into a network uploader."""
    for index, control in enumerate(controls):
        if control != "|" or index + 1 >= len(segments):
            continue
        source = segments[index]
        destination = segments[index + 1]
        reads_secret = any(effect.capability == "secret.read" for effect in source.effects)
        uploads = any(effect.capability == "network.upload" for effect in destination.effects)
        if reads_secret and uploads:
            return True
    return False


def evaluate_bash(
    plan: CommandPlan,
    *,
    authorization: dict[str, Any] | None = None,
    danger_policy: str = "ask",
    workspace: Path | None = None,
    file_access: str = "ask",
) -> BashEvaluation:
    """L1 deterministic decision.  Model layers may never override a deny."""
    reasons = tuple(_reason_codes(plan))
    risks = [effect.risk for segment in plan.segments for effect in segment.effects]
    known_r3 = any(
        effect.risk == "R3" and effect.known
        for segment in plan.segments for effect in segment.effects
    )
    if plan.parse_status == "invalid":
        return BashEvaluation(plan, "deny", None, ("INVALID_INPUT",), False, ())
    if "R4" in risks:
        return BashEvaluation(plan, "deny", "R4", reasons or ("HARD_DENY",), False, ())
    if known_r3:
        if danger_policy == "deny":
            return BashEvaluation(plan, "deny", "R3", reasons + ("POLICY_DENY",), False, ())
        return BashEvaluation(plan, "ask", "R3", reasons, False, ("once",))
    external_targets = _external_targets(plan, workspace)
    if external_targets and file_access == "workspace":
        return BashEvaluation(plan, "deny", "R3", reasons + ("EXTERNAL_PATH",), False, ())
    if external_targets and file_access == "ask":
        return BashEvaluation(plan, "ask", "R3", reasons + ("EXTERNAL_PATH",), False, ("once",))
    if plan.parse_status != "complete" or plan.unknowns:
        return BashEvaluation(plan, "ask", None, reasons or ("UNRESOLVED_SYNTAX",), False, ("once",))
    if "R3" in risks:
        if danger_policy == "deny":
            return BashEvaluation(plan, "deny", "R3", reasons + ("POLICY_DENY",), False, ())
        return BashEvaluation(plan, "ask", "R3", reasons, False, ("once",))
    if "R2" in risks:
        if has_matching_grant(authorization or {}, plan):
            return BashEvaluation(plan, "allow", "R2", ("TASK_CAPABILITY_GRANT",), True, ())
        return BashEvaluation(plan, "ask", "R2", reasons or ("CAPABILITY_GRANT_REQUIRED",), True, ("once", "task_capability"))
    return BashEvaluation(plan, "allow", "R0", ("VERIFIED_READ_ONLY",), False, ())


def evaluate_bash_with_layers(
    plan: CommandPlan,
    *,
    authorization: dict[str, Any] | None = None,
    danger_policy: str = "ask",
    workspace: Path | None = None,
    file_access: str = "ask",
    usage_context: dict[str, Any] | None = None,
    semantic_decider: Callable[[dict[str, Any]], tuple[str, float] | None] | None = None,
) -> BashEvaluation:
    """Apply L1, then optional Jev and LLM advisory layers.

    L2/L3 are intentionally monotonic: they may add a deny but cannot turn an
    L1 ask into allow.  Only a fresh deterministic plan plus a matching task
    capability grant can authorize execution.
    """
    result = evaluate_bash(
        plan, authorization=authorization, danger_policy=danger_policy,
        workspace=workspace, file_access=file_access,
    )
    if result.action != "ask":
        return result
    state = {
        "action": "bash.execute",
        "parse_status": plan.parse_status,
        "controls": list(plan.controls),
        "effects": [
            {
                "capability": effect.capability,
                "target": effect.target,
                "risk": effect.risk,
                "known": effect.known,
                "reason": effect.reason,
            }
            for segment in plan.segments for effect in segment.effects
        ],
        "unknowns": list(plan.unknowns),
        "grant_eligible": result.grant_eligible,
        "authorization_present": has_matching_grant(authorization or {}, plan),
        "hard_constraints": {"allow_requires_complete_effects": True, "deny_is_final": True},
    }
    try:
        from server.runtime.typesafe_judgments import TypeSafeUnavailable, decide_bash_with_typesafe

        jev = decide_bash_with_typesafe(state, usage_context=usage_context)
        if jev.choice == "deny" and jev.confidence >= 0.90:
            return BashEvaluation(plan, "deny", result.risk_level, result.reason_codes + ("MODEL_REJECTED",), False, (), "jev", jev.confidence)
    except Exception as exc:  # Jev is advisory; unavailable must never expand permission.
        try:
            from server.runtime.typesafe_judgments import TypeSafeUnavailable, record_typesafe_unavailable

            if isinstance(exc, TypeSafeUnavailable):
                record_typesafe_unavailable("bash_permission", exc, usage_context)
        except Exception:
            pass
    semantic_relevant = {"DYNAMIC_SHELL_SYNTAX", "NESTED_SHELL_SYNTAX", "PACKAGE_LIFECYCLE_SCRIPT", "FIND_EXEC", "RG_PREPROCESSOR"}
    if semantic_decider is not None and semantic_relevant.intersection(plan.unknowns):
        try:
            semantic = semantic_decider(state)
            if semantic is not None:
                choice, confidence = semantic
                if choice == "deny" and confidence >= 0.90:
                    return BashEvaluation(plan, "deny", result.risk_level, result.reason_codes + ("SEMANTIC_MODEL_REJECTED",), False, (), "llm", confidence)
        except Exception:
            pass
    return result


def _reason_codes(plan: CommandPlan) -> list[str]:
    return list(dict.fromkeys([*(effect.reason for segment in plan.segments for effect in segment.effects if effect.reason), *plan.unknowns]))


def _segment_display(segment: CommandSegment) -> str:
    command = " ".join(segment.argv) if segment.argv else "<unparsed shell syntax>"
    effects = ", ".join(effect.capability for effect in segment.effects) or "unknown"
    return f"- {command}: {effects}"


def _dynamic_target(target: str) -> bool:
    return not target or target == ".." or target.startswith("../") or any(mark in target for mark in ("$", "`", "~", "*", "?", "["))


def _is_sensitive_path(target: str) -> bool:
    return any(marker in target.lower() for marker in _SENSITIVE_MARKERS)


def _is_system_path(target: str) -> bool:
    lower = target.lower()
    return any(lower == prefix or lower.startswith(prefix + "/") for prefix in _SYSTEM_PREFIXES)


def _external_targets(plan: CommandPlan, workspace: Path | None) -> tuple[str, ...]:
    if workspace is None:
        return ()
    root = workspace.resolve()
    outside: list[str] = []
    for effect in (effect for segment in plan.segments for effect in segment.effects):
        target = effect.target
        if not target.startswith("/"):
            continue
        try:
            Path(target).resolve().relative_to(root)
        except ValueError:
            outside.append(target)
    return tuple(dict.fromkeys(outside))


def task_command_authorization(context: dict[str, Any], *, workspace: Path, continuation: bool) -> dict[str, Any]:
    now = time.time()
    current = context.get("command_authorization")
    workspace_value = str(workspace.resolve())
    valid = continuation and isinstance(current, dict) and current.get("workspace") == workspace_value and float(current.get("expires_at") or 0) > now
    if not valid:
        current = {"task_id": f"command_task_{uuid.uuid4().hex[:12]}", "workspace": workspace_value, "grants": [], "capability_grants": [], "created_at": now, "expires_at": now + 3600}
        context["command_authorization"] = current
    current.setdefault("capability_grants", [])
    return current


def grant_capabilities(context: dict[str, Any], plan: CommandPlan) -> None:
    if not context or plan.unknowns or any(effect.risk not in {"R0", "R2"} for segment in plan.segments for effect in segment.effects):
        return
    workspace = str(context.get("workspace") or "")
    effects = _grant_effects(plan, workspace)
    if not effects:
        return
    grant = {
        "id": f"bash_grant_{uuid.uuid4().hex[:12]}",
        "workspace": workspace,
        "capabilities": list(plan.capabilities),
        "effects": effects,
        "expires_at": min(float(context.get("expires_at") or 0), time.time() + 3600),
    }
    grants = list(context.get("capability_grants") or [])
    if not any(
        isinstance(item, dict)
        and item.get("workspace") == grant["workspace"]
        and item.get("effects") == grant["effects"]
        for item in grants
    ):
        grants.append(grant)
    context["capability_grants"] = grants


def has_matching_grant(context: dict[str, Any], plan: CommandPlan) -> bool:
    workspace = str(context.get("workspace") or "")
    required = _grant_effects(plan, workspace)
    if not required or plan.unknowns:
        return False
    for grant in context.get("capability_grants") or []:
        if not (
            isinstance(grant, dict)
            and grant.get("workspace") == workspace
            and float(grant.get("expires_at") or 0) > time.time()
        ):
            continue
        granted_effects = [item for item in grant.get("effects") or [] if isinstance(item, dict)]
        if all(any(_effect_is_within_grant(effect, granted) for granted in granted_effects) for effect in required):
            return True
    return False


def _grant_effects(plan: CommandPlan, workspace: str) -> list[dict[str, str]]:
    """Freeze each approved capability and its normalized resource target."""
    root = Path(workspace).resolve() if workspace else None
    effects: list[dict[str, str]] = []
    for effect in (item for segment in plan.segments for item in segment.effects):
        if effect.risk not in {"R0", "R2"}:
            return []
        if effect.risk != "R2":
            continue
        target = effect.target
        if (
            root is not None
            and target
            and effect.capability.startswith("filesystem.")
            and not target.startswith("/")
        ):
            target = str((root / target).resolve())
        effects.append({"capability": effect.capability, "target": target})
    return sorted(effects, key=lambda item: (item["capability"], item["target"]))


def _effect_is_within_grant(required: dict[str, str], granted: dict[str, str]) -> bool:
    if required.get("capability") != granted.get("capability"):
        return False
    requested_target = str(required.get("target") or "")
    granted_target = str(granted.get("target") or "")
    if requested_target == granted_target:
        return True
    # A task-level directory grant includes only that directory's descendants.
    if granted.get("capability") != "filesystem.directory.create":
        return False
    try:
        Path(requested_target).resolve().relative_to(Path(granted_target).resolve())
        return True
    except ValueError:
        return False
