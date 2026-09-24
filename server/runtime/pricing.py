"""Local model pricing and USD/CNY conversion for usage reporting."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
import httpx

from avent_paths import data_root, ensure_data_dirs


_RATE_CACHE_TTL_SECONDS = 6 * 60 * 60
_PRICING_SEED = """# Model name is the key. Prices are per 1,000,000 tokens.
models:
  gpt-5.6-terra:
    currency: USD
    input_per_million: 2.00
    cached_input_per_million: 0.20
    output_per_million: 12.00
  deepseek-v4-flash:
    currency: CNY
    input_per_million: 3.00
    cached_input_per_million: 0.10
    output_per_million: 9.00
  glm-5.3-flash:
    currency: CNY
    input_per_million: 0.80
    cached_input_per_million: 0.23
    output_per_million: 2.80
"""


def pricing_path() -> Path:
    ensure_data_dirs()
    return data_root() / "Pricing.yaml"


def ensure_pricing_file() -> Path:
    path = pricing_path()
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(_PRICING_SEED, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def _rate_cache_path() -> Path:
    ensure_data_dirs()
    return data_root() / "data" / "usd_cny_rate.json"


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _currency(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"CNY", "RMB", "人民币", "¥"}:
        return "CNY"
    if raw in {"USD", "美元", "$"}:
        return "USD"
    return ""


def _pricing_models() -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load(ensure_pricing_file().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def _cached_rate() -> tuple[float | None, float]:
    try:
        payload = json.loads(_rate_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None, 0
    if not isinstance(payload, dict):
        return None, 0
    return _number(payload.get("rate")), _number(payload.get("fetched_at")) or 0


def _store_rate(rate: float) -> None:
    _rate_cache_path().write_text(
        json.dumps({"rate": rate, "fetched_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def usd_to_cny_rate() -> float | None:
    cached_rate, fetched_at = _cached_rate()
    if cached_rate is not None and time.time() - fetched_at < _RATE_CACHE_TTL_SECONDS:
        return cached_rate
    try:
        response = httpx.get("https://api.frankfurter.dev/v2/rate/USD/CNY", timeout=3)
        response.raise_for_status()
        payload = response.json()
        rate = _number(payload.get("rate") if isinstance(payload, dict) else None)
        if rate is None or rate <= 0:
            raise ValueError("invalid USD/CNY rate")
        _store_rate(rate)
        return rate
    except Exception:
        return cached_rate


def apply_model_costs(rows: list[dict]) -> list[dict]:
    models = _pricing_models()
    needs_usd_rate = any(
        _currency(models.get(str(row.get("model") or ""), {}).get("currency")) == "USD"
        for row in rows
    )
    usd_rate = usd_to_cny_rate() if needs_usd_rate else None
    enriched: list[dict] = []
    for source in rows:
        row = dict(source)
        pricing = models.get(str(row.get("model") or ""))
        if not isinstance(pricing, dict):
            row.update({"spend_cny": None, "price_status": "unconfigured"})
            enriched.append(row)
            continue
        currency = _currency(pricing.get("currency"))
        input_price = _number(pricing.get("input_per_million"))
        cached_price = _number(pricing.get("cached_input_per_million"))
        output_price = _number(pricing.get("output_per_million"))
        if (
            not currency
            or input_price is None
            or cached_price is None
            or output_price is None
            or min(input_price, cached_price, output_price) < 0
        ):
            row.update({"spend_cny": None, "price_status": "unconfigured"})
            enriched.append(row)
            continue
        if currency == "USD" and usd_rate is None:
            row.update({"spend_cny": None, "price_status": "exchange_rate_unavailable"})
            enriched.append(row)
            continue
        multiplier = usd_rate if currency == "USD" else 1.0
        uncached_input = _number(row.get("uncached_input_tokens")) or 0
        cached_input = _number(row.get("cached_input_tokens")) or 0
        output = _number(row.get("output_tokens")) or 0
        amount = (
            input_price * uncached_input
            + cached_price * cached_input
            + output_price * output
        ) / 1_000_000 * multiplier
        row.update({"spend_cny": amount, "price_status": "configured"})
        enriched.append(row)
    return enriched


def cost_total(rows: list[dict]) -> tuple[float, int]:
    amount = sum(float(row["spend_cny"]) for row in rows if row.get("spend_cny") is not None)
    unpriced_models = {
        str(row.get("model") or "")
        for row in rows
        if row.get("price_status") == "unconfigured"
    }
    return amount, len(unpriced_models)


def apply_agent_costs(agent_rows: list[dict], agent_model_rows: list[dict]) -> list[dict]:
    costs_by_agent: dict[str, list[dict]] = {}
    for row in apply_model_costs(agent_model_rows):
        costs_by_agent.setdefault(str(row.get("agent_id") or ""), []).append(row)
    enriched: list[dict] = []
    for source in agent_rows:
        row = dict(source)
        model_costs = costs_by_agent.get(str(row.get("agent_id") or ""), [])
        amount, unpriced_model_count = cost_total(model_costs)
        row.update(
            {
                "spend_cny": amount if any(cost.get("spend_cny") is not None for cost in model_costs) else None,
                "unpriced_model_count": unpriced_model_count,
            }
        )
        enriched.append(row)
    return enriched
