"""Web search / extract provider ABC for Avent server runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class WebSearchProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool:
        """Fast local check — no network I/O."""

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        raise NotImplementedError

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError
