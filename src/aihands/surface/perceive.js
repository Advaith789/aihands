// Perception: what the model is allowed to see of a page.
//
// Runs once per frame, per observation. Returns a flat list of controls with a
// role, a name, and enough context to tell two similar controls apart.
//
// Why a union of two passes rather than one:
//
//   Pass A -- SEMANTIC. Elements the page itself declares as controls: real
//   inputs, buttons, links, anything carrying an explicit role. On a decently
//   built form this is everything, and the names are excellent because the
//   application author wrote them for screen readers.
//
//   Pass B -- BEHAVIOURAL. Elements that are not declared as controls but
//   behave like them: an onclick handler, a tabindex, a pointer cursor. Legacy
//   back-office software is full of these -- a <td> that navigates when you
//   click it is the canonical case. Pass A cannot see them at all.
//
// A fixed list of "control-ish tags" only ever finds pass A, which is why that
// approach needs a code change every time an unfamiliar app shows up. "Has a
// click handler" is a property, not an enumeration, so pass B generalises.
//
// Names are then synthesised for anything pass B found without one, because an
// unnamed control cannot be targeted durably -- and a locator that cannot be
// written durably is a capability that breaks next month.

(() => {
  const MAX_ROWS_PER_TABLE = 4;   // bound memory by page STRUCTURE, not page size
  const MAX_CONTROLS = 120;
  const MAX_TEXT = 80;

  const clip = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, MAX_TEXT);

  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  // ---- role -------------------------------------------------------------
  const IMPLICIT_ROLE = {
    A: "link", BUTTON: "button", SELECT: "combobox", TEXTAREA: "textbox",
    H1: "heading", H2: "heading", H3: "heading",
  };
  const INPUT_ROLE = {
    submit: "button", button: "button", reset: "button",
    checkbox: "checkbox", radio: "radio", text: "textbox",
    password: "textbox", email: "textbox", number: "spinbutton", search: "searchbox",
  };

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().toLowerCase();
    if (el.tagName === "INPUT") return INPUT_ROLE[(el.type || "text").toLowerCase()] || "textbox";
    if (el.tagName === "A" && !el.getAttribute("href")) return "generic";
    return IMPLICIT_ROLE[el.tagName] || null;
  }

  // ---- accessible name --------------------------------------------------
  // A deliberately simplified accname chain. Not the full W3C algorithm -- it
  // covers the cases these applications actually use, and anything it misses
  // falls through to synthesis below rather than producing a wrong answer.
  function accName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return clip(aria);

    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const txt = labelledby.split(/\s+/)
        .map((id) => (document.getElementById(id) || {}).innerText || "")
        .join(" ");
      if (txt.trim()) return clip(txt);
    }

    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.innerText.trim()) return clip(lab.innerText);
    }

    const wrapping = el.closest("label");
    if (wrapping && wrapping.innerText.trim()) return clip(wrapping.innerText);

    // A submit button's label lives in its value attribute, not its text.
    if (el.tagName === "INPUT" && ["submit", "button", "reset"].includes((el.type || "").toLowerCase())) {
      if (el.value) return clip(el.value);
    }

    for (const attr of ["title", "alt", "placeholder"]) {
      const v = el.getAttribute(attr);
      if (v && v.trim()) return clip(v);
    }

    if (["A", "BUTTON", "TD", "TH", "H1", "H2", "H3"].includes(el.tagName)) {
      if (el.innerText && el.innerText.trim()) return clip(el.innerText);
    }
    return "";
  }

  // ---- name synthesis ---------------------------------------------------
  // For a control the page never named. The column header is the strongest
  // signal available in a table-based UI: it is what a human reads to know
  // what the cell means, and it survives restyling.
  function synthesise(el) {
    const cell = el.closest("td, th");
    if (cell) {
      const row = cell.closest("tr");
      const table = cell.closest("table");
      if (row && table) {
        const idx = Array.prototype.indexOf.call(row.cells, cell);
        const headRow = table.rows[0];
        if (headRow && headRow !== row && headRow.cells[idx]) {
          const header = clip(headRow.cells[idx].innerText);
          if (header) return { name: clip(cell.innerText), context: `column "${header}"` };
        }
      }
      return { name: clip(cell.innerText), context: "table cell" };
    }
    const heading = (() => {
      let n = el;
      while (n && n !== document.body) {
        let s = n.previousElementSibling;
        while (s) {
          if (/^H[1-6]$/.test(s.tagName) && s.innerText.trim()) return clip(s.innerText);
          s = s.previousElementSibling;
        }
        n = n.parentElement;
      }
      return "";
    })();
    return { name: clip(el.innerText), context: heading ? `under "${heading}"` : "" };
  }

  // ---- is this a control? ----------------------------------------------
  const SEMANTIC = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA"]);

  function behavioural(el) {
    if (el.hasAttribute("onclick")) return "onclick";
    const ti = el.getAttribute("tabindex");
    if (ti !== null && parseInt(ti, 10) >= 0) return "tabindex";
    if (getComputedStyle(el).cursor === "pointer" && clip(el.innerText)) return "pointer";
    return null;
  }

  // ---- walk -------------------------------------------------------------
  const out = [];
  const seen = new Set();          // O(1) dedupe across both passes
  const rowsPerTable = new Map();  // bounded row sampling
  let truncated = 0;
  let ref = 0;

  const all = document.querySelectorAll("*");
  for (const el of all) {
    if (out.length >= MAX_CONTROLS) { truncated++; continue; }
    if (seen.has(el)) continue;

    const isSemantic = SEMANTIC.has(el.tagName) || el.hasAttribute("role");
    const via = isSemantic ? "semantic" : behavioural(el);
    if (!via) continue;
    if (!visible(el)) continue;

    // A repeated results table can be arbitrarily long. Keep a sample plus a
    // count: the model does not need row 200 to decide what to click, and the
    // token cost of sending it is real.
    const table = el.closest("table");
    if (table) {
      const n = (rowsPerTable.get(table) || 0);
      const row = el.closest("tr");
      if (row && row !== table.rows[0]) {
        if (n >= MAX_ROWS_PER_TABLE * 3) { truncated++; continue; }
        rowsPerTable.set(table, n + 1);
      }
    }

    let role = roleOf(el);
    let name = accName(el);
    let context = "";

    // Name and context are separate problems. A table cell names itself from
    // its own text, which accName already returns -- but "M-1001" alone does
    // not say WHICH column it came from, and two cells in the same row are
    // otherwise indistinguishable. So anything found behaviourally always gets
    // its context computed, whether or not it already had a name.
    if (!name || via !== "semantic") {
      const s = synthesise(el);
      if (!name) name = s.name;
      context = s.context;
    }
    if (!role) role = via === "semantic" ? "generic" : "clickable";
    if (!name) continue;   // an unnameable control cannot be targeted durably

    seen.add(el);
    const id = `c${++ref}`;
    el.setAttribute("data-ah-ref", id);   // transient handle, never recorded

    out.push({
      ref: id,
      role,
      name,
      context,
      via,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || "",
      value: ("value" in el ? String(el.value ?? "") : "").slice(0, MAX_TEXT),
      enabled: !el.disabled,
      href: el.getAttribute("href") || "",
    });
  }

  // ---- readouts ---------------------------------------------------------
  // Controls are what you can act on; readouts are what you can read. A record
  // view is mostly the latter, and a capability that extracts a confirmation
  // number needs to see it before it can promise it as an output. Restricted to
  // the two-column Label | Value shape because that is what these screens are
  // built from, and because an unbounded text dump is expensive on every turn.
  const readouts = [];
  let rref = 0;
  for (const row of document.querySelectorAll("tr")) {
    if (readouts.length >= 25) break;
    if (row.cells.length !== 2) continue;
    const label = clip(row.cells[0].innerText).replace(/:$/, "");
    const value = clip(row.cells[1].innerText);
    if (!label || !value) continue;
    if (label.length > 40) continue;              // a paragraph is not a label
    if (row.querySelector("input, button, a, select, textarea")) continue;
    const id = `r${++rref}`;
    row.cells[1].setAttribute("data-ah-ref", id);
    readouts.push({ ref: id, label, value });
  }

  return {
    url: location.href, title: document.title, truncated, controls: out, readouts,
    // How much screen this frame occupies. In a frameset the navigation column
    // is narrow and the content pane is the rest, so area is what tells them
    // apart -- control counts tie constantly, and document order is an
    // accident of how the frameset happened to load.
    area: (window.innerWidth || 0) * (window.innerHeight || 0),
  };
})();
