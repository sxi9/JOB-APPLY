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
  let i = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'image') continue;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const hidden = style.display === 'none' || style.visibility === 'hidden' || (r.width === 0 && r.height === 0 && el.type !== 'file');
    let selector = '';
    if (el.id && !/^\d|[:.]/.test(el.id)) selector = `#${esc(el.id)}`;
    else if (el.name) selector = `${el.tagName.toLowerCase()}[name="${el.name.replace(/"/g, '\\"')}"]`;
    if (!selector || seen.has(selector)) {
      el.setAttribute('data-jobapply-idx', String(i));
      selector = `[data-jobapply-idx="${i}"]`;
    }
    seen.add(selector);
    let options = [];
    if (el.tagName === 'SELECT') options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 80);
    const type = el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text'));
    out.push({ idx: i, selector, tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '',
      label: (labelFor(el) || '').trim().replace(/\s+/g, ' ').slice(0, 200),
      group: (type === 'radio' || type === 'checkbox') ? (groupLabel(el) || '').trim().replace(/\s+/g, ' ').slice(0, 200) : '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true' || /required|✱|\*/.test((el.labels && el.labels[0]) ? el.labels[0].innerText : '')),
      value: el.type === 'checkbox' || el.type === 'radio' ? (el.value || '') : '',
      options, hidden, autocomplete: el.autocomplete || '' });
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
