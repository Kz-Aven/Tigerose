from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"TIGEROSE_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        from avent_paths import reset_path_cache

        reset_path_cache()
        self.addCleanup(reset_path_cache)

    def test_default_prices_convert_usd_and_exclude_unconfigured_models(self):
        from server.runtime import pricing

        with patch.object(pricing, "usd_to_cny_rate", return_value=7.2):
            rows = pricing.apply_model_costs(
                [
                    {
                        "model": "gpt-5.6-terra",
                        "uncached_input_tokens": 1_000_000,
                        "cached_input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                    },
                    {
                        "model": "deepseek-v4-flash",
                        "uncached_input_tokens": 1_000_000,
                        "cached_input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                    },
                    {
                        "model": "unknown-model",
                        "uncached_input_tokens": 1_000_000,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                    },
                ]
            )

        self.assertTrue(pricing.pricing_path().is_file())
        self.assertAlmostEqual(rows[0]["spend_cny"], 102.24)
        self.assertAlmostEqual(rows[1]["spend_cny"], 12.1)
        self.assertIsNone(rows[2]["spend_cny"])
        self.assertEqual(rows[2]["price_status"], "unconfigured")
        total, unpriced_model_count = pricing.cost_total(rows)
        self.assertAlmostEqual(total, 114.34)
        self.assertEqual(unpriced_model_count, 1)

    def test_stale_cached_exchange_rate_is_used_when_refresh_fails(self):
        from server.runtime import pricing

        cache_path = pricing._rate_cache_path()
        cache_path.write_text(json.dumps({"rate": 7.223, "fetched_at": 0}), encoding="utf-8")
        with patch("server.runtime.pricing.httpx.get", side_effect=OSError("offline")):
            self.assertEqual(pricing.usd_to_cny_rate(), 7.223)

    def test_concurrent_first_loads_never_read_a_partial_price_table(self):
        from server.runtime import pricing

        row = {
            "model": "deepseek-v4-flash",
            "uncached_input_tokens": 1_000_000,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: pricing.apply_model_costs([row])[0], range(4)))

        self.assertTrue(all(item["price_status"] == "configured" for item in results))


if __name__ == "__main__":
    unittest.main()
