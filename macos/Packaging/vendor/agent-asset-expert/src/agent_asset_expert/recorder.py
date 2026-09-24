"""Failure-isolated public ingestion facade for platform adapters."""
from __future__ import annotations

import logging
from typing import Any

from .storage import AssetStore

log = logging.getLogger(__name__)


class ObservationRecorder:
    def __init__(self, store: AssetStore | None = None) -> None:
        self.store = store or AssetStore()

    def record_execution(self, **kwargs: Any) -> str | None:
        try:
            return self.store.record_execution(**kwargs)
        except Exception as exc:
            log.exception("agent asset collection failed")
            return None
