"""Playwright-backed web surface.

The only implementation we ship. It exists behind a narrow interface -- observe,
resolve, act -- so that a desktop driver over UIA or AX could take its place
without the artifact schema or the replay engine knowing. That seam is the point;
a second implementation is not.

Frames are walked explicitly rather than assumed away. The target application
uses a real frameset, and so do a great many of the systems this stands in for.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from typing import Any

from playwright.async_api import Browser, Frame, Page, async_playwright

from ..schema.outcomes import AihandsError, Code, ControlAmbiguous, ControlNotFound
from .controls import Control, FrameInfo, Observation, Readout


class SurfaceError(AihandsError):
    code = Code.SURFACE_ERROR


@dataclass
class Resolution:
    """A control the surface has found and is willing to act on."""

    locator: object
    strategy: str
    candidate_index: int
    match_count: int
    frame_path: tuple[str, ...] = ()


#: Installed when a human takes the session. Records what they touched, never
#: what they typed into a field marked sensitive -- the audit trail needs to
#: show that a password box was filled, not what went into it.
HUMAN_CAPTURE_JS = """() => {
  if (window.__ah_capture_installed) return true;
  window.__ah_capture_installed = true;
  const describe = (el) => {
    if (!el || !el.tagName) return {};
    const label = el.id
      ? (document.querySelector(`label[for="${CSS.escape(el.id)}"]`) || {}).innerText
      : null;
    return {
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: ((el.getAttribute('aria-label') || label || el.value ||
              el.innerText || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 60),
    };
  };
  // Reported out of the page immediately rather than buffered in it. A buffer
  // on `window` dies with the document, and in these applications every click
  // navigates -- so the actions we most need to record are exactly the ones
  // that would be lost.
  const send = (rec) => { try { window.__ah_record(rec); } catch (_) {} };
  document.addEventListener('click', (e) => {
    send({t: Date.now(), kind: 'click', ...describe(e.target)});
  }, true);
  document.addEventListener('change', (e) => {
    const d = describe(e.target);
    const secret = d.type === 'password';
    send({t: Date.now(), kind: 'change', ...d,
          value: secret ? '<withheld>' : String(e.target.value || '').slice(0, 60)});
  }, true);
  return true;
}"""


#: A short structural path, used only as a last-resort locator. Capped in depth
#: because a twelve-level selector is not more precise, only more brittle.
CSS_PATH_JS = """(ref) => {
  const el = document.querySelector(`[data-ah-ref="${ref}"]`);
  if (!el) return null;
  const parts = [];
  let n = el;
  while (n && n.nodeType === 1 && parts.length < 6 && n.tagName !== 'BODY') {
    if (n.id) { parts.unshift('#' + CSS.escape(n.id)); break; }
    let sel = n.tagName.toLowerCase();
    const sibs = n.parentElement
      ? Array.from(n.parentElement.children).filter(c => c.tagName === n.tagName) : [];
    if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(n) + 1})`;
    parts.unshift(sel);
    n = n.parentElement;
  }
  return parts.join(' > ');
}"""


@dataclass
class _ProbeTarget:
    """A one-candidate stand-in so probing can reuse the real resolver rather
    than a parallel implementation that could disagree with it."""

    candidates: list
    frame_path: tuple[str, ...] = ()
    on_ambiguous: str = "fail"


#: Finds the value cell of a `Label | Value` row, and tags it.
ROW_LABEL_JS = """(args) => {
  const {label, marker} = args;
  document.querySelectorAll('[data-ah-find]').forEach(el => el.removeAttribute('data-ah-find'));
  const want = (s) => (s || '').replace(/\\s+/g, ' ').trim().replace(/:$/, '');
  for (const row of document.querySelectorAll('tr')) {
    if (row.cells.length < 2) continue;
    if (want(row.cells[0].innerText) === want(label)) {
      row.cells[1].setAttribute('data-ah-find', marker);
    }
  }
  return document.querySelectorAll('[data-ah-find]').length;
}"""

#: Finds the cell holding `text` under the column headed `column`, and tags it.
COLUMN_TEXT_JS = """(args) => {
  const {text, column, marker} = args;
  document.querySelectorAll('[data-ah-find]').forEach(el => el.removeAttribute('data-ah-find'));
  const want = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  for (const table of document.querySelectorAll('table')) {
    const head = table.rows[0];
    if (!head) continue;
    let idx = -1;
    for (let i = 0; i < head.cells.length; i++) {
      if (want(head.cells[i].innerText) === want(column)) { idx = i; break; }
    }
    if (idx < 0) continue;
    for (let r = 1; r < table.rows.length; r++) {
      const cell = table.rows[r].cells[idx];
      if (cell && want(cell.innerText) === want(text)) cell.setAttribute('data-ah-find', marker);
    }
  }
  return document.querySelectorAll('[data-ah-find]').length;
}"""


_SCRIPT = (Path(__file__).resolve().parent / "perceive.js").read_text()


def _frame_path(frame: Frame) -> tuple[str, ...]:
    """Names from the root down. This is what a recorded target carries so that
    replay can descend the same way instead of guessing which document it is in."""
    parts: list[str] = []
    node: Frame | None = frame
    while node is not None and node.parent_frame is not None:
        parts.append(node.name or "")
        node = node.parent_frame
    return tuple(reversed(parts))


class WebSurface:
    def __init__(self, page: Page, browser: Browser | None = None, pw=None) -> None:
        self.page = page
        self._browser = browser
        self._pw = pw
        self._non_get = 0
        self._capture_on = False
        self._human_actions: list[dict] = []

    def _note_request(self, request) -> None:
        if request.method != "GET":
            self._non_get += 1

    @property
    def non_get_count(self) -> int:
        return self._non_get

    # -- lifecycle ------------------------------------------------------

    @classmethod
    async def launch(cls, headless: bool = True) -> "WebSurface":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        surface = cls(page, browser, pw)
        # Whether an action committed anything is answerable from the wire: a
        # GET fetched a page, a POST changed something. Guessing from button
        # labels would be a heuristic about English; this is the actual request
        # the application made.
        page.on("request", surface._note_request)
        return surface

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    # -- observe --------------------------------------------------------

    async def observe(self) -> Observation:
        """Run the perception pass in every frame and merge the results.

        Refs are namespaced per frame, so two frames that each produce "c1"
        stay distinguishable without a second round trip to disambiguate.
        """
        controls: list[Control] = []
        readouts: list[Readout] = []
        frames: list[FrameInfo] = []
        truncated = 0
        for index, frame in enumerate(self.page.frames):
            frame_info_index = len(frames)
            frames.append(FrameInfo(path=_frame_path(frame), url=frame.url))
            try:
                data = await frame.evaluate(_SCRIPT)
            except Exception:
                # A frame can be mid-navigation or cross-origin. Skipping it is
                # correct: it is not part of what we can act on right now.
                continue
            truncated += data.get("truncated", 0)
            frames[frame_info_index] = FrameInfo(
                path=_frame_path(frame), url=frame.url, area=int(data.get("area", 0)))
            path = _frame_path(frame)
            for raw in data.get("readouts", []):
                readouts.append(Readout(ref=f"f{index}:{raw['ref']}", label=raw["label"],
                                        value=raw["value"], frame_path=path))
            for raw in data.get("controls", []):
                controls.append(
                    Control(
                        ref=f"f{index}:{raw['ref']}",
                        role=raw["role"],
                        name=raw["name"],
                        context=raw.get("context", ""),
                        via=raw.get("via", "semantic"),
                        tag=raw.get("tag", ""),
                        type=raw.get("type", ""),
                        value=raw.get("value", ""),
                        enabled=raw.get("enabled", True),
                        href=raw.get("href", ""),
                        frame_path=path,
                    )
                )
        return Observation(
            url=self.page.url, title=await self.page.title(),
            controls=controls, truncated=truncated, frames=frames, readouts=readouts,
        )

    # -- act ------------------------------------------------------------

    def _locator(self, control: Control):
        frame_index = int(control.ref.split(":", 1)[0][1:])
        frame = self.page.frames[frame_index]
        local_ref = control.ref.split(":", 1)[1]
        return frame.locator(f'[data-ah-ref="{local_ref}"]')

    async def click(self, control: Control) -> None:
        before = self._content_url()
        await self._locator(control).click()
        await self.settle(before)

    async def fill(self, control: Control, text: str) -> None:
        await self._locator(control).fill(text)

    # -- waiting --------------------------------------------------------

    def _content_url(self) -> str:
        nested = [f for f in self.page.frames if f.parent_frame is not None]
        return nested[-1].url if nested else self.page.url

    async def settle(self, before_url: str, start_window: float = 0.6,
                     finish_timeout: int = 8000) -> None:
        """Wait for whatever the last action started to finish.

        A frameset makes page-level waiting a trap: submitting a form inside a
        frame never navigates the top-level document, so page.wait_for_load_state
        returns immediately and the next observation reads the OLD screen. The
        race is silent and intermittent, which is the worst kind.

        So: watch the content frame for a navigation to begin (bounded, because
        plenty of actions correctly navigate nowhere), then wait for every frame
        to finish loading. No fixed sleeps -- a sleep long enough to be safe is
        always long enough to be slow.
        """
        deadline = time.monotonic() + start_window
        while time.monotonic() < deadline:
            if self._content_url() != before_url:
                break
            await asyncio.sleep(0.02)

        for frame in self.page.frames:
            try:
                await frame.wait_for_load_state("domcontentloaded", timeout=finish_timeout)
            except Exception:
                # A frame that detached mid-navigation is not an error: it is
                # gone, and the next observation simply will not include it.
                continue

    async def read(self, control: Control) -> str:
        return (await self._locator(control).inner_text()).strip()

    # -- resolving a recorded target ------------------------------------

    def _frame_for(self, path: tuple[str, ...]):
        """Find the frame a recorded target named.

        Matched by name path rather than index: frame order is an accident of
        how the document happened to load, and an artifact that depended on it
        would break the first time a frame was added.
        """
        if not path:
            return self.page.main_frame
        for frame in self.page.frames:
            if _frame_path(frame) == tuple(path):
                return frame
        raise ControlNotFound(f"frame {'/'.join(path)!r} is not present")

    async def _candidate_locator(self, frame, cand: dict, marker: str):
        strategy = cand["strategy"]
        if strategy == "role_name":
            return frame.get_by_role(cand["role"], name=cand["name"], exact=cand.get("exact", True))
        if strategy == "label":
            return frame.get_by_label(cand["text"], exact=True)
        if strategy == "link_text":
            return frame.get_by_role("link", name=cand["text"], exact=True)
        if strategy == "css":
            return frame.locator(cand["value"])
        if strategy == "row_label":
            await frame.evaluate(ROW_LABEL_JS, {"label": cand["label"], "marker": marker})
            return frame.locator(f'[data-ah-find="{marker}"]')
        if strategy == "column_text":
            # No Playwright primitive expresses "the cell holding X under the
            # column headed Y", so we tag matches in the page and select the
            # tag. The attribute is scratch state, cleared on the next
            # resolution and never recorded.
            await frame.evaluate(COLUMN_TEXT_JS, {
                "text": cand["text"], "column": cand["column"], "marker": marker,
            })
            return frame.locator(f'[data-ah-find="{marker}"]')
        raise ControlNotFound(f"unknown locator strategy {strategy!r}")

    async def resolve(self, target, timeout_ms: int = 8000) -> "Resolution":
        """Walk the recorded candidates, best first, and stop at the first that
        resolves to exactly one element.

        A candidate that matches several elements is skipped rather than
        accepted, because a more specific candidate further down the list may
        still be unambiguous. Only if every candidate is either absent or
        ambiguous do we decide which of the two it was -- and ambiguity is
        reported distinctly, because clicking one of three matching rows in a
        banking UI is how you action the wrong member's account.
        """
        frame = self._frame_for(tuple(target.frame_path))
        saw_ambiguous = False
        details: list[str] = []

        for index, cand in enumerate(target.candidates):
            raw = cand.model_dump(mode="json") if hasattr(cand, "model_dump") else dict(cand)
            marker = f"m{index}_{int(time.monotonic() * 1000) % 100000}"
            try:
                locator = await self._candidate_locator(frame, raw, marker)
                count = await locator.count()
            except Exception as exc:
                details.append(f"{raw['strategy']}: {type(exc).__name__}")
                continue

            details.append(f"{raw['strategy']}={count}")
            if count == 1:
                return Resolution(locator=locator, strategy=raw["strategy"],
                                  candidate_index=index, match_count=1,
                                  frame_path=tuple(target.frame_path))
            if count > 1:
                saw_ambiguous = True

        summary = ", ".join(details) or "no candidates"
        if saw_ambiguous and target.on_ambiguous == "fail":
            raise ControlAmbiguous(
                f"target matched multiple elements and none uniquely ({summary})"
            )
        raise ControlNotFound(f"target did not resolve ({summary})")

    # -- conditions ------------------------------------------------------

    async def evaluate(self, condition) -> bool:
        kind = condition.kind
        try:
            frame = self._frame_for(tuple(condition.frame_path))
        except ControlNotFound:
            # The frame a condition names is not on screen. That is an answer,
            # not an error: nothing can be present in a frame that does not
            # exist. Outcome detectors run against every screen of a run,
            # including ones before the frameset has loaded, so raising here
            # would make a detector for a later screen break an earlier step.
            return kind.endswith("_absent")

        if kind == "frame_url_matches":
            return bool(re.search(condition.value or "", frame.url))

        if kind in ("text_present", "text_absent"):
            try:
                body = await frame.locator("body").inner_text(timeout=2000)
            except Exception:
                body = ""
            hit = (condition.value or "").lower() in body.lower()
            return hit if kind == "text_present" else not hit

        if kind in ("control_present", "control_absent"):
            try:
                await self.resolve(condition.target)
                found = True
            except (ControlNotFound, ControlAmbiguous):
                found = False
            return found if kind == "control_present" else not found

        raise SurfaceError(f"unknown condition kind {kind!r}")

    async def wait_for(self, condition, timeout_ms: int = 8000, poll_ms: int = 100) -> bool:
        """Poll a condition to a deadline.

        Polling rather than a fixed sleep: a sleep long enough to be safe on a
        slow load is always long enough to be wasteful on a fast one, and every
        step pays it.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self.evaluate(condition):
                return True
            await asyncio.sleep(poll_ms / 1000)
        return await self.evaluate(condition)

    async def reload_frame(self, frame_path: tuple[str, ...] = ()) -> None:
        """Re-request the current document of one frame.

        Safe by construction: it repeats a GET. We never reload as a way of
        retrying something that posted, because that is how a form gets
        submitted twice.
        """
        frame = self._frame_for(tuple(frame_path))
        before = self._content_url()
        await frame.goto(frame.url, wait_until="domcontentloaded")
        await self.settle(before)

    # -- acting on a resolved target -------------------------------------

    async def act(self, resolution, action) -> Any:
        """Perform one recorded action. Returns a value only for `read`.

        Every mutating action settles before returning, so the caller never has
        to remember to wait -- forgetting is silent and intermittent, which is
        the worst failure mode a test can have.
        """
        kind = action.type
        if kind == "navigate":
            before = self._content_url()
            await self.page.goto(action.url, wait_until="domcontentloaded")
            await self.settle(before)
            return None
        if kind == "click":
            before = self._content_url()
            await resolution.locator.click(timeout=8000)
            await self.settle(before)
            return None
        if kind == "fill":
            await resolution.locator.fill(action.value, timeout=8000)
            return None
        if kind == "read":
            text = (await resolution.locator.inner_text(timeout=8000)).strip()
            if action.extract:
                m = re.search(action.extract, text)
                if not m:
                    raise SurfaceError(
                        f"extract pattern {action.extract!r} did not match {text[:60]!r}"
                    )
                return (m.group(1) if m.groups() else m.group(0)).strip()
            return text
        if kind == "wait":
            return await self.wait_for(action.until, action.timeout_ms)
        raise SurfaceError(f"unknown action {kind!r}")

    # -- probing: measuring how durable a locator actually is -------------

    async def css_path(self, ref: str) -> str | None:
        frame_index = int(ref.split(":", 1)[0][1:])
        frame = self.page.frames[frame_index]
        return await frame.evaluate(CSS_PATH_JS, ref.split(":", 1)[1])

    async def probe(self, item, frame_path: tuple[str, ...]) -> list[tuple[dict, int]]:
        """Build every locator that could plausibly find this element again, and
        measure each one against the live page.

        This is the difference between recording a locator and recording a
        *measured* locator. A candidate that matched three elements at record
        time was never going to work on replay, and we know that now rather
        than in six weeks. Only survivors reach the artifact.
        """
        from ..schema.capability import (ColumnTextLocator, CssLocator, LabelLocator,
                                         LinkTextLocator, RoleNameLocator, RowLabelLocator)

        candidates: list = []
        role = getattr(item, "role", "")
        name = getattr(item, "name", "")
        tag = getattr(item, "tag", "")
        context = getattr(item, "context", "")

        if hasattr(item, "label"):                       # a readout
            candidates.append(RowLabelLocator(label=item.label))
        else:
            # A real accessibility role is the most durable handle there is,
            # so it goes first when the element actually has one. "clickable"
            # and "generic" are our own inventions and cannot be queried.
            if role not in ("clickable", "generic", "") and name:
                candidates.append(RoleNameLocator(role=role, name=name))
            if tag in ("input", "select", "textarea") and name:
                candidates.append(LabelLocator(text=name))
            if role == "link" and name:
                candidates.append(LinkTextLocator(text=name))
            if context.startswith('column "') and name:
                candidates.append(ColumnTextLocator(text=name, column=context[8:-1]))

        css = await self.css_path(item.ref)
        if css:
            candidates.append(CssLocator(value=css))

        measured: list[tuple[dict, int]] = []
        for cand in candidates:
            probe_target = _ProbeTarget(candidates=[cand], frame_path=frame_path)
            try:
                await self.resolve(probe_target)
                count = 1
            except ControlAmbiguous:
                count = 2
            except Exception:
                count = 0
            measured.append((cand.model_dump(mode="json"), count))
        return measured

    # -- human handoff ----------------------------------------------------

    async def start_human_capture(self) -> None:
        """Begin recording what a person does on this page.

        The operator drives the real browser with a real mouse -- not a
        constrained action vocabulary -- because "take control" that can only do
        the four things automation can do is not taking control. The trade is
        that we must observe them rather than mediate them.

        Events are pushed straight out of the page through a binding, and the
        listener is re-installed on every new document. Buffering inside the
        page loses everything the moment a click navigates, which in these
        applications is every click that matters.
        """
        if self._capture_on:
            return

        async def record(source, action: dict) -> None:
            frame = source.get("frame")
            path = "/".join(_frame_path(frame)) if frame is not None else ""
            self._human_actions.append({**action, "frame": path})

        await self.page.expose_binding("__ah_record", record)
        await self.page.add_init_script(HUMAN_CAPTURE_JS)
        for frame in self.page.frames:
            try:
                await frame.evaluate(HUMAN_CAPTURE_JS)
            except Exception:
                continue
        self._capture_on = True

    async def collect_human_actions(self) -> list[dict]:
        """Drain what the person did into the same audit trail as the automated
        steps. A handoff nobody can review afterwards is a gap in the record."""
        collected = sorted(self._human_actions, key=lambda a: a.get("t", 0))
        self._human_actions = []
        return collected
