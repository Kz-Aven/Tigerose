"""Connector lifecycle manager. Individual adapters never leak into turn runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from server.connectors.catalog import get_manifest, list_manifests
from server.connectors.contracts import ConnectorStatus
from server.connectors.store import config_dir, connector_root, installs_root, load_state, save_state

_lock = threading.Lock()
_operations: dict[str, str] = {}
_LARK_MESSAGE_SEND_SCOPE = "im:message.send_as_user"


def _dws_paths(connector_id: str, version: str) -> tuple[Path, Path]:
    root = installs_root(connector_id) / version
    return root / "node_modules" / ".bin" / "dws", root / "skills"


def _lark_paths(version: str) -> tuple[Path, Path]:
    root = installs_root("lark") / version
    return root / "node_modules" / ".bin" / "lark-cli", root / "skills"


def _status_from_state(connector_id: str) -> ConnectorStatus:
    manifest = get_manifest(connector_id)
    if not manifest:
        raise ValueError(f"unknown connector: {connector_id}")
    state = load_state(connector_id)
    with _lock:
        operation = _operations.get(connector_id, "")
    if connector_id == "dingtalk" and state.get("installed") and not operation:
        state = _refresh_dingtalk_auth_state(state)
    elif connector_id == "lark" and state.get("installed") and not operation:
        state = _refresh_lark_auth_state(state)
    return ConnectorStatus(
        connector_id=connector_id,
        display_name=manifest.display_name,
        installed=bool(state.get("installed")),
        authenticated=bool(state.get("authenticated")),
        health="degraded" if operation else str(state.get("health") or "unknown"),
        active_version=str(state.get("active_version") or ""),
        executable=str(state.get("executable") or ""),
        app_id=str(state.get("app_id") or ""),
        app_name=str(state.get("app_name") or ""),
        account_name=str(state.get("account_name") or ""),
        corp_name=str(state.get("corp_name") or ""),
        message=operation or str(state.get("message") or ""),
    )


def _set_operation(connector_id: str, value: str) -> None:
    with _lock:
        if value:
            _operations[connector_id] = value
        else:
            _operations.pop(connector_id, None)


def _connector_env(connector_id: str) -> dict[str, str]:
    env = dict(os.environ)
    if connector_id == "dingtalk":
        env["DWS_CONFIG_DIR"] = str(config_dir(connector_id))
    elif connector_id == "lark":
        env["LARKSUITE_CLI_CONFIG_DIR"] = str(config_dir(connector_id))
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def _json_output(executable: Path, args: list[str], timeout: int = 30) -> dict[str, Any]:
    result = subprocess.run(
        [str(executable), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_connector_env("dingtalk"),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "命令失败").strip())
    payload = json.loads(result.stdout or "{}")
    return payload if isinstance(payload, dict) else {}


def _refresh_dingtalk_auth_state(state: dict[str, Any]) -> dict[str, Any]:
    executable = Path(str(state.get("executable") or ""))
    if not executable.is_file():
        return state
    try:
        payload = _json_output(executable, ["auth", "status", "--format", "json"], timeout=5)
    except (RuntimeError, subprocess.SubprocessError):
        return state
    authenticated = bool(payload.get("authenticated"))
    changed = authenticated != bool(state.get("authenticated"))
    if authenticated:
        account_name = str(payload.get("user_name") or "")
        corp_name = str(payload.get("corp_name") or "")
        changed = changed or account_name != str(state.get("account_name") or "")
        changed = changed or corp_name != str(state.get("corp_name") or "")
        state.update(
            {
                "authenticated": True,
                "health": "healthy",
                "account_name": account_name,
                "corp_name": corp_name,
                "message": "",
            }
        )
    if changed:
        save_state("dingtalk", state)
    return state


def _lark_json_output(executable: Path, args: list[str], timeout: int = 30) -> dict[str, Any]:
    result = subprocess.run(
        [str(executable), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_connector_env("lark"),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "命令失败").strip())
    payload = json.loads(result.stdout or "{}")
    return payload if isinstance(payload, dict) else {}


def _refresh_lark_auth_state(state: dict[str, Any]) -> dict[str, Any]:
    executable = Path(str(state.get("executable") or ""))
    if not executable.is_file():
        return state
    try:
        payload = _lark_json_output(executable, ["auth", "status", "--json", "--verify"], timeout=15)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
        return state
    identities = payload.get("identities") if isinstance(payload.get("identities"), dict) else {}
    user = identities.get("user") if isinstance(identities.get("user"), dict) else {}
    bot = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
    authenticated = bool(user.get("available")) and payload.get("verified") is not False
    account_name = str(user.get("userName") or "")
    brand = str(payload.get("brand") or "")
    app_id = str(payload.get("appId") or "")
    app_name = str(bot.get("appName") or "")
    changed = (
        authenticated != bool(state.get("authenticated"))
        or account_name != str(state.get("account_name") or "")
        or brand != str(state.get("corp_name") or "")
        or app_id != str(state.get("app_id") or "")
        or app_name != str(state.get("app_name") or "")
    )
    state.update(
        {
            "authenticated": authenticated,
            "configured": bool(payload.get("appId")),
            "app_id": app_id,
            "app_name": app_name,
            "health": "healthy" if authenticated else "unknown",
            "account_name": account_name,
            "corp_name": brand,
            "message": "" if authenticated else str(user.get("message") or payload.get("note") or ""),
        }
    )
    if changed:
        save_state("lark", state)
    return state


def _copy_dws_skills(package_root: Path, destination: Path) -> None:
    source = package_root / "share" / "skills" / "multi"
    if not source.is_dir():
        source = package_root / "skills" / "multi"
    if not source.is_dir():
        raise RuntimeError("钉钉 CLI 包未包含 multi Skills，安装未提交")
    shutil.copytree(source, destination, dirs_exist_ok=False)


def _lark_skill_path(destination: Path, skill_path: str) -> Path:
    relative = Path(skill_path)
    if not skill_path or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError(f"飞书 CLI 返回了无效的 Skill 路径：{skill_path}")
    root = destination.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise RuntimeError(f"飞书 CLI 返回了越界的 Skill 路径：{skill_path}")
    return target


def _export_lark_skills(executable: Path, destination: Path) -> None:
    listing = _lark_json_output(executable, ["skills", "list"], timeout=30)
    skills = listing.get("skills")
    if not isinstance(skills, list) or not skills:
        raise RuntimeError("飞书 CLI 未提供内置 Skills，安装未提交")

    destination.mkdir(parents=True, exist_ok=False)

    def export_directory(skill_path: str) -> None:
        entries = _lark_json_output(executable, ["skills", "list", skill_path], timeout=30).get("entries")
        if not isinstance(entries, list):
            raise RuntimeError(f"飞书 CLI 未返回 Skill 目录内容：{skill_path}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(f"飞书 CLI 返回了无效的 Skill 目录项：{skill_path}")
            path = entry.get("path")
            if not isinstance(path, str):
                raise RuntimeError(f"飞书 CLI 返回了无效的 Skill 路径：{skill_path}")
            target = _lark_skill_path(destination, path)
            if entry.get("is_dir"):
                target.mkdir(parents=True, exist_ok=True)
                export_directory(path)
                continue
            content = _lark_json_output(executable, ["skills", "read", path, "--json"], timeout=30).get("content")
            if not isinstance(content, str):
                raise RuntimeError(f"飞书 CLI 未返回 Skill 文件内容：{path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    for skill in skills:
        name = skill.get("name") if isinstance(skill, dict) else None
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise RuntimeError("飞书 CLI 返回了无效的 Skill 名称")
        _lark_skill_path(destination, name).mkdir(parents=True, exist_ok=True)
        export_directory(name)


def _install_dingtalk() -> None:
    connector_id = "dingtalk"
    manifest = get_manifest(connector_id)
    assert manifest and manifest.cli
    _set_operation(connector_id, "installing")
    staging = installs_root(connector_id) / f"staging-{int(time.time())}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("未找到 npm，无法安装钉钉 CLI")
        install_env = {
            **os.environ,
            "HOME": str(staging / "npm-home"),
            "DWS_CONFIG_DIR": str(staging / "dws-config"),
            "HERMES_HOME": str(staging / "hermes-home"),
        }
        installed = subprocess.run(
            [npm, "install", "--prefix", str(staging), manifest.cli.package, f"--registry={manifest.cli.registry}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=install_env,
        )
        executable = staging / "node_modules" / ".bin" / manifest.cli.executable
        if installed.returncode != 0 or not executable.is_file():
            raise RuntimeError((installed.stderr or installed.stdout or "未找到 dws 可执行文件").strip())
        package_root = staging / "node_modules" / manifest.cli.package
        package_meta = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        version = str(package_meta.get("version") or "unknown")
        _copy_dws_skills(package_root, staging / "skills")
        final = installs_root(connector_id) / version
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.rmtree(staging)
        else:
            staging.replace(final)
        active_executable, skills_dir = _dws_paths(connector_id, version)
        state = load_state(connector_id)
        state.update(
            {
                "installed": True,
                "authenticated": False,
                "health": "healthy",
                "active_version": version,
                "executable": str(active_executable),
                "skills_dir": str(skills_dir),
                "message": "",
            }
        )
        save_state(connector_id, state)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        state = load_state(connector_id)
        state.update({"health": "unhealthy", "message": f"安装失败：{exc}"})
        save_state(connector_id, state)
    finally:
        _set_operation(connector_id, "")


def _install_lark() -> None:
    connector_id = "lark"
    manifest = get_manifest(connector_id)
    assert manifest and manifest.cli
    _set_operation(connector_id, "installing")
    staging = installs_root(connector_id) / f"staging-{int(time.time())}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("未找到 npm，无法安装飞书 CLI")
        installed = subprocess.run(
            [npm, "install", "--prefix", str(staging), manifest.cli.package, f"--registry={manifest.cli.registry}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={**os.environ, "HOME": str(staging / "npm-home")},
        )
        executable = staging / "node_modules" / ".bin" / manifest.cli.executable
        if installed.returncode != 0 or not executable.is_file():
            raise RuntimeError((installed.stderr or installed.stdout or "未找到 lark-cli 可执行文件").strip())
        package_root = staging / "node_modules" / manifest.cli.package
        package_meta = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        version = str(package_meta.get("version") or "unknown")
        _export_lark_skills(executable, staging / "skills")
        final = installs_root(connector_id) / version
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.rmtree(staging)
        else:
            staging.replace(final)
        active_executable, skills_dir = _lark_paths(version)
        state = load_state(connector_id)
        state.update(
            {
                "installed": True,
                "authenticated": False,
                "configured": False,
                "health": "healthy",
                "active_version": version,
                "executable": str(active_executable),
                "skills_dir": str(skills_dir),
                "message": "",
            }
        )
        save_state(connector_id, state)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        state = load_state(connector_id)
        state.update({"health": "unhealthy", "message": f"安装失败：{exc}"})
        save_state(connector_id, state)
    finally:
        _set_operation(connector_id, "")


def install(connector_id: str) -> ConnectorStatus:
    if connector_id not in {"dingtalk", "lark"} or not get_manifest(connector_id):
        raise ValueError(f"unsupported connector: {connector_id}")
    current = _status_from_state(connector_id)
    if current.installed or _operations.get(connector_id):
        return current
    _set_operation(connector_id, "installing")
    target = _install_dingtalk if connector_id == "dingtalk" else _install_lark
    threading.Thread(target=target, name=f"connector-install-{connector_id}", daemon=True).start()
    return _status_from_state(connector_id)


def _install_then_connect_dingtalk() -> None:
    _install_dingtalk()
    if _status_from_state("dingtalk").installed:
        _connect_dingtalk()


def _open_browser(url: str) -> None:
    if url.startswith(("https://", "http://")):
        webbrowser.open(url, new=2)


def _run_lark_config_init(executable: Path) -> None:
    process = subprocess.Popen(
        [str(executable), "config", "init", "--new"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_connector_env("lark"),
    )
    output: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            for value in line.split():
                if value.startswith(("https://", "http://")):
                    _open_browser(value)
        if process.wait(timeout=600) != 0:
            raise RuntimeError("".join(output).strip() or "飞书应用初始化失败")
    finally:
        if process.poll() is None:
            process.terminate()


def _authorize_lark_scopes(executable: Path, scopes: list[str]) -> None:
    requested = sorted({scope.strip() for scope in scopes if scope.strip()})
    if not requested:
        return
    process: subprocess.Popen | None = None
    try:
        login = _lark_json_output(
            executable,
            ["auth", "login", "--scope", " ".join(requested), "--no-wait", "--json"],
            timeout=30,
        )
        verification_url = str(login.get("verification_url") or "")
        device_code = str(login.get("device_code") or "")
        if not verification_url or not device_code:
            raise RuntimeError("飞书授权未返回浏览器链接")
        _open_browser(verification_url)
        process = subprocess.Popen(
            [str(executable), "auth", "login", "--device-code", device_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_connector_env("lark"),
        )
        if process.wait(timeout=600) != 0:
            raise RuntimeError("浏览器授权未完成")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("授权超时，请重新尝试") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()


def authorize_lark_scopes(scopes: list[str]) -> None:
    state = load_state("lark")
    executable = Path(str(state.get("executable") or ""))
    if not executable.is_file():
        raise RuntimeError("飞书 CLI 未安装")
    _authorize_lark_scopes(executable, scopes)
    if not _refresh_lark_auth_state(state).get("authenticated"):
        raise RuntimeError("飞书授权未完成")


def _connect_lark() -> None:
    connector_id = "lark"
    _set_operation(connector_id, "authorizing")
    process: subprocess.Popen | None = None
    try:
        state = load_state(connector_id)
        executable = Path(str(state.get("executable") or ""))
        if not executable.is_file():
            raise RuntimeError("飞书 CLI 未安装")
        try:
            status_payload = _lark_json_output(executable, ["auth", "status", "--json"], timeout=15)
        except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
            status_payload = {}
        if not status_payload.get("appId"):
            raise RuntimeError("飞书应用未绑定，请在连接器设置中选择“更换应用”")
        login = _lark_json_output(
            executable,
            ["auth", "login", "--scope", _LARK_MESSAGE_SEND_SCOPE, "--no-wait", "--json"],
            timeout=30,
        )
        verification_url = str(login.get("verification_url") or "")
        device_code = str(login.get("device_code") or "")
        if not verification_url or not device_code:
            raise RuntimeError("飞书授权未返回浏览器链接")
        _open_browser(verification_url)
        process = subprocess.Popen(
            [str(executable), "auth", "login", "--device-code", device_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_connector_env(connector_id),
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state = _refresh_lark_auth_state(state)
            if state.get("authenticated"):
                return
            if process.poll() is not None:
                raise RuntimeError("浏览器授权未完成")
            time.sleep(1)
        raise RuntimeError("授权超时，请重新尝试")
    except Exception as exc:  # noqa: BLE001
        state = load_state(connector_id)
        state.update({"authenticated": False, "health": "unhealthy", "message": f"授权失败：{exc}"})
        save_state(connector_id, state)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
        _set_operation(connector_id, "")


def _install_then_connect_lark() -> None:
    _install_lark()
    if _status_from_state("lark").installed:
        state = load_state("lark")
        if state.get("app_id"):
            _connect_lark()


def reconnect(connector_id: str) -> ConnectorStatus:
    if connector_id != "lark" or not get_manifest(connector_id):
        return connect(connector_id)
    current = _status_from_state(connector_id)
    if not current.installed or _operations.get(connector_id):
        return current
    _set_operation(connector_id, "authorizing")
    threading.Thread(target=_connect_lark, name="connector-lark-reauthorize", daemon=True).start()
    return _status_from_state(connector_id)


def _connect_dingtalk() -> None:
    connector_id = "dingtalk"
    _set_operation(connector_id, "authorizing")
    process: subprocess.Popen | None = None
    try:
        state = load_state(connector_id)
        executable = Path(str(state.get("executable") or ""))
        if not executable.is_file():
            raise RuntimeError("钉钉 CLI 未安装")
        process = subprocess.Popen(
            [str(executable), "auth", "login"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_connector_env(connector_id),
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try:
                payload = _json_output(executable, ["auth", "status", "--format", "json"])
            except (RuntimeError, subprocess.SubprocessError):
                payload = {}
            if payload.get("authenticated"):
                state.update(
                    {
                        "authenticated": True,
                        "health": "healthy",
                        "account_name": str(payload.get("user_name") or ""),
                        "corp_name": str(payload.get("corp_name") or ""),
                        "message": "",
                    }
                )
                save_state(connector_id, state)
                return
            if process.poll() is not None:
                raise RuntimeError("授权窗口已关闭，但未完成授权")
            time.sleep(1)
        raise RuntimeError("授权超时，请重新尝试")
    except Exception as exc:  # noqa: BLE001
        state = load_state(connector_id)
        state.update({"authenticated": False, "health": "unhealthy", "message": f"授权失败：{exc}"})
        save_state(connector_id, state)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
        _set_operation(connector_id, "")


def connect(connector_id: str) -> ConnectorStatus:
    if connector_id not in {"dingtalk", "lark"} or not get_manifest(connector_id):
        raise ValueError(f"unsupported connector: {connector_id}")
    current = _status_from_state(connector_id)
    if not current.installed:
        _set_operation(connector_id, "installing")
        target = _install_then_connect_dingtalk if connector_id == "dingtalk" else _install_then_connect_lark
        threading.Thread(target=target, name=f"connector-install-connect-{connector_id}", daemon=True).start()
        return _status_from_state(connector_id)
    if current.authenticated or _operations.get(connector_id):
        return current
    _set_operation(connector_id, "authorizing")
    target = _connect_dingtalk if connector_id == "dingtalk" else _connect_lark
    threading.Thread(target=target, name=f"connector-auth-{connector_id}", daemon=True).start()
    return _status_from_state(connector_id)


def disconnect(connector_id: str) -> ConnectorStatus:
    if connector_id not in {"dingtalk", "lark"} or not get_manifest(connector_id):
        raise ValueError(f"unsupported connector: {connector_id}")
    state = load_state(connector_id)
    executable = Path(str(state.get("executable") or ""))
    if executable.is_file():
        subprocess.run(
            [str(executable), "auth", "logout", *( ["--json"] if connector_id == "lark" else [] )],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_connector_env(connector_id),
        )
    state.update({"authenticated": False, "health": "unknown", "account_name": "", "corp_name": ""})
    save_state(connector_id, state)
    return _status_from_state(connector_id)


def uninstall(connector_id: str) -> ConnectorStatus:
    if connector_id not in {"dingtalk", "lark"} or not get_manifest(connector_id):
        raise ValueError(f"unsupported connector: {connector_id}")
    if _operations.get(connector_id):
        return _status_from_state(connector_id)
    _set_operation(connector_id, "uninstalling")
    try:
        disconnect(connector_id)
        shutil.rmtree(connector_root(connector_id), ignore_errors=True)
    finally:
        _set_operation(connector_id, "")
    return _status_from_state(connector_id)


def status(connector_id: str) -> ConnectorStatus:
    return _status_from_state(connector_id)


def list_statuses() -> list[ConnectorStatus]:
    return [status(manifest.connector_id) for manifest in list_manifests()]
