from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.runtime.web.credentials import remove_legacy_feishu_credentials


class WebCredentialsTests(unittest.TestCase):
    def test_removing_legacy_feishu_credentials_preserves_other_env_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "TAVILY_API_KEY=keep-me\n"
                "FEISHU_APP_ID=old-app\n"
                "FEISHU_APP_SECRET=old-secret\n"
                "FEISHU_DOMAIN=feishu\n"
                "LARK_APP_ID=older-app\n"
                "LARK_APP_SECRET=older-secret\n"
                "# retained comment\n",
                encoding="utf-8",
            )
            with patch("server.runtime.web.credentials.env_path", return_value=path), patch.dict(
                os.environ,
                {
                    "TAVILY_API_KEY": "keep-me",
                    "FEISHU_APP_ID": "old-app",
                    "FEISHU_APP_SECRET": "old-secret",
                    "FEISHU_DOMAIN": "feishu",
                    "LARK_APP_ID": "older-app",
                    "LARK_APP_SECRET": "older-secret",
                },
                clear=False,
            ):
                remove_legacy_feishu_credentials()
                self.assertEqual(path.read_text(encoding="utf-8"), "TAVILY_API_KEY=keep-me\n# retained comment\n")
                self.assertEqual(os.environ["TAVILY_API_KEY"], "keep-me")
                for key in (
                    "FEISHU_APP_ID",
                    "FEISHU_APP_SECRET",
                    "FEISHU_DOMAIN",
                    "LARK_APP_ID",
                    "LARK_APP_SECRET",
                ):
                    self.assertNotIn(key, os.environ)


if __name__ == "__main__":
    unittest.main()
