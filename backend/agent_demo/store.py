"""Small data-source boundary used by the offline demo."""

from __future__ import annotations

from copy import deepcopy
from typing import Protocol


class ProjectStore(Protocol):
    def get_outline(self) -> list[dict]: ...

    def get_character(self, name: str) -> dict | None: ...

    def list_character_names(self) -> list[str]: ...

    def get_chapters(self) -> list[dict]: ...


class InMemoryProjectStore:
    """Read-only project data backed by a JSON-compatible mapping."""

    def __init__(self, data: dict):
        self._outline = deepcopy(data.get("outline", []))
        self._characters = deepcopy(data.get("characters", []))
        self._chapters = deepcopy(data.get("chapters", []))

    def get_outline(self) -> list[dict]:
        return deepcopy(self._outline)

    def get_character(self, name: str) -> dict | None:
        wanted = name.strip().casefold()
        for character in self._characters:
            if wanted == str(character.get("name", "")).casefold() or any(
                wanted == str(alias).casefold() for alias in character.get("aliases", [])
            ):
                return deepcopy(character)
        return None

    def list_character_names(self) -> list[str]:
        return [str(character["name"]) for character in self._characters if character.get("name")]

    def get_chapters(self) -> list[dict]:
        return deepcopy(self._chapters)

    def get_all_characters(self) -> list[dict]:
        """Return cards for deterministic validation without exposing mutable state."""
        return deepcopy(self._characters)
