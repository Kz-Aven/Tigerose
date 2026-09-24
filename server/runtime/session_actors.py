"""Session mutation actors — only user_ui or approved agent requests may clear/delete."""

from __future__ import annotations

ACTOR_USER_UI = "user_ui"
ACTOR_USER_APPROVED_AGENT = "user_approved_agent_request"
ALLOWED_DESTRUCTIVE_ACTORS = frozenset({ACTOR_USER_UI, ACTOR_USER_APPROVED_AGENT})

HEADER_NAME = "X-Avent-Actor"


def normalize_actor(raw: str | None) -> str:
    return (raw or "").strip().lower()


def assert_destructive_actor(actor: str | None) -> str:
    """Return normalized actor or raise ValueError."""
    value = normalize_actor(actor)
    if value not in ALLOWED_DESTRUCTIVE_ACTORS:
        raise ValueError(
            "destructive session mutation requires actor "
            f"'{ACTOR_USER_UI}' or '{ACTOR_USER_APPROVED_AGENT}' "
            f"(got {actor!r})"
        )
    return value
