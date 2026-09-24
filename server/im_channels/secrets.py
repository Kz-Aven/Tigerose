"""Small macOS Keychain wrapper; database rows only retain the reference."""

from __future__ import annotations

import json
import subprocess


SERVICE = "com.avent.agent.im-channel"


def save(ref: str, value: dict) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ref, "-w", json.dumps(value)],
        check=True,
        capture_output=True,
        text=True,
    )


def load(ref: str) -> dict:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", ref, "-w"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def remove(ref: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", ref],
        check=False,
        capture_output=True,
        text=True,
    )
