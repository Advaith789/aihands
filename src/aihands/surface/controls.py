"""What a surface reports and what the model gets to read."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Control:
    """One thing on screen that can be acted on.

    `ref` is a handle for THIS observation only. It is never written into an
    artifact: a recorded flow that depended on it would be a recording of one
    afternoon's DOM, not a capability.
    """

    ref: str
    role: str
    name: str
    context: str = ""
    via: str = "semantic"          # "semantic" | "onclick" | "tabindex" | "pointer"
    tag: str = ""
    type: str = ""
    value: str = ""
    enabled: bool = True
    href: str = ""
    frame_path: tuple[str, ...] = ()

    def line(self) -> str:
        """One line, for the model. Deliberately terse: every token here is paid
        for on every turn of discovery."""
        bits = [f"[{self.ref}]", self.role, f'"{self.name}"']
        if self.value:
            bits.append(f"value={self.value!r}")
        if self.context:
            bits.append(f"({self.context})")
        if not self.enabled:
            bits.append("[disabled]")
        if self.frame_path:
            bits.append(f"frame={'/'.join(self.frame_path)}")
        return " ".join(bits)


@dataclass(frozen=True)
class Readout:
    """A value on screen that can be read but not acted on."""

    ref: str
    label: str
    value: str
    frame_path: tuple[str, ...] = ()

    def line(self) -> str:
        return f'{self.label}: "{self.value}"'


@dataclass(frozen=True)
class FrameInfo:
    """A frame and where it currently points.

    Framesets make the top-level URL a liar: the address bar can sit on /desk
    all afternoon while the content frame moves between records. Anything that
    asserts "we reached the member page" has to read the frame, not the page,
    so the frame URLs are part of what we observe rather than an afterthought.
    """

    path: tuple[str, ...]
    url: str
    area: int = 0


@dataclass
class Observation:
    url: str
    title: str
    controls: list[Control] = field(default_factory=list)
    truncated: int = 0
    frames: list[FrameInfo] = field(default_factory=list)
    readouts: list[Readout] = field(default_factory=list)

    def content_frame(self) -> FrameInfo | None:
        """The frame the operator is actually working in.

        THE single definition. This used to be decided in three places -- the
        planner by area, this class by document order, the distiller by whichever
        frame the clicked control happened to live in -- and they disagreed, so
        a checkpoint recorded from one frame was asserted against another. One
        rule, one place.

        Biggest frame wins: a navigation column and a record pane can hold the
        same number of controls, but not the same number of pixels.
        """
        nested = [f for f in self.frames if f.path]
        return max(nested, key=lambda f: (f.area, len(f.path))) if nested else None

    def content_frame_path(self) -> tuple[str, ...]:
        frame = self.content_frame()
        return frame.path if frame else ()

    def content_url(self) -> str:
        frame = self.content_frame()
        return frame.url if frame else self.url

    def render(self) -> str:
        frame_lines = "".join(
            f"\n  frame {'/'.join(f.path)}: {f.url}" for f in self.frames if f.path
        )
        head = f"URL: {self.url}{frame_lines}\nTITLE: {self.title}\nCONTROLS:"
        body = "\n".join("  " + c.line() for c in self.controls)
        tail = f"\n  ... {self.truncated} further elements omitted" if self.truncated else ""
        reads = ""
        if self.readouts:
            reads = "\nREADOUTS:\n" + "\n".join("  " + r.line() for r in self.readouts)
        return f"{head}\n{body}{tail}{reads}"

    @staticmethod
    def _normalise(ref: str) -> str:
        """Accept a reference however it was written back to us.

        The list renders refs as "[f0:c2]" and a planner may copy the brackets
        along with the id. That is not a mistake worth failing a run over -- it
        names the same control either way. Normalising here fixes it for every
        caller, which is better than a prompt asking more politely.
        """
        return (ref or "").strip().strip("[]").strip()

    def by_ref(self, ref: str) -> Control | None:
        want = self._normalise(ref)
        return next((c for c in self.controls if c.ref == want), None)

    def readout_by_ref(self, ref: str) -> Readout | None:
        want = self._normalise(ref)
        return next((r for r in self.readouts if r.ref == want), None)
