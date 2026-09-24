"""IM bot registration, routing, and reply delivery.

Adapters never receive an assistant id from a platform event.  The bot record is
the authority for ownership, and the conversation mapping owns the session id.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from server.db import repos

from . import secrets
from .media import attachment_from_path, save_media, text_summary

log = logging.getLogger("avent.im_channels")
_PLATFORMS = frozenset({"feishu", "dingtalk", "wecom"})
_DINGTALK_REGISTRATION_BASE_URL = "https://oapi.dingtalk.com"


def _mask(value: str) -> str:
    value = value or ""
    return value if len(value) <= 8 else f"{value[:4]}{'*' * 8}{value[-4:]}"


def _qr_image(url: str) -> str:
    try:
        import qrcode

        image = qrcode.make(url)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception as exc:
        raise RuntimeError("二维码组件不可用，请重新安装 Avent") from exc


def _content_text(value: Any) -> str:
    """Extract human-readable text from a provider rich-message payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    direct = value.get("text") or value.get("content")
    if isinstance(direct, str):
        return direct
    parts = [str(value[key]) for key in ("title", "url", "chat_id", "user_id") if isinstance(value.get(key), str)]
    for key in ("content", "elements", "zh_cn", "en_us"):
        nested = value.get(key)
        if nested is not None:
            text = _content_text(nested)
            if text:
                parts.append(text)
    return "\n".join(dict.fromkeys(parts))


def _resource_keys(value: Any) -> list[tuple[str, str, str]]:
    """Return (file_key, resource_type, suggested_name) from nested IM content."""
    found: list[tuple[str, str, str]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_resource_keys(item))
        return found
    if not isinstance(value, dict):
        return found
    name = str(value.get("file_name") or "")
    image_key = value.get("image_key")
    if isinstance(image_key, str) and image_key:
        found.append((image_key, "image", name or "image"))
    file_key = value.get("file_key")
    if isinstance(file_key, str) and file_key:
        found.append((file_key, "", name or "attachment"))
    for child in value.values():
        if isinstance(child, (dict, list)):
            found.extend(_resource_keys(child))
    return found


def _download_codes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [code for item in value for code in _download_codes(item)]
    if not isinstance(value, dict):
        return []
    codes = [str(value[key]) for key in ("downloadCode", "download_code") if value.get(key)]
    for child in value.values():
        if isinstance(child, (dict, list)):
            codes.extend(_download_codes(child))
    return codes


@dataclass
class Registration:
    registration_id: str
    platform: str
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    state: str = "starting"
    qr_image: str = ""
    expires_in: int | None = None
    bot_id: str = ""
    display_name: str = ""
    error: str = ""
    message: str = ""
    created_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, Any]:
        with self.lock:
            data = {"id": self.registration_id, "platform": self.platform, "state": self.state}
            if self.qr_image:
                data["qr_image"] = self.qr_image
            if self.expires_in:
                data["expires_in"] = self.expires_in
            if self.state == "success":
                data.update({"bot_id": self.bot_id, "display_name": self.display_name})
            if self.error:
                data["error"] = self.error
            if self.message:
                data["message"] = self.message
            return data


class ImChannelManager:
    def __init__(self) -> None:
        self._registrations: dict[str, Registration] = {}
        self._reg_lock = threading.Lock()
        self._feishu_started: set[str] = set()
        self._wecom_processes: dict[str, subprocess.Popen[str]] = {}
        self._wecom_lock = threading.Lock()
        self._dingtalk_lock = threading.Lock()
        self._dingtalk_started: set[str] = set()
        self._dingtalk_clients: dict[str, Any] = {}
        self._dingtalk_reply_targets: dict[tuple[str, str], tuple[Any, Any]] = {}

    def list_channels(self, platform: str | None = None) -> list[dict]:
        self._assert_platform(platform) if platform else None
        return repos.list_im_channels(platform)

    def list_assistant_bots(self, template_id: str) -> dict:
        return repos.list_im_bots_for_assistant(template_id)

    def start_registration(self, platform: str) -> dict:
        self._assert_platform(platform)
        registration = Registration(registration_id=uuid.uuid4().hex, platform=platform)
        with self._reg_lock:
            self._registrations[registration.registration_id] = registration
        worker = {
            "feishu": self._register_feishu,
            "wecom": self._register_wecom,
            "dingtalk": self._register_dingtalk,
        }[platform]
        threading.Thread(target=worker, args=(registration,), daemon=True, name=f"im-register-{platform}").start()
        return registration.public()

    def registration(self, registration_id: str) -> dict | None:
        registration = self._registrations.get(registration_id)
        return registration.public() if registration else None

    def refresh_registration(self, registration_id: str) -> dict | None:
        old = self._registrations.get(registration_id)
        if not old:
            return None
        old.cancelled.set()
        return self.start_registration(old.platform)

    def cancel_registration(self, registration_id: str) -> bool:
        registration = self._registrations.get(registration_id)
        if not registration:
            return False
        registration.cancelled.set()
        with registration.lock:
            if registration.state not in {"success", "error"}:
                registration.state = "cancelled"
        return True

    def bind_bot(self, bot_id: str, template_id: str | None) -> dict:
        bot = repos.bind_im_bot(bot_id, template_id)
        if not bot:
            raise ValueError("机器人或助理不存在")
        if template_id:
            self.start_bot(bot_id)
        return bot

    def delete_bot(self, bot_id: str) -> bool:
        bot = repos.delete_im_bot(bot_id)
        if not bot:
            return False
        ref = str(bot.get("secret_ref") or "")
        if ref:
            secrets.remove(ref)
        self._feishu_started.discard(bot_id)
        self._stop_wecom_bot(bot_id)
        self._stop_dingtalk_bot(bot_id)
        return True

    def start_all(self) -> None:
        for app in repos.list_im_channels():
            for bot in app.get("bots") or []:
                if bot.get("template_id"):
                    self.start_bot(str(bot["bot_id"]))

    def start_bot(self, bot_id: str) -> None:
        bot = repos.get_im_bot(bot_id, include_secret_ref=True)
        if not bot or not bot.get("template_id"):
            return
        platform = str(bot["platform"])
        try:
            if platform == "feishu":
                self._start_feishu_bot(bot)
            elif platform == "wecom":
                self._start_wecom_bot(bot)
            elif platform == "dingtalk":
                self._start_dingtalk_bot(bot)
            else:
                # Registration is real for all platforms. Runtime SDK startup is
                # isolated so a missing optional SDK never affects other bots.
                repos.set_im_bot_health(bot_id, "stopped", f"{platform} 机器人运行组件未安装")
        except Exception as exc:
            repos.set_im_bot_health(bot_id, "error", str(exc))
            log.exception("failed to start %s bot %s", platform, bot_id)

    def handle_inbound(
        self,
        *,
        platform: str,
        provider_bot_id: str,
        provider_message_id: str,
        kind: str,
        remote_conversation_id: str,
        text: str,
        attachments: list[dict] | None = None,
        display_name: str = "",
        reply_target: dict | None = None,
    ) -> None:
        self._assert_platform(platform)
        if kind not in {"direct", "group"}:
            raise ValueError("invalid IM conversation kind")
        bot = repos.get_im_bot_by_provider(platform, provider_bot_id)
        if not bot or not bot.get("template_id"):
            log.warning("IM inbound dropped: unbound %s bot=%s", platform, provider_bot_id)
            return
        conversation = repos.get_im_conversation(str(bot["bot_id"]), kind, remote_conversation_id)
        if not conversation:
            from server.api import assistant_session_ops as sessions

            title = f"{self._platform_label(platform)} {display_name or ('群聊' if kind == 'group' else '私聊')}"
            session = sessions.get_sessions().create_session(
                "assistant_dm", sessions.scope_for(str(bot["template_id"])), title=title, bind_scope=False
            )
            session_id = session.meta.session_id
            conversation = repos.create_im_conversation(
                bot_id=str(bot["bot_id"]),
                kind=kind,
                remote_conversation_id=remote_conversation_id,
                session_id=session_id,
                display_name=display_name,
            )
        if not repos.claim_im_message(
            platform=platform,
            bot_id=str(bot["bot_id"]),
            provider_message_id=provider_message_id,
            conversation_id=str(conversation["conversation_id"]),
        ):
            return
        inbound_attachments = [a for a in (attachments or []) if isinstance(a, dict) and a.get("path")]
        content = (text or "").strip()
        if inbound_attachments:
            summaries = [text_summary(str(a.get("kind") or "file"), str(a.get("name") or "")) for a in inbound_attachments]
            content = "\n".join(part for part in [content, *summaries] if part).strip()
        content = content or "（用户 @ 了机器人）"
        session_id = str(conversation["session_id"])
        msg_meta = {
            "origin": "im",
            "via": platform,
            "im_conversation_id": conversation["conversation_id"],
            "provider_message_id": provider_message_id,
        }
        if inbound_attachments:
            msg_meta["attachments"] = inbound_attachments
        msg = repos.add_assistant_message(
            str(bot["template_id"]), "user", content, session_id=session_id, meta=msg_meta
        )
        repos.touch_im_conversation(str(conversation["conversation_id"]))
        repos.mark_session_unread(session_id)
        from server.api import assistant_session_ops as sessions
        from server.scheduler import group_scheduler as scheduler

        sessions.sync_after_message(str(bot["template_id"]), session_id)
        channel = f"assistant:{bot['template_id']}"
        scheduler._publish(channel, "assistant.message", msg)
        scheduler._publish(channel, "assistant.unread", {"template_id": bot["template_id"], "session_id": session_id})
        from server.runtime.run_coordinator import coordinator

        try:
            run_id = coordinator.enqueue(session_id, agent_id=str(bot["template_id"]))
        except Exception:
            log.warning("IM inbound ignored: session %s is busy", session_id)
            return
        scheduler.run_im_assistant_turn_async(
            str(bot["template_id"]),
            content,
            session_id=session_id,
            run_id=run_id,
            im_meta={
                "platform": platform,
                "conversation_id": conversation["conversation_id"],
                "provider_message_id": provider_message_id,
                "reply_target": reply_target or {},
            },
            attachments=inbound_attachments,
        )

    def reply_after_turn(self, meta: dict, text: str, attachments: list[dict] | None = None) -> None:
        conversation_id = str(meta.get("conversation_id") or "")
        if not conversation_id:
            return
        platform = str(meta.get("platform") or "")
        if platform == "feishu":
            target = meta.get("reply_target") or {}
            message_id = str(target.get("message_id") or meta.get("provider_message_id") or "")
            bot_id = str(target.get("provider_bot_id") or "")
            if message_id and bot_id:
                self._reply_feishu(bot_id, message_id, text, attachments or [])
        elif platform == "wecom":
            target = meta.get("reply_target") or {}
            bot_id = str(target.get("provider_bot_id") or "")
            chat_id = str(target.get("chat_id") or "")
            if bot_id and chat_id:
                self._reply_wecom(bot_id, chat_id, text, attachments or [])
        elif platform == "dingtalk":
            target = meta.get("reply_target") or {}
            bot_id = str(target.get("provider_bot_id") or "")
            message_id = str(target.get("message_id") or "")
            if bot_id and message_id:
                self._reply_dingtalk(bot_id, message_id, text, attachments or [])

    def _complete_registration(
        self,
        registration: Registration,
        *,
        application_id: str,
        bot_id: str,
        secret_data: dict,
        display_name: str,
    ) -> None:
        if registration.cancelled.is_set():
            return
        app = repos.upsert_im_application(
            platform=registration.platform,
            provider_application_id=application_id,
            display_name=display_name,
        )
        ref = f"im-bot:{registration.platform}:{application_id}:{bot_id}"
        secrets.save(ref, secret_data)
        bot = repos.upsert_im_bot(
            application_id=str(app["application_id"]),
            provider_bot_id=bot_id,
            display_name=display_name,
            secret_ref=ref,
            health="stopped",
        )
        with registration.lock:
            registration.bot_id = str(bot["bot_id"])
            registration.display_name = display_name
            registration.state = "success"

    def _register_feishu(self, registration: Registration) -> None:
        try:
            import lark_oapi as lark

            def on_qr(info: dict) -> None:
                if registration.cancelled.is_set():
                    return
                with registration.lock:
                    registration.qr_image = _qr_image(str(info["url"]))
                    registration.expires_in = int(info.get("expire_in") or 0) or None
                    registration.state = "waiting"

            result = lark.register_app(
                on_qr_code=on_qr,
                on_status_change=lambda _info: None,
                cancel_event=registration.cancelled,
                app_preset={"name": "Avent AI 助理"},
                addons={"events": {"items": {"tenant": ["im.message.receive_v1"]}}},
                source="avent-agent",
            )
            app_id = str(result.get("client_id") or "")
            secret = str(result.get("client_secret") or "")
            if not app_id or not secret:
                raise RuntimeError("飞书未返回完整机器人凭据")
            self._complete_registration(
                registration,
                application_id=app_id,
                bot_id=app_id,
                secret_data={"app_id": app_id, "app_secret": secret, "domain": "feishu"},
                display_name="飞书机器人",
            )
        except Exception as exc:
            self._fail_registration(registration, "飞书扫码创建失败，请刷新二维码后重试。", exc)

    def _register_wecom(self, registration: Registration) -> None:
        try:
            platform = {"darwin": 1, "win32": 2, "linux": 3}.get(__import__("sys").platform, 0)
            response = httpx.get(
                "https://work.weixin.qq.com/ai/qc/generate",
                params={"source": "wecom_cli_external", "plat": platform},
                timeout=15,
            )
            payload = response.json().get("data") or {}
            scode, auth_url = str(payload.get("scode") or ""), str(payload.get("auth_url") or "")
            if not response.is_success or not scode or not auth_url:
                raise RuntimeError("企微二维码接口未返回授权信息")
            with registration.lock:
                registration.qr_image = _qr_image(auth_url)
                registration.expires_in = 300
                registration.state = "waiting"
            deadline = time.monotonic() + 300
            while not registration.cancelled.is_set() and time.monotonic() < deadline:
                time.sleep(3)
                result = httpx.get(
                    "https://work.weixin.qq.com/ai/qc/query_result", params={"scode": scode}, timeout=15
                )
                data = result.json().get("data") or {}
                if data.get("status") != "success":
                    continue
                bot = data.get("bot_info") or {}
                bot_id, secret = str(bot.get("botid") or ""), str(bot.get("secret") or "")
                if not bot_id or not secret:
                    raise RuntimeError("企微未返回完整机器人凭据")
                self._complete_registration(
                    registration,
                    application_id=bot_id,
                    bot_id=bot_id,
                    secret_data={"bot_id": bot_id, "secret": secret},
                    display_name="企业微信机器人",
                )
                return
            if not registration.cancelled.is_set():
                raise RuntimeError("二维码已过期")
        except Exception as exc:
            self._fail_registration(registration, "企微扫码创建失败，请刷新二维码后重试。", exc)

    def _register_dingtalk(self, registration: Registration) -> None:
        try:
            initialized = self._dingtalk_registration_request("/app/registration/init", {})
            nonce = str(initialized.get("nonce") or "")
            if not nonce:
                raise RuntimeError("钉钉未返回授权信息")
            started = self._dingtalk_registration_request("/app/registration/begin", {"nonce": nonce})
            device_code = str(started.get("device_code") or "")
            authorization_url = str(started.get("verification_uri_complete") or "")
            if not device_code or not authorization_url:
                raise RuntimeError("钉钉未返回扫码链接")
            expires_in = int(started.get("expires_in") or 7200)
            interval = max(1, int(started.get("interval") or 5))
            with registration.lock:
                registration.state = "waiting"
                registration.qr_image = _qr_image(authorization_url)
                registration.expires_in = expires_in
                registration.message = "请打开钉钉扫描二维码，并在手机上确认创建机器人。"
            deadline = time.monotonic() + expires_in
            while not registration.cancelled.is_set() and time.monotonic() < deadline:
                result = self._dingtalk_registration_request("/app/registration/poll", {"device_code": device_code})
                status = str(result.get("status") or "").upper()
                if status == "SUCCESS":
                    client_id = str(result.get("client_id") or "")
                    client_secret = str(result.get("client_secret") or "")
                    if not client_id or not client_secret:
                        raise RuntimeError("钉钉未返回完整机器人凭据")
                    self._complete_registration(
                        registration,
                        application_id=client_id,
                        bot_id=client_id,
                        secret_data={"client_id": client_id, "client_secret": client_secret},
                        display_name="钉钉机器人",
                    )
                    return
                if status == "FAIL":
                    reason = str(result.get("fail_reason") or "授权未完成")
                    raise RuntimeError(reason)
                if status == "EXPIRED":
                    raise RuntimeError("二维码已过期")
                if status != "WAITING":
                    raise RuntimeError("钉钉返回了未知的授权状态")
                time.sleep(interval)
            if not registration.cancelled.is_set():
                raise RuntimeError("二维码已过期")
        except Exception as exc:
            self._fail_registration(registration, "钉钉机器人创建失败，请完成扫码授权后重试。", exc)

    @staticmethod
    def _dingtalk_registration_request(path: str, payload: dict[str, str]) -> dict[str, Any]:
        response = httpx.post(
            f"{_DINGTALK_REGISTRATION_BASE_URL}{path}", json=payload, timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("钉钉返回格式无效")
        if data.get("errcode") not in (0, "0"):
            raise RuntimeError(str(data.get("errmsg") or "钉钉服务暂时不可用"))
        return data

    def _start_feishu_bot(self, bot: dict) -> None:
        bot_id = str(bot["bot_id"])
        if bot_id in self._feishu_started:
            return
        credentials = secrets.load(str(bot.get("secret_ref") or ""))
        app_id, app_secret = str(credentials["app_id"]), str(credentials["app_secret"])
        import lark_oapi as lark

        def on_message(data: Any) -> None:
            event, message = data.event, data.event.message
            raw_content = str(message.content or "")
            try:
                payload = json.loads(raw_content)
            except Exception:
                payload = raw_content
            message_type = str(getattr(message, "message_type", "") or "text")
            content = _content_text(payload).strip()
            attachments: list[dict] = []
            if isinstance(payload, dict):
                seen_keys: set[str] = set()
                for file_key, resource_type, name in _resource_keys(payload):
                    if file_key in seen_keys:
                        continue
                    seen_keys.add(file_key)
                    try:
                        attachment = self._download_feishu_attachment(
                            app_id,
                            app_secret,
                            str(message.message_id or ""),
                            file_key,
                            resource_type or message_type,
                            name,
                        )
                        if attachment:
                            attachments.append(attachment)
                    except Exception as exc:
                        log.warning("Feishu attachment download failed: %s", exc)
            if not content and not attachments:
                content = f"[飞书{message_type}消息]"
            chat_id = str(message.chat_id or "")
            sender = str(event.sender.sender_id.open_id or "")
            kind = "direct" if str(message.chat_type) == "p2p" else "group"
            remote_id = sender if kind == "direct" else chat_id
            self.handle_inbound(
                platform="feishu",
                provider_bot_id=app_id,
                provider_message_id=str(message.message_id or ""),
                kind=kind,
                remote_conversation_id=remote_id,
                text=content,
                attachments=attachments,
                display_name="群聊" if kind == "group" else "飞书私聊",
                reply_target={"message_id": str(message.message_id or ""), "provider_bot_id": app_id},
            )

        def run() -> None:
            import asyncio
            import lark_oapi.ws.client as ws_client

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ws_client.loop = loop
            # The SDK captures its event loop while building the dispatcher and
            # client. Both must be created after installing this thread's loop.
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message)
                .build()
            )
            client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
            client.start()

        threading.Thread(target=run, daemon=True, name=f"im-feishu-{bot_id}").start()
        self._feishu_started.add(bot_id)
        repos.set_im_bot_health(bot_id, "connected")

    @staticmethod
    def _download_feishu_attachment(
        app_id: str,
        app_secret: str,
        message_id: str,
        file_key: str,
        message_type: str,
        name: str,
    ) -> dict | None:
        if not message_id or not file_key:
            return None
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(message_type).build()
        result = client.im.v1.message_resource.get(request)
        if not result.success() or not result.file:
            raise RuntimeError(f"飞书附件下载失败: {result.code} {result.msg}")
        filename = name or str(result.file_name or "attachment")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return save_media(result.file.read(), platform="feishu", message_id=message_id, name=filename, mime=mime)

    def _reply_feishu(self, provider_bot_id: str, message_id: str, text: str, attachments: list[dict]) -> None:
        bot = repos.get_im_bot_by_provider("feishu", provider_bot_id, include_secret_ref=True)
        if not bot:
            return
        credentials = secrets.load(str(bot.get("secret_ref") or ""))
        import json
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
            CreateImageRequest,
            CreateImageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        client = lark.Client.builder().app_id(credentials["app_id"]).app_secret(credentials["app_secret"]).build()
        def reply(msg_type: str, content: dict) -> None:
            body = ReplyMessageRequestBody.builder().msg_type(msg_type).content(json.dumps(content, ensure_ascii=False)).build()
            result = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())
            if not result.success():
                raise RuntimeError(f"飞书回发失败: {result.code} {result.msg}")

        if text:
            reply("interactive", _feishu_markdown_card(text))
        for attachment in attachments:
            path = str(attachment.get("path") or "")
            item = attachment_from_path(path, name=str(attachment.get("name") or ""), mime=str(attachment.get("mime") or ""))
            if not item:
                continue
            with open(item["path"], "rb") as stream:
                if item["kind"] == "image":
                    upload = client.im.v1.image.create(
                        CreateImageRequest.builder().request_body(
                            CreateImageRequestBody.builder().image_type("message").image(stream).build()
                        ).build()
                    )
                    if not upload.success() or not upload.data or not upload.data.image_key:
                        raise RuntimeError(f"飞书图片上传失败: {upload.code} {upload.msg}")
                    reply("image", {"image_key": upload.data.image_key})
                    continue
                file_type = "opus" if item["kind"] == "audio" else "mp4" if item["kind"] == "video" else "stream"
                upload = client.im.v1.file.create(
                    CreateFileRequest.builder().request_body(
                        CreateFileRequestBody.builder().file_type(file_type).file_name(item["name"]).file(stream).build()
                    ).build()
                )
            if not upload.success() or not upload.data or not upload.data.file_key:
                raise RuntimeError(f"飞书文件上传失败: {upload.code} {upload.msg}")
            reply("audio" if item["kind"] == "audio" else "media" if item["kind"] == "video" else "file", {"file_key": upload.data.file_key})

    def _start_dingtalk_bot(self, bot: dict) -> None:
        bot_id = str(bot["bot_id"])
        with self._dingtalk_lock:
            if bot_id in self._dingtalk_started:
                return
        credentials = secrets.load(str(bot.get("secret_ref") or ""))
        client_id = str(credentials["client_id"])
        client_secret = str(credentials["client_secret"])
        import dingtalk_stream

        owner = self

        class DingTalkHandler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: Any) -> tuple[int, str]:
                message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                is_private = str(message.conversation_type) == "1"
                is_group_mention = str(message.conversation_type) == "2" and bool(message.is_in_at_list)
                if not (is_private or is_group_mention):
                    return dingtalk_stream.AckMessage.STATUS_OK, "ignored"
                remote_id = str(message.sender_id if is_private else message.conversation_id or "")
                message_id = str(message.message_id or "")
                if not remote_id or not message_id:
                    return dingtalk_stream.AckMessage.STATUS_OK, "ignored"
                content = "\n".join(message.get_text_list() or [])
                attachments: list[dict] = []
                download_codes = list(message.get_image_list() or [])
                download_codes.extend(_download_codes(getattr(message, "extensions", {})))
                for index, download_code in enumerate(dict.fromkeys(map(str, download_codes))):
                    try:
                        url = self.get_image_download_url(str(download_code))
                        if not url:
                            continue
                        response = httpx.get(url, timeout=30)
                        response.raise_for_status()
                        attachments.append(
                            save_media(
                                response.content,
                                platform="dingtalk",
                                message_id=message_id,
                                name=f"{getattr(message, 'message_type', 'attachment')}-{index + 1}",
                                mime=response.headers.get("content-type", "application/octet-stream"),
                            )
                        )
                    except Exception as exc:
                        log.warning("DingTalk attachment download failed: %s", exc)
                if not content:
                    content = _content_text(getattr(message, "extensions", {})).strip()
                if not content and not attachments:
                    content = f"[钉钉{getattr(message, 'message_type', 'unknown')}消息]"
                with owner._dingtalk_lock:
                    owner._dingtalk_reply_targets[(bot_id, message_id)] = (self, message)
                owner.handle_inbound(
                    platform="dingtalk",
                    provider_bot_id=client_id,
                    provider_message_id=message_id,
                    kind="direct" if is_private else "group",
                    remote_conversation_id=remote_id,
                    text=content,
                    attachments=attachments,
                    display_name=str(message.sender_nick if is_private else message.conversation_title or "钉钉群聊"),
                    reply_target={
                        "provider_bot_id": client_id,
                        "message_id": message_id,
                        "target_id": remote_id,
                        "target_kind": "direct" if is_private else "group",
                    },
                )
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(client_id, client_secret))
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, DingTalkHandler())
        with self._dingtalk_lock:
            if bot_id in self._dingtalk_started:
                return
            self._dingtalk_started.add(bot_id)
            self._dingtalk_clients[bot_id] = client

        def run() -> None:
            try:
                client.start_forever()
            except Exception as exc:
                repos.set_im_bot_health(bot_id, "error", f"钉钉消息连接失败: {exc}")
                log.exception("DingTalk Stream bot stopped: %s", bot_id)
            finally:
                with self._dingtalk_lock:
                    self._dingtalk_started.discard(bot_id)
                    self._dingtalk_clients.pop(bot_id, None)

        threading.Thread(target=run, daemon=True, name=f"im-dingtalk-{bot_id}").start()
        repos.set_im_bot_health(bot_id, "connected")

    def _stop_dingtalk_bot(self, bot_id: str) -> None:
        with self._dingtalk_lock:
            self._dingtalk_started.discard(bot_id)
            self._dingtalk_clients.pop(bot_id, None)
            for key in [key for key in self._dingtalk_reply_targets if key[0] == bot_id]:
                self._dingtalk_reply_targets.pop(key, None)

    def _reply_dingtalk(self, provider_bot_id: str, message_id: str, text: str, attachments: list[dict]) -> None:
        bot = repos.get_im_bot_by_provider("dingtalk", provider_bot_id)
        if not bot:
            return
        with self._dingtalk_lock:
            target = self._dingtalk_reply_targets.pop((str(bot["bot_id"]), message_id), None)
        if not target:
            raise RuntimeError("钉钉回复通道已过期")
        handler, message = target
        if text:
            handler.reply_markdown(str(bot.get("display_name") or "Avent 助理")[:64], text[:4000], message)
        for attachment in attachments:
            item = attachment_from_path(
                str(attachment.get("path") or ""),
                name=str(attachment.get("name") or ""),
                mime=str(attachment.get("mime") or ""),
            )
            if not item:
                continue
            self._send_dingtalk_attachment(
                handler,
                provider_bot_id,
                message,
                item,
            )

    @staticmethod
    def _send_dingtalk_attachment(handler: Any, provider_bot_id: str, message: Any, item: dict) -> None:
        """Send media through the OpenAPI; session webhooks only reliably deliver text."""
        media_type = "image" if item["kind"] == "image" else "file"
        media_id = handler.dingtalk_client.upload_to_dingtalk(
            Path(item["path"]).read_bytes(),
            filetype=media_type,
            filename=item["name"],
            mimetype=item["mime"],
        )
        if not media_id:
            raise RuntimeError("钉钉媒体上传失败")
        target_kind = "direct" if str(message.conversation_type) == "1" else "group"
        # The inbound senderId is an OpenDingTalkId. The proactive one-to-one
        # endpoint instead requires the sender's enterprise staff ID.
        target_id = str(
            (getattr(message, "sender_staff_id", "") or getattr(message, "sender_id", ""))
            if target_kind == "direct"
            else message.conversation_id
            or ""
        )
        if not target_id:
            raise RuntimeError("钉钉媒体回传目标缺失")
        if item["kind"] == "image":
            msg_key = "sampleImageMsg"
            msg_param = {"photoURL": media_id}
        else:
            msg_key = "sampleFile"
            msg_param = {
                "mediaId": media_id,
                "fileName": item["name"],
                "fileType": Path(item["name"]).suffix.lstrip(".") or "file",
            }
        payload = {
            "robotCode": provider_bot_id,
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        if target_kind == "group":
            payload["openConversationId"] = target_id
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload["userIds"] = [target_id]
        access_token = handler.dingtalk_client.get_access_token()
        if not access_token:
            raise RuntimeError("钉钉访问令牌获取失败")
        response = httpx.post(
            url,
            json=payload,
            headers={"x-acs-dingtalk-access-token": access_token},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("钉钉媒体回传返回格式无效")
        if result.get("success") is False or result.get("errcode") not in (None, 0, "0") or result.get("code") not in (None, 0, "0"):
            raise RuntimeError(f"钉钉媒体回传失败: {result.get('errmsg') or result.get('message') or result}")
        log.info("DingTalk attachment delivered: name=%s target_kind=%s", item["name"], target_kind)

    def _start_wecom_bot(self, bot: dict) -> None:
        bot_id = str(bot["bot_id"])
        with self._wecom_lock:
            current = self._wecom_processes.get(bot_id)
            if current and current.poll() is None:
                return
        node = shutil.which("node")
        if not node:
            raise RuntimeError("未找到 Node.js，无法启动企微机器人")
        credentials = secrets.load(str(bot.get("secret_ref") or ""))
        bridge = Path(__file__).with_name("wecom_bridge.mjs")
        if not bridge.is_file():
            raise RuntimeError("企微机器人运行组件缺失")
        process = subprocess.Popen(
            [node, str(bridge)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with self._wecom_lock:
            self._wecom_processes[bot_id] = process
        self._send_wecom_command(
            bot_id,
            {"type": "start", "bot_id": credentials["bot_id"], "secret": credentials["secret"]},
        )
        threading.Thread(target=self._read_wecom_output, args=(bot_id, process), daemon=True, name=f"im-wecom-{bot_id}").start()
        threading.Thread(target=self._read_wecom_errors, args=(bot_id, process), daemon=True, name=f"im-wecom-stderr-{bot_id}").start()
        repos.set_im_bot_health(bot_id, "connecting")

    def _stop_wecom_bot(self, bot_id: str) -> None:
        with self._wecom_lock:
            process = self._wecom_processes.pop(bot_id, None)
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.write('{"type":"stop"}\n')
                process.stdin.flush()
            process.terminate()
        except OSError:
            pass

    def _send_wecom_command(self, bot_id: str, payload: dict) -> None:
        with self._wecom_lock:
            process = self._wecom_processes.get(bot_id)
            if not process or process.poll() is not None or not process.stdin:
                raise RuntimeError("企微机器人未连接")
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _read_wecom_output(self, bot_id: str, process: subprocess.Popen[str]) -> None:
        if not process.stdout:
            return
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("invalid WeCom bridge output: %s", line.strip())
                continue
            try:
                kind = event.get("type")
                if kind == "status":
                    status = str(event.get("status") or "")
                    if status == "connected":
                        repos.set_im_bot_health(bot_id, "connected")
                    elif status == "error":
                        repos.set_im_bot_health(bot_id, "error", str(event.get("error") or "企微连接失败"))
                elif kind == "log":
                    log.log(
                        logging.WARNING if event.get("level") in {"warning", "error"} else logging.INFO,
                        "WeCom bridge %s: %s",
                        bot_id,
                        event.get("message") or "",
                    )
                elif kind == "inbound":
                    attachments: list[dict] = []
                    for raw in event.get("attachments") or []:
                        if not isinstance(raw, dict):
                            continue
                        encoded = str(raw.get("data") or "")
                        if not encoded:
                            continue
                        try:
                            name = str(raw.get("name") or "attachment")
                            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                            attachments.append(
                                save_media(
                                    base64.b64decode(encoded, validate=True),
                                    platform="wecom",
                                    message_id=str(event["provider_message_id"]),
                                    name=name,
                                    mime=mime,
                                )
                            )
                        except Exception as exc:
                            log.warning("WeCom attachment decode failed: %s", exc)
                    self.handle_inbound(
                        platform="wecom",
                        provider_bot_id=str(event["provider_bot_id"]),
                        provider_message_id=str(event["provider_message_id"]),
                        kind=str(event["kind"]),
                        remote_conversation_id=str(event["remote_conversation_id"]),
                        text=str(event.get("text") or ""),
                        attachments=attachments,
                        display_name=str(event.get("display_name") or ""),
                        reply_target=event.get("reply_target") or {},
                    )
            except Exception:
                log.exception("failed to handle WeCom bridge event")
        if process.poll() is not None:
            repos.set_im_bot_health(bot_id, "error", "企微机器人连接已停止")

    def _read_wecom_errors(self, bot_id: str, process: subprocess.Popen[str]) -> None:
        if not process.stderr:
            return
        for line in process.stderr:
            log.warning("WeCom bridge %s: %s", bot_id, line.strip())

    def _reply_wecom(self, provider_bot_id: str, chat_id: str, text: str, attachments: list[dict]) -> None:
        bot = repos.get_im_bot_by_provider("wecom", provider_bot_id)
        if not bot:
            return
        payload_attachments = []
        for attachment in attachments:
            item = attachment_from_path(
                str(attachment.get("path") or ""),
                name=str(attachment.get("name") or ""),
                mime=str(attachment.get("mime") or ""),
            )
            if not item:
                continue
            payload_attachments.append(
                {"kind": item["kind"], "name": item["name"], "data": base64.b64encode(Path(item["path"]).read_bytes()).decode("ascii")}
            )
        self._send_wecom_command(
            str(bot["bot_id"]),
            {"type": "reply", "chat_id": chat_id, "text": text[:4000], "attachments": payload_attachments},
        )

    def _fail_registration(self, registration: Registration, message: str, exc: Exception) -> None:
        if registration.cancelled.is_set():
            return
        log.warning("%s registration failed: %s", registration.platform, exc)
        with registration.lock:
            registration.state = "error"
            registration.error = message

    @staticmethod
    def _platform_label(platform: str) -> str:
        return {"feishu": "飞书", "dingtalk": "钉钉", "wecom": "企微"}[platform]

    @staticmethod
    def _assert_platform(platform: str | None) -> None:
        if platform not in _PLATFORMS:
            raise ValueError("unsupported IM platform")


def _feishu_markdown_card(text: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text[:4000]}}],
    }


manager = ImChannelManager()
