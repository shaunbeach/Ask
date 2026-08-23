"""The welcome screen: a prism splitting white light into a spectrum.

Drawn as a character grid with a colour per cell rather than as art pasted into
a string, because the shape has to be built twice — full size, and compact for a
narrow pane — and hand-drawn ASCII does not survive being resized.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual import events
from textual.widgets import Static

#: Red through violet, the order light comes out of a prism in.
SPECTRUM = ("#f87171", "#fb923c", "#fde047", "#4ade80", "#38bdf8", "#a78bfa")

#: Edge colours: cool at the apex, warming down the right face and cooling down
#: the left, so the two sides read as different faces of the same glass.
APEX_COLOUR = "#cffafe"
EDGE_TOP = "#a5f3fc"
EDGE_LEFT = "#818cf8"
EDGE_RIGHT = "#c084fc"
BEAM_COLOUR = "#e2f4ff"
GLYPH_COLOUR = "#67e8f9"

#: Inside the glass — the app is a terminal assistant, and this is its prompt.
GLYPH = "❯_"

#: Below this many columns the full drawing wraps, which turns it into noise.
COMPACT_BELOW = 50

Cell = tuple[str, str | None]


def _blend(a: str, b: str, t: float) -> str:
    def part(s: str, i: int) -> int:
        return int(s[i : i + 2], 16)

    return "#%02x%02x%02x" % tuple(
        round(part(a, i) + (part(b, i) - part(a, i)) * t) for i in (1, 3, 5)
    )


def build_prism(
    half: int = 9, base_y: int = 8, beam: int = 7, ray: int = 16
) -> list[list[Cell]]:
    """A prism with a beam entering left and a spectrum leaving right.

    `half` is the base's half-width, `base_y` its height, `beam` and `ray` the
    lengths of the incoming and outgoing light.
    """
    apex = half + beam + 1
    width = apex + half + 2 + ray
    height = base_y + 1
    grid: list[list[Cell]] = [[(" ", None) for _ in range(width)] for _ in range(height)]

    def put(x: int, y: int, ch: str, colour: str) -> None:
        if 0 <= x < width and 0 <= y < height:
            grid[y][x] = (ch, colour)

    def edge(y: int) -> int:
        """How far the faces have spread from the apex by row `y`."""
        return round(half * y / base_y)

    beam_y = base_y - 3

    # The incoming beam, stopping where it meets the left face.
    face = apex - edge(beam_y)
    for x in range(face - beam, face):
        put(x, beam_y, "━", BEAM_COLOUR)

    # The spectrum. Each band leaves the right face on its own row, so the face
    # widening as it descends spreads them into a wedge on its own — no diagonal
    # stepping, which at this scale only ever produced broken, colliding lines.
    for i, colour in enumerate(SPECTRUM):
        y = beam_y - 2 + i
        if not 0 <= y < height:
            continue
        start = apex + edge(y) + 2
        for x in range(start, min(start + ray, width)):
            put(x, y, "━", colour)

    # The prism last, so nothing paints over its edges.
    for y in range(base_y + 1):
        t = y / base_y
        put(apex - edge(y), y, "╱", _blend(EDGE_TOP, EDGE_LEFT, t))
        put(apex + edge(y), y, "╲", _blend(EDGE_TOP, EDGE_RIGHT, t))
    for x in range(apex - half + 1, apex + half):
        put(x, base_y, "━", _blend(EDGE_LEFT, EDGE_RIGHT, (x - apex + half) / (2 * half)))
    put(apex, 0, "▲", APEX_COLOUR)

    for i, ch in enumerate(GLYPH):
        put(apex - len(GLYPH) // 2 + i, base_y - 2, ch, GLYPH_COLOUR)

    return grid


def render_prism(compact: bool = False) -> Text:
    grid = build_prism(half=6, base_y=6, beam=4, ray=9) if compact else build_prism()
    text = Text()
    for y, row in enumerate(grid):
        for ch, colour in row:
            text.append(ch, style=colour or "")
        if y < len(grid) - 1:
            text.append("\n")
    return text


class Splash(Static):
    """Shown while the conversation is empty."""

    def __init__(self, model_name: str, provider: str, **kwargs) -> None:
        self._model_name = model_name
        self._provider = provider
        self._compact = False
        super().__init__(**kwargs)

    def on_mount(self) -> None:
        self._compact = self.size.width < COMPACT_BELOW
        self.update(self._build())

    def on_resize(self, event: events.Resize) -> None:
        # Width from the event, not from `self.size`, which is still the
        # pre-resize one while this fires.
        compact = event.size.width < COMPACT_BELOW
        if compact != self._compact:
            self._compact = compact
            self.update(self._build())

    def _build(self) -> Group:
        blank = Text("")
        return Group(
            Align.center(Text("Welcome back!", style="bold #f4f4f5")),
            blank,
            Align.center(render_prism(self._compact)),
            blank,
            Align.center(Text(self._model_name, style="#e4e4e7")),
            Align.center(Text(self._provider, style="#52525b")),
        )

    def refresh_labels(self, model_name: str, provider: str) -> None:
        self._model_name = model_name
        self._provider = provider
        self.update(self._build())
