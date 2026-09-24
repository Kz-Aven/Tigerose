from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.im_channels.manager import ImChannelManager, _content_text, _feishu_markdown_card, _resource_keys
from server.im_channels.media import attachment_from_path
from server.runtime.turn import _reply_attachments_from_tool_trace


class ImChannelMediaTests(unittest.TestCase):
    def test_rich_content_extracts_text_and_resources(self) -> None:
        payload = {
            "zh_cn": {"title": "报告", "content": [[{"tag": "text", "text": "请分析"}, {"image_key": "img-1"}]]},
            "file_key": "file-1",
            "file_name": "report.pdf",
        }
        self.assertIn("请分析", _content_text(payload))
        self.assertEqual(_resource_keys(payload), [("file-1", "", "report.pdf"), ("img-1", "image", "image")])

    def test_inbound_attachments_reach_assistant_turn(self) -> None:
        manager = ImChannelManager()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            path.write_bytes(b"image")
            attachment = attachment_from_path(path)
            assert attachment is not None
            bot = {"bot_id": "bot-1", "template_id": "assistant-1"}
            conversation = {"conversation_id": "conversation-1", "session_id": "session-1"}
            with patch("server.im_channels.manager.repos.get_im_bot_by_provider", return_value=bot), patch(
                "server.im_channels.manager.repos.get_im_conversation", return_value=conversation
            ), patch("server.im_channels.manager.repos.claim_im_message", return_value=True), patch(
                "server.im_channels.manager.repos.add_assistant_message", return_value={}
            ) as add_message, patch("server.im_channels.manager.repos.touch_im_conversation"), patch(
                "server.im_channels.manager.repos.mark_session_unread"
            ), patch("server.api.assistant_session_ops.sync_after_message"), patch(
                "server.scheduler.group_scheduler._publish"
            ), patch("server.runtime.run_coordinator.coordinator.enqueue", return_value="run-1"), patch(
                "server.scheduler.group_scheduler.run_im_assistant_turn_async"
            ) as enqueue:
                manager.handle_inbound(
                    platform="feishu",
                    provider_bot_id="provider-1",
                    provider_message_id="message-1",
                    kind="direct",
                    remote_conversation_id="user-1",
                    text="请看附件",
                    attachments=[attachment],
                )

        self.assertIn("[图片: image.png]", add_message.call_args.args[2])
        self.assertEqual(add_message.call_args.kwargs["meta"]["attachments"], [attachment])
        self.assertEqual(enqueue.call_args.kwargs["attachments"], [attachment])

    def test_reply_attachments_are_collected_from_successful_file_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.txt"
            report.write_text("done")
            attachments = _reply_attachments_from_tool_trace(
                [{"tool": "write_file", "args": {"path": "report.txt"}, "outcome": "ok"}], str(root)
            )
        self.assertEqual(attachments[0]["name"], "report.txt")
        self.assertEqual(attachments[0]["kind"], "file")

    def test_bash_outputs_are_not_collected_as_reply_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.csv"
            report.write_text("name,value\na,1\n", encoding="utf-8")
            attachments = _reply_attachments_from_tool_trace(
                [{"tool": "bash", "args": {"command": "touch report.csv"}, "outcome": "ok"}], str(root)
            )
        self.assertEqual(attachments, [])

    def test_feishu_reply_uses_markdown_card(self) -> None:
        card = _feishu_markdown_card("**完成**")
        self.assertEqual(card["elements"][0]["text"], {"tag": "lark_md", "content": "**完成**"})

    def test_reply_dispatches_attachments_to_platform_adapter(self) -> None:
        manager = ImChannelManager()
        attachments = [{"path": "/tmp/report.txt", "name": "report.txt", "mime": "text/plain"}]
        with patch.object(manager, "_reply_feishu") as reply:
            manager.reply_after_turn(
                {"platform": "feishu", "conversation_id": "conversation", "reply_target": {"provider_bot_id": "bot", "message_id": "message"}},
                "完成",
                attachments,
            )
        reply.assert_called_once_with("bot", "message", "完成", attachments)

    def test_dingtalk_reply_uses_markdown_and_proactive_file_message(self) -> None:
        manager = ImChannelManager()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.txt"
            report.write_text("done", encoding="utf-8")
            handler = type("Handler", (), {})()
            handler.reply_markdown = Mock()
            handler.dingtalk_client = type("Client", (), {"upload_to_dingtalk": Mock(return_value="media-1")})()
            message = type(
                "Message", (),
                {"conversation_type": "1", "sender_id": "open-user-1", "sender_staff_id": "staff-1", "conversation_id": ""},
            )()
            response = Mock()
            response.json.return_value = {"processQueryKey": "sent"}
            handler.dingtalk_client.get_access_token = Mock(return_value="token-1")
            with patch("server.im_channels.manager.repos.get_im_bot_by_provider", return_value={"bot_id": "bot", "display_name": "助理"}), patch(
                "server.im_channels.manager.httpx.post", return_value=response
            ) as post:
                manager._dingtalk_reply_targets[("bot", "message")] = (handler, message)
                manager._reply_dingtalk("provider", "message", "**完成**", [attachment_from_path(report)])

        handler.reply_markdown.assert_called_once_with("助理", "**完成**", message)
        self.assertEqual(post.call_args.args[0], "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend")
        self.assertEqual(post.call_args.kwargs["json"], {
            "robotCode": "provider",
            "msgKey": "sampleFile",
            "msgParam": '{"mediaId": "media-1", "fileName": "report.txt", "fileType": "txt"}',
            "userIds": ["staff-1"],
        })
        self.assertEqual(post.call_args.kwargs["headers"], {"x-acs-dingtalk-access-token": "token-1"})

    def test_dingtalk_group_attachment_uses_group_target(self) -> None:
        manager = ImChannelManager()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.pdf"
            report.write_bytes(b"pdf")
            handler = type("Handler", (), {})()
            handler.dingtalk_client = type(
                "Client", (),
                {"upload_to_dingtalk": Mock(return_value="media-1"), "get_access_token": Mock(return_value="token-1")},
            )()
            message = type("Message", (), {"conversation_type": "2", "sender_id": "user-1", "conversation_id": "cid-group"})()
            response = Mock()
            response.json.return_value = {"processQueryKey": "sent"}
            with patch("server.im_channels.manager.httpx.post", return_value=response) as post:
                manager._send_dingtalk_attachment(handler, "provider", message, attachment_from_path(report))

        self.assertEqual(post.call_args.args[0], "https://api.dingtalk.com/v1.0/robot/groupMessages/send")
        self.assertEqual(post.call_args.kwargs["json"]["openConversationId"], "cid-group")


if __name__ == "__main__":
    unittest.main()
