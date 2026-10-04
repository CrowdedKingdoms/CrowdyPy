"""Crowdy Studio's pane layout, headless: which panes are open and how big, persisted the
way CrowdyJS persists it (``ck:crowdy-studio:layout:v1``, the same JSON), so a Python tool
and the browser Studio read each other's layout. Checked against CrowdyCPP's shared fixture.
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol

__all__ = [
    "STUDIO_LAYOUT_STORAGE_KEY",
    "STUDIO_PANE_IDS",
    "StudioLayoutController",
    "StudioLayoutStorage",
    "clamp_studio_pane_size",
    "studio_pane_size_range",
]

StudioPaneId = Literal["explorer", "settings", "agent", "bottom"]

STUDIO_LAYOUT_STORAGE_KEY = "ck:crowdy-studio:layout:v1"
STUDIO_PANE_IDS: tuple[StudioPaneId, ...] = ("explorer", "settings", "agent", "bottom")

_DEFAULT_VISIBLE: dict[str, bool] = {
    "explorer": True,
    "settings": False,
    "agent": False,
    "bottom": False,
}
_DEFAULT_SIZES: dict[str, int] = {"explorer": 230, "settings": 280, "agent": 340, "bottom": 180}
_RANGES: dict[str, tuple[int, int]] = {
    "explorer": (160, 480),
    "settings": (220, 480),
    "agent": (280, 620),
    "bottom": (96, 480),
}


class StudioLayoutStorage(Protocol):
    def get_item(self, key: str) -> str | None: ...

    def set_item(self, key: str, value: str) -> None: ...


def studio_pane_size_range(pane: StudioPaneId) -> tuple[int, int]:
    """``(min, max)`` pixels for a pane."""
    return _RANGES[pane]


def clamp_studio_pane_size(pane: StudioPaneId, size: float) -> int:
    """Clamp to the pane's range and round half up, as JavaScript's ``Math.round`` does; a
    non-finite size is the pane's default."""
    if not math.isfinite(size):
        return _DEFAULT_SIZES[pane]
    low, high = _RANGES[pane]
    return math.floor(min(high, max(low, size)) + 0.5)


class StudioLayoutController:
    def __init__(
        self,
        *,
        storage: StudioLayoutStorage | None = None,
        storage_key: str = STUDIO_LAYOUT_STORAGE_KEY,
        defaults: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._key = storage_key
        self._listeners: list[Callable[[dict[str, dict[str, Any]]], Any]] = []
        self._visible: dict[str, bool] = {
            **_DEFAULT_VISIBLE,
            **dict((defaults or {}).get("visible", {})),
        }
        self._sizes: dict[str, int] = dict(_DEFAULT_SIZES)
        for pane, size in dict((defaults or {}).get("sizes", {})).items():
            if pane in self._sizes:
                self._sizes[pane] = clamp_studio_pane_size(pane, size)  # type: ignore[arg-type]
        self._load()

    def get_state(self) -> dict[str, dict[str, Any]]:
        return {"visible": dict(self._visible), "sizes": dict(self._sizes)}

    def is_visible(self, pane: StudioPaneId) -> bool:
        return self._visible[pane]

    def pane_size(self, pane: StudioPaneId) -> int:
        return self._sizes[pane]

    def subscribe(self, listener: Callable[[dict[str, dict[str, Any]]], Any]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def set_visible(self, pane: StudioPaneId, visible: bool) -> None:
        if self._visible[pane] == visible:
            return
        self._visible = {**self._visible, pane: visible}
        self._persist()
        self._emit()

    def toggle(self, pane: StudioPaneId) -> None:
        self.set_visible(pane, not self._visible[pane])

    def set_size(self, pane: StudioPaneId, size: float, persist: bool = True) -> None:
        clamped = clamp_studio_pane_size(pane, size)
        if self._sizes[pane] == clamped:
            if persist:
                self._persist()
            return
        self._sizes = {**self._sizes, pane: clamped}
        if persist:
            self._persist()
        self._emit()

    def _emit(self) -> None:
        state = self.get_state()
        for listener in list(self._listeners):
            listener(state)

    def _load(self) -> None:
        if self._storage is None:
            return
        try:
            raw = self._storage.get_item(self._key)
        except Exception:
            return
        if not raw:
            return
        try:
            record = json.loads(raw)
        except ValueError:
            return  # a corrupt layout leaves the defaults in effect
        if not isinstance(record, dict):
            return
        visible: dict[str, Any] = (
            record["visible"] if isinstance(record.get("visible"), dict) else {}
        )
        sizes: dict[str, Any] = record["sizes"] if isinstance(record.get("sizes"), dict) else {}
        for pane in STUDIO_PANE_IDS:
            shown = visible.get(pane)
            if isinstance(shown, bool):
                self._visible[pane] = shown
            size = sizes.get(pane)
            if (
                isinstance(size, (int, float))
                and not isinstance(size, bool)
                and math.isfinite(size)
            ):
                self._sizes[pane] = clamp_studio_pane_size(pane, size)

    def _persist(self) -> None:
        if self._storage is None:
            return
        with contextlib.suppress(Exception):  # unavailable storage keeps the layout in memory
            self._storage.set_item(self._key, self._json())

    def _json(self) -> str:
        """The persisted form, byte-identical to CrowdyJS's ``JSON.stringify``."""
        return json.dumps({"visible": self._visible, "sizes": self._sizes}, separators=(",", ":"))
