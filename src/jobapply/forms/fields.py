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
  const groupLabel = (el) => {
    const fs = el.closest('fieldset, [role=group], [role=radiogroup], .application-question, .field, li');
    if (!fs) return '';
    const lg = fs.querySelector('legend, .application-label, .field-label, label:not(:has(input))');
    return lg ? lg.innerText : '';
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
  const inputs = Array.from(document.querySelectorAll('input, select, textarea'))
    .filter(el => !(el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'image'));
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
    const combo = isCombobox(el);
    const type = el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (combo ? 'combobox' : (el.type || 'text')));
    let label = (labelFor(el) || '').trim();
    let required = !!(el.required || el.getAttribute('aria-required') === 'true' || /required|✱|\*/.test((el.labels && el.labels[0]) ? el.labels[0].innerText : ''));
    const role = el.getAttribute('role') || '';
    const ariaAutocomplete = el.getAttribute('aria-autocomplete') || '';
    const expanded = el.getAttribute('aria-expanded') || '';
    const controls = el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
    let twin = '';
    if (combo) {
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
      group: (type === 'radio' || type === 'checkbox') ? (groupLabel(el) || '').trim().replace(/\s+/g, ' ').slice(0, 200) : '',
      required,
      value: el.type === 'checkbox' || el.type === 'radio' ? (el.value || '') : '',
      options, hidden, autocomplete: el.autocomplete || '',
      role, aria_autocomplete: ariaAutocomplete, expanded, controls, twin });
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

    def brief(self) -> str:
        """One line for the AI mapper prompt."""
        parts = [f"#{self.idx}", self.type, f"label={self.label!r}" if self.label else "", f"group={self.group!r}" if self.group else "",
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
