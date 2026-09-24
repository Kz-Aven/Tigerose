from __future__ import annotations

import unittest
from unittest.mock import patch

from server.im_channels.manager import ImChannelManager, Registration


class DingTalkImChannelTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    def test_registration_uses_official_device_flow_and_stores_stream_credentials(self) -> None:
        manager = ImChannelManager()
        registration = Registration(registration_id="reg", platform="dingtalk")
        completed: dict = {}
        responses = [
            self._Response({"errcode": 0, "nonce": "nonce-1", "expires_in": 300}),
            self._Response({
                "errcode": 0,
                "device_code": "device-1",
                "verification_uri_complete": "https://scan.dingtalk.test/device-1",
                "expires_in": 7200,
                "interval": 5,
            }),
            self._Response({"errcode": 0, "status": "SUCCESS", "client_id": "client-1", "client_secret": "secret-1"}),
        ]
        with patch("server.im_channels.manager.httpx.post", side_effect=responses) as post, patch(
            "server.im_channels.manager._qr_image", return_value="data:image/png;base64,qr"
        ), patch.object(
            manager, "_complete_registration", side_effect=lambda _r, **kwargs: completed.update(kwargs)
        ):
            manager._register_dingtalk(registration)  # noqa: SLF001

        self.assertEqual(completed["application_id"], "client-1")
        self.assertEqual(completed["bot_id"], "client-1")
        self.assertEqual(completed["secret_data"], {"client_id": "client-1", "client_secret": "secret-1"})
        self.assertEqual(registration.qr_image, "data:image/png;base64,qr")
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            "https://oapi.dingtalk.com/app/registration/init",
            "https://oapi.dingtalk.com/app/registration/begin",
            "https://oapi.dingtalk.com/app/registration/poll",
        ])
        self.assertEqual([call.kwargs["json"] for call in post.call_args_list], [
            {}, {"nonce": "nonce-1"}, {"device_code": "device-1"},
        ])

    def test_registration_request_surfaces_official_error(self) -> None:
        manager = ImChannelManager()
        with patch("server.im_channels.manager.httpx.post", return_value=self._Response({"errcode": 400, "errmsg": "服务繁忙"})):
            with self.assertRaisesRegex(RuntimeError, "服务繁忙"):
                manager._dingtalk_registration_request("/app/registration/init", {})  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
