from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gi.repository import Atspi


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def overlaps(self, other: Rect) -> bool:
        return (self.x < other.x + other.width and other.x < self.x + self.width
                and self.y < other.y + other.height and other.y < self.y + self.height)


@dataclass
class Control:
    element: Atspi.Accessible
    rect: Rect
    role: str
    name: str
    focused: bool
    showing: bool
    scope: Atspi.Accessible | None = None

    @property
    def content(self) -> bool:
        return (self.role == "link" and self.rect.width >= 80
                and (self.rect.height >= 60 or (self.rect.width >= 250 and self.rect.height >= 30)))


def neighbor(current: Control, controls: list[Control], direction: str) -> Control | None:
    horizontal = direction in {"left", "right"}
    forward = direction in {"right", "down"}
    cx, cy = current.rect.center
    candidates = []
    for index, control in enumerate(controls):
        if control is current:
            continue
        x, y = control.rect.center
        primary = x - cx if horizontal else y - cy
        if (primary if forward else -primary) <= 1:
            continue
        if horizontal:
            overlap = min(current.rect.y + current.rect.height, control.rect.y + control.rect.height) \
                - max(current.rect.y, control.rect.y)
            secondary = abs(y - cy)
            tolerance = min(current.rect.height, control.rect.height) * 0.1
        else:
            overlap = min(current.rect.x + current.rect.width, control.rect.x + control.rect.width) \
                - max(current.rect.x, control.rect.x)
            secondary = abs(x - cx)
            tolerance = min(current.rect.width, control.rect.width) * 0.1
        score = (overlap < -tolerance,
                 current.content and current.rect.height >= current.rect.width and not control.content,
                 abs(primary) + secondary * 3, index)
        candidates.append((score, control))
    return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def initial(controls: list[Control]) -> Control:
    showing = [control for control in controls if control.showing]
    if not showing:
        raise RuntimeError("Stremio has no visible selectable controls")
    content = [control for control in showing if control.content]
    return min(content or showing, key=lambda control: (control.rect.y, control.rect.x))


def accessibility():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, GLib
    except (ImportError, ValueError) as error:
        raise RuntimeError(
            "Stremio focus navigation needs system Python with python-gobject and at-spi2-core; "
            "run the plugin's setup.sh"
        ) from error
    Atspi.set_timeout(500, 1500)
    return Atspi, GLib


def snapshot(atspi) -> list[Control]:
    desktop = atspi.get_desktop(0)
    if desktop is None:
        raise RuntimeError("The desktop accessibility service is unavailable")
    apps = [desktop.get_child_at_index(index) for index in range(desktop.get_child_count())]
    apps = [app for app in apps if app and app.get_name().casefold() in {
        "stremio", "com.stremio.stremio",
    }]
    if len(apps) != 1:
        raise RuntimeError("No unique Stremio accessibility application; open Stremio and close duplicate windows")
    apps[0].set_cache_mask(
        atspi.Cache.CHILDREN | atspi.Cache.ROLE | atspi.Cache.NAME
        | atspi.Cache.STATES | atspi.Cache.INTERFACES | atspi.Cache.PARENT
    )
    queue = deque([(apps[0], None, None)])
    controls = []
    documents = []
    modals = []
    visited = 0
    while queue:
        element, document, scope = queue.popleft()
        if element is None:
            continue
        visited += 1
        if visited > 5000:
            raise RuntimeError("Stremio accessibility tree exceeds the navigation safety limit")
        states = element.get_state_set()
        if states.contains(atspi.StateType.DEFUNCT):
            continue
        role = element.get_role_name()
        if role == "document web":
            if not states.contains(atspi.StateType.SHOWING):
                continue
            component = element.get_component_iface()
            bounds = component.get_extents(atspi.CoordType.SCREEN)
            document = Rect(bounds.x, bounds.y, bounds.width, bounds.height)
            documents.append(document)
        if document is not None:
            if (role in {"dialog", "alert", "menu", "list box"} or states.contains(atspi.StateType.MODAL)) \
                    and states.contains(atspi.StateType.SHOWING):
                scope = element
                modals.append(element)
            if (states.contains(atspi.StateType.FOCUSABLE)
                    and states.contains(atspi.StateType.VISIBLE)
                    and states.contains(atspi.StateType.SENSITIVE)
                    and role not in {"document web", "dialog", "alert", "landmark"}):
                component = element.get_component_iface()
                bounds = component.get_extents(atspi.CoordType.SCREEN)
                rect = Rect(bounds.x, bounds.y, bounds.width, bounds.height)
                # WebKit marks Stremio's transparent full-window file-drop input as showing.
                if (rect.width > 2 and rect.height > 2
                        and rect.width * rect.height < document.width * document.height * 0.85):
                    controls.append(Control(
                        element, rect, role, element.get_name(),
                        states.contains(atspi.StateType.FOCUSED),
                        states.contains(atspi.StateType.SHOWING) and rect.overlaps(document), scope,
                    ))
        queue.extend((element.get_child_at_index(index), document, scope)
                     for index in range(element.get_child_count()))
    if len(documents) > 1:
        raise RuntimeError("Multiple visible Stremio web views; refusing ambiguous navigation")
    if modals:
        controls = [control for control in controls if control.scope == modals[-1]]
    return controls


def navigate(action: str, atspi, glib=None) -> dict:
    deadline = time.monotonic() + (4 if action == "initialize" else 0)
    context = glib.MainContext.default() if glib is not None else None
    while True:
        if context is not None:
            while context.pending():
                context.iteration(False)
        controls = snapshot(atspi)
        if action == "back":
            return {"activate": False, "key": "Escape" if any(control.scope is not None for control in controls)
                    else "BackSpace"}
        if controls:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Stremio exposes no selectable controls; wait for the view to load and retry")
        time.sleep(0.1)
    current = next((control for control in controls if control.focused), None)
    activate = action == "select" and current is not None
    if current is None:
        target = initial(controls)
    elif action in {"initialize", "select"}:
        target = current
    else:
        target = neighbor(current, controls, action) or current
    if target is not current and not target.element.get_component_iface().grab_focus():
        raise RuntimeError("Stremio refused to focus the selected control")
    return {"activate": activate, "name": target.name[:160], "role": target.role,
            "bounds": [target.rect.x, target.rect.y, target.rect.width, target.rect.height]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("initialize", "up", "down", "left", "right", "select", "back"))
    args = parser.parse_args()
    try:
        atspi, glib = accessibility()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        # Keep cached focus and visibility current while a new view loads.
        listener = atspi.EventListener.new(lambda event, *args: None)
        if not listener.register("object:state-changed"):
            raise RuntimeError("Cannot subscribe to Stremio accessibility state updates")
        print(json.dumps(navigate(args.action, atspi, glib)))
    except (RuntimeError, glib.Error) as error:
        print(f"Stremio focus navigation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
