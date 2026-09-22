"""Bounded, local view history, deliberately independent of playback state."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class Route:
    kind: str = "library"
    playlist_id: int | None = None
    entity_key: object | None = None
    section: str = "tracks"
    label: str = field(default="Library", compare=False)

    def __post_init__(self) -> None:
        if self.kind != "custom" and self.playlist_id is not None:
            raise ValueError("Only playlist routes may carry a playlist ID")
        if self.kind == "custom" and self.playlist_id is None:
            raise ValueError("A playlist route needs its stable ID")
        if self.kind in {"album_tracks", "artist_tracks"} and self.entity_key is None:
            raise ValueError("A detail route needs its canonical entity key")


@dataclass(frozen=True, slots=True)
class ViewState:
    route: Route
    query: str = ""
    selected_ids: tuple[int, ...] = ()
    current_id: int | None = None
    scroll: int = 0
    sort_column: int = -1
    descending: bool = False
    browser_key: str | None = None


class NavigationHistory:
    def __init__(self, initial: Route | None = None, *, limit: int = 80) -> None:
        self.limit = max(2, int(limit))
        self._routes = [initial or Route()]
        self._position = 0
        self._states: OrderedDict[Route, ViewState] = OrderedDict()

    @property
    def current(self) -> ViewState:
        route = self._routes[self._position]
        return replace(self._states.get(route, ViewState(route)), route=route)

    @property
    def can_back(self) -> bool:
        return self._position > 0

    @property
    def can_forward(self) -> bool:
        return self._position + 1 < len(self._routes)

    def save(self, state: ViewState) -> None:
        if state.route != self.current.route:
            raise ValueError("Cannot save a different route as the current view")
        self._states[state.route] = state
        self._states.move_to_end(state.route)
        while len(self._states) > self.limit:
            self._states.popitem(last=False)

    def visit(self, route: Route) -> ViewState:
        if route == self.current.route:
            self._routes[self._position] = route
            return self.current
        del self._routes[self._position + 1:]
        self._routes.append(route)
        self._routes = self._routes[-self.limit:]
        self._position = len(self._routes) - 1
        return self.current

    def back(self) -> ViewState | None:
        if not self.can_back:
            return None
        self._position -= 1
        return self.current

    def forward(self) -> ViewState | None:
        if not self.can_forward:
            return None
        self._position += 1
        return self.current
