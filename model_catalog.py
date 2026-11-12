"""Versioned public Standard rates; availability comes only from the user's API key."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

PRICING_DATE = "2026-11-12"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
MODELS_URL = "https://api.openai.com/v1/models"
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
SPECS = {
    "gpt-6-luna": ("GPT-6 Luna", .1, .01, .5, EFFORTS),
    "gpt-6.1-terra": ("GPT-6.1 Terra", 1., .05, 5., EFFORTS),
    "gpt-6.1-sol": ("GPT-6.1 Sol", 2., .1, 10., EFFORTS[1:]),
    "gpt-6-astra": ("GPT-6 Astra", 10., 1., 50., EFFORTS[1:]),
}


def model_info(identifier):
    spec = SPECS.get(identifier)
    if not spec:
        return {"id": identifier, "name": identifier, "efforts": [], "pricing": None, "compatible": None}
    name, input_price, cached, output, efforts = spec
    return {"id": identifier, "name": name, "efforts": list(efforts), "compatible": True,
            "pricing": {"input": input_price, "cached_input": cached, "output": output,
                        "currency": "USD", "per_tokens": 1_000_000, "date": PRICING_DATE,
                        "tier": "Standard", "source": PRICING_SOURCE}}


def validate_effort(model, effort):
    if effort not in ["auto", *EFFORTS]:
        raise ValueError("Неизвестное усилие анализа.")
    if effort != "auto" and effort not in model_info(model)["efforts"]:
        raise ValueError("Для этой модели усилие не подтверждено. Выберите значение по умолчанию.")
    return effort


def cost(usage, pricing):
    if not pricing or usage.get("reported_calls", 0) != usage.get("calls", 0):
        return None
    if usage.get("unpriced_tokens"):
        return None
    cached = min(usage.get("cached_tokens", 0), usage["input_tokens"])
    return round(((usage["input_tokens"] - cached) * pricing["input"]
                  + cached * pricing["cached_input"] + usage["output_tokens"] * pricing["output"]) / 1_000_000, 8)


class Catalog:
    def __init__(self, api_key):
        self.key = api_key
        self.lock = threading.Lock()
        self.ids = None
        self.updated = None
        self.error = ""
        self.last_attempt = 0

    def snapshot(self):
        with self.lock:
            models = [dict(model_info(identifier), available=None if self.ids is None else identifier in self.ids)
                      for identifier in SPECS]
            return {"models": models, "other_models": sorted((self.ids or set()) - SPECS.keys()),
                    "api_configured": bool(self.key), "checked_at": self.updated, "error": self.error,
                    "pricing_date": PRICING_DATE, "pricing_source": PRICING_SOURCE,
                    "availability_checked": self.ids is not None}

    def refresh(self, http):
        from publications import ServiceError

        with self.lock:
            if time.monotonic() - self.last_attempt < 10:
                return
            self.last_attempt = time.monotonic()
            if not self.key:
                self.error = "Ключ API отсутствует в окружении сервера."
                return
            try:
                payload = http.json(MODELS_URL, "OpenAI", timeout=8,
                                    headers={"Authorization": "Bearer " + self.key})
                records = payload.get("data")
                if not isinstance(records, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                                        or item.get("shutdown_date") is not None and not isinstance(item["shutdown_date"], str)
                                                        for item in records):
                    raise ServiceError("OpenAI: неожиданный формат списка моделей.")
                self.ids = {item["id"] for item in records if not item.get("shutdown_date")
                            or item["shutdown_date"] > datetime.now(timezone.utc).date().isoformat()}
                self.updated = datetime.now(timezone.utc).isoformat()
                self.error = ""
            except ServiceError as error:
                self.error = str(error)
                if error.status in (401, 403):
                    self.ids = None
                    self.updated = None
