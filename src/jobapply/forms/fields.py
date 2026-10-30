"""Form field extraction: one compact record per input, from a Playwright page (or frame)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Runs in the browser. Returns every visible-ish control with a stable selector and its label.
EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1');
  const labelFor = (el) => {
    if (el.labels && el.labels[0]) return el.labels[0].innerText;
    if (el.id) { const l = document.querySelector(`label[for="${esc(el.id)}"]`); if (l) return l.innerText; }
    const al = el.getAttribute('aria-label'); if (al) return al;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return lb.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
    const wrap = el.closest('label, .field, .application-question, .form-group, fieldset, li, div');
    if (wrap) {
      const lg = wrap.querySelector('legend, label, .application-label, .field-label, h3, h4, .text, p');
      if (lg && lg !== el) return lg.innerText;
    }
    return el.placeholder || '';
  };
  const GROUP_SEL = 'fieldset, [role=group], [role=radiogroup], .application-question, .field, li, '
    + '[data-automation-id^="formField-"]';
  const groupLabel = (el) => {
    // The nearest group container first; when it holds no question text (Workday's role=radiogroup div inside
    // the fieldset), the next enclosing one, up to three levels.
    let fs = el.closest(GROUP_SEL);
    for (let d = 0; fs && d < 3; d++, fs = fs.parentElement ? fs.parentElement.closest(GROUP_SEL) : null) {
      const lg = fs.querySelector('legend, .application-label, .field-label');
      if (lg) return lg.innerText;
      // A label that is not one of the radios' own ("Yes" / "No" with a `for` pointing at a radio): Workday
      // puts the question in a label / legend of the formField container and each radio's text in its own label.
      for (const l of fs.querySelectorAll('label')) {
        if (l.querySelector('input')) continue;
        const target = l.getAttribute('for') ? document.getElementById(l.getAttribute('for')) : null;
        if (target && (target.type === 'radio' || target.type === 'checkbox')) continue;
        return l.innerText;
      }
    }
    return '';
  };
  // Workday's single-select dropdown is neither <select> nor role=combobox: a <button aria-haspopup="listbox">
  // whose text is the current value ("India", "Select One"); clicking it opens a popup of [role=option] /
  // [data-automation-id=menuItem] rows. Extracted as type "listbox".
  const isListbox = (el) => (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') &&
    el.getAttribute('aria-haspopup') === 'listbox';
  const LISTBOX_PLACEHOLDER = /^(select( one| an? option|\.{3}|…)?|choose( one)?|please select|-+|none|--)?$/i;
  // Workday's multi-select prompt: a text input (placeholder "Search", list icon) inside multiSelectContainer;
  // the chosen values are [data-automation-id=selectedItem] chips next to it, the options only exist while
  // its popup is open (typed text + Enter). Extracted as a combobox with multiselect=true.
  const formFieldOf = (el) => el.closest('[data-automation-id^="formField-"]');
  const chipText = (n) => {
    const c = n.cloneNode(true);
    c.querySelectorAll('button, svg, [aria-hidden="true"]').forEach(x => x.remove());
    return (c.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const chipsOf = (box) => box ? Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]')).map(chipText).filter(Boolean) : [];
  const multiBoxOf = (el) => {
    if (el.tagName !== 'INPUT' || !(el.type === 'text' || el.type === '')) return null;
    const ms = el.closest('[data-automation-id="multiSelectContainer"]');
    if (ms) return ms;
    const ff = formFieldOf(el);
    if (!ff) return null;
    if (ff.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"]')) return ff;
    if (/^search$/i.test(el.placeholder || '') && ff.querySelector('svg, [class*="icon"], button[aria-label]')) return ff;
    return null;
  };
  // Custom dropdowns (React-Select, Ashby, Workday) are a text input with role=combobox / aria-autocomplete
  // whose options only exist in the DOM while the menu is open. They have no <label for> as often as not,
  // so the label is resolved from aria-labelledby / aria-describedby or the nearest visible label text.
  const isCombobox = (el) => el.tagName === 'INPUT' && (el.type === 'text' || el.type === '') &&
    (el.getAttribute('role') === 'combobox' || /^(list|both)$/.test(el.getAttribute('aria-autocomplete') || ''));
  const idsText = (attr) => (attr || '').split(/\s+/).filter(Boolean)
    .map(id => { const n = document.getElementById(id); return n && !/placeholder|error/.test(id) ? (n.innerText || '').trim() : ''; })
    .filter(Boolean).join(' ');
  const nearestLabel = (el) => {
    let n = el;
    for (let d = 0; d < 6 && n && n !== document.body; d++, n = n.parentElement) {
      const lg = n.querySelector('label, legend, .application-label, .field-label, [class*="label"]');
      if (lg && lg !== el && lg.innerText && lg.innerText.trim()) return lg.innerText;
    }
    return '';
  };
  // A text input that only mirrors a combobox for validation, never one the user is meant to type into:
  // React-Select's requiredInput (tabindex -1, opacity 0, class *required*) or an ATS question_<n> box.
  const looksMirror = (x) => {
    if (!(x.type === 'text' || x.type === '')) return false;
    if (x.tabIndex === -1 || x.getAttribute('aria-hidden') === 'true') return true;
    if (/required/i.test(x.className) || /^question_\d+$/.test(x.id)) return true;
    const st = window.getComputedStyle(x);
    const r = x.getBoundingClientRect();
    return st.opacity === '0' || st.pointerEvents === 'none' || (r.width === 0 && r.height === 0);
  };
  // The input that shares a small container with a combobox and looks like its validation mirror. A real
  // field beside it (a phone number next to a country-code picker, a postal code next to a city) is not a
  // twin: it keeps its own label, is filled on its own and is reported when left empty.
  const twinOf = (el) => {
    let n = el.parentElement;
    for (let d = 0; d < 6 && n && n.tagName !== 'FORM' && n !== document.body; d++, n = n.parentElement) {
      const inputs = Array.from(n.querySelectorAll('input')).filter(x => !['hidden', 'submit', 'button', 'image', 'file', 'checkbox', 'radio'].includes(x.type));
      if (inputs.length < 2) continue;
      if (inputs.length === 2) {
        const other = inputs.find(x => x !== el) || null;
        return other && looksMirror(other) ? other : null;
      }
      return null;
    }
    return null;
  };
  const inputs = Array.from(document.querySelectorAll(
      'input, select, textarea, button[aria-haspopup="listbox"], [role="button"][aria-haspopup="listbox"]'))
    .filter(el => isListbox(el) || !(el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'image'));
  const twins = new Map();  // non-combobox input -> its combobox
  for (const el of inputs) {
    if (!isCombobox(el)) continue;
    const t = twinOf(el);
    if (t && !isCombobox(t)) twins.set(t, el);
  }
  // Stable selectors first, so a twin can name its combobox whichever of the two comes first in the DOM.
  const bySelector = new Map();
  inputs.forEach((el, i) => {
    let selector = '';
    if (el.id && !/^\d|[:.]/.test(el.id)) selector = `#${esc(el.id)}`;
    else if (el.name) selector = `${el.tagName.toLowerCase()}[name="${el.name.replace(/"/g, '\\"')}"]`;
    if (!selector || seen.has(selector)) {
      el.setAttribute('data-jobapply-idx', String(i));
      selector = `[data-jobapply-idx="${i}"]`;
    }
    seen.add(selector);
    bySelector.set(el, selector);
  });
  let i = 0;
  for (const el of inputs) {
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    let hidden = style.display === 'none' || style.visibility === 'hidden' || (r.width === 0 && r.height === 0 && el.type !== 'file');
    const selector = bySelector.get(el);
    let options = [];
    if (el.tagName === 'SELECT') options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 80);
    const listbox = isListbox(el);
    const multiBox = listbox ? null : multiBoxOf(el);
    const combo = !listbox && (isCombobox(el) || !!multiBox);
    const type = listbox ? 'listbox' : (el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (combo ? 'combobox' : (el.type || 'text'))));
    let label = (labelFor(el) || '').trim();
    const ownLabel = (el.labels && el.labels[0]) ? el.labels[0].innerText : '';
    const REQUIRED_MARK = /required|✱|\*/;
    let required = !!(el.required || el.getAttribute('aria-required') === 'true' || REQUIRED_MARK.test(ownLabel));
    let value = el.type === 'checkbox' || el.type === 'radio' ? (el.value || '') : '';
    // A radio / checkbox question's asterisk sits in the group's legend or label, not on the inputs.
    const group = (type === 'radio' || type === 'checkbox') ? (groupLabel(el) || '').trim().replace(/\s+/g, ' ').slice(0, 200) : '';
    if (group && REQUIRED_MARK.test(group)) required = true;
    if (listbox) {
      const shown = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      value = LISTBOX_PLACEHOLDER.test(shown) ? '' : shown;
      if (!label) label = idsText(el.getAttribute('aria-labelledby')) || (nearestLabel(el) || '').trim();
      if (REQUIRED_MARK.test(label)) required = true;  // the asterisk of an aria-labelledby / nearby label
      const ff = formFieldOf(el);
      const ffLabel = ff ? ff.querySelector('label') : null;
      if (ffLabel && REQUIRED_MARK.test(ffLabel.innerText || '')) required = true;
    } else if (multiBox) {
      value = chipsOf(multiBox).join('; ');
      const ff = formFieldOf(el) || multiBox;
      const ffLabel = ff.querySelector('label');
      // No label at all, or only the input's placeholder ("Search") when the container's <label> has no `for`.
      if (ffLabel && (!label || label === (el.placeholder || '').trim())) label = (ffLabel.innerText || '').trim();
      if (ffLabel && REQUIRED_MARK.test(ffLabel.innerText || '')) required = true;
    }
    const role = el.getAttribute('role') || '';
    const ariaAutocomplete = el.getAttribute('aria-autocomplete') || '';
    const expanded = el.getAttribute('aria-expanded') || '';
    const controls = el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
    let twin = '';
    if (combo && !multiBox) {
      if (!label) label = idsText(el.getAttribute('aria-labelledby')) || idsText(el.getAttribute('aria-describedby'));
      const t = twinOf(el);
      if (t && !isCombobox(t)) {
        if (!label) label = (labelFor(t) || '').trim();
        required = required || !!(t.required || t.getAttribute('aria-required') === 'true');
      }
      if (!label) label = (nearestLabel(el) || '').trim();
    } else if (twins.has(el)) {
      // The twin is never filled directly: the combobox is the control the user sees.
      const c = twins.get(el);
      twin = bySelector.get(c) || '';
      if (!label) label = (labelFor(c) || idsText(c.getAttribute('aria-labelledby')) || nearestLabel(c) || '').trim();
      hidden = true;
    }
    out.push({ idx: i, selector, tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '',
      label: label.replace(/\s+/g, ' ').slice(0, 200),
      group,
      required,
      value,
      options, hidden, autocomplete: el.autocomplete || '',
      role, aria_autocomplete: ariaAutocomplete, expanded, controls, twin, multiselect: !!multiBox });
    i += 1;
  }
  return out;
}
"""

CAPTCHA_JS = r"""
() => {
  const sel = ['iframe[src*="recaptcha"]', '.g-recaptcha[data-sitekey]', 'iframe[src*="hcaptcha"]', '.h-captcha',
               'iframe[src*="turnstile"]', '.cf-turnstile', '[data-captcha]', 'iframe[src*="arkoselabs"]'];
  for (const s of sel) { const el = document.querySelector(s); if (el) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return s; } }
  return '';
}
"""


@dataclass
class FormField:
    idx: int
    selector: str
    tag: str
    type: str
    name: str = ""
    id: str = ""
    label: str = ""
    group: str = ""
    required: bool = False
    value: str = ""
    options: list[str] = field(default_factory=list)
    hidden: bool = False
    autocomplete: str = ""
    # ARIA of custom dropdowns: role=combobox, aria-autocomplete list/both, aria-expanded, aria-controls/owns.
    role: str = ""
    aria_autocomplete: str = ""
    expanded: str = ""
    controls: str = ""
    # Set on the input that mirrors a combobox (React-Select's required twin, a hidden question_<n> input):
    # the combobox's selector. Such a field is reported hidden and never filled on its own.
    twin: str = ""
    # A Workday multi-select prompt (type "combobox"): the chosen values are chips beside the input, the
    # options come from a popup opened with typed text + Enter. `value` holds the current chips ("; " joined).
    multiselect: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FormField:
        return cls(**{k: d.get(k, getattr(cls, k, "") if k != "options" else []) for k in cls.__dataclass_fields__})

    @property
    def question(self) -> str:
        """The text a human would read as this field's question."""
        return (self.group or self.label or self.name or self.id).strip()

    @property
    def is_file(self) -> bool:
        return self.type == "file"

    @property
    def is_choice(self) -> bool:
        return self.type in ("radio", "checkbox")

    @property
    def is_combobox(self) -> bool:
        return self.type == "combobox"

    @property
    def is_listbox(self) -> bool:
        """A `<button aria-haspopup="listbox">` single-select (Workday): its text is the current value."""
        return self.type == "listbox"

    def brief(self) -> str:
        """One line for the AI mapper prompt."""
        parts = [f"#{self.idx}", self.type, "multiselect" if self.multiselect else "",
                 f"label={self.label!r}" if self.label else "", f"group={self.group!r}" if self.group else "",
                 f"name={self.name!r}" if self.name and not self.label else "", "required" if self.required else ""]
        if self.options:
            parts.append("options=" + " | ".join(self.options[:25]))
        if self.is_choice and self.value:
            parts.append(f"value={self.value!r}")
        return " ".join(p for p in parts if p)


def extract_fields(page_or_frame) -> list[FormField]:
    raw = page_or_frame.evaluate(EXTRACT_JS)
    return [FormField.from_dict(d) for d in raw]


def detect_captcha(page) -> str:
    return page.evaluate(CAPTCHA_JS) or ""


_LABEL_PREFIX = re.compile(r"^label=", re.I)


def match_selector(fields: list[FormField], selector: str) -> list[FormField]:
    """Fields a profile selector refers to. `label=` selectors match on label/group text."""
    if _LABEL_PREFIX.match(selector):
        needle = _LABEL_PREFIX.sub("", selector).strip().lower()
        return [f for f in fields if needle in f.label.lower() or needle in f.group.lower()]
    return [f for f in fields if f.selector == selector or _css_matches(f, selector)]


def _css_matches(f: FormField, selector: str) -> bool:
    """Cheap matcher for the selector shapes profiles use, without a DOM: tag[name=x], #id, tag[type=x]."""
    m = re.fullmatch(r"(?P<tag>input|select|textarea)?(?P<attrs>(\[[^\]]+\])*)", selector.strip())
    if m and m.group("attrs"):
        if m.group("tag") and m.group("tag") != f.tag:
            return False
        for attr in re.findall(r"\[([^\]]+)\]", m.group("attrs")):
            if "=" not in attr:
                return False
            k, v = attr.split("=", 1)
            v = v.strip().strip("\"'")
            op = ""
            if k.endswith(("*", "^", "$")):
                op, k = k[-1], k[:-1]
            actual = {"name": f.name, "id": f.id, "type": f.type}.get(k.strip())
            if actual is None:
                return False
            if op == "*" and v not in actual:
                return False
            if op == "^" and not actual.startswith(v):
                return False
            if op == "$" and not actual.endswith(v):
                return False
            if op == "" and actual != v:
                return False
        return True
    if selector.startswith("#") and re.fullmatch(r"#[\w-]+", selector):
        return f.id == selector[1:]
    return False
