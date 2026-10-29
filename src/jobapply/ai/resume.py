"""Render a JSON Resume (master basics + tailored variant) to PDF with fpdf2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fpdf import FPDF

from jobapply.ai.base import ResumeVariant

FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]


class _Pdf(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4")
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=16)
        self.family = "Helvetica"
        self.unicode = False
        for regular, bold in FONT_CANDIDATES:
            if Path(regular).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold if Path(bold).exists() else regular)
                self.family, self.unicode = "Body", True
                break

    def text_(self, text: str) -> str:
        return text if self.unicode else text.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False) -> None:
        self.set_font(self.family, "B" if bold else "", size)

    def heading(self, text: str) -> None:
        self.ln(3)
        self.font(11, bold=True)
        self.set_text_color(30, 30, 30)
        self.cell(0, 6, self.text_(text.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(120, 120, 120)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.set_text_color(0, 0, 0)

    def para(self, text: str, size: float = 10) -> None:
        self.font(size)
        self.multi_cell(0, 5, self.text_(text), new_x="LMARGIN", new_y="NEXT")

    def bullet(self, text: str) -> None:
        self.font(10)
        self.cell(5, 5, self.text_("•" if self.unicode else "-"))
        self.multi_cell(0, 5, self.text_(text), new_x="LMARGIN", new_y="NEXT")


def _date_range(start: str, end: str) -> str:
    if not start and not end:
        return ""
    return f"{start or ''} – {end or 'present'}"


def render_resume_pdf(basics: dict[str, Any], variant: ResumeVariant, path: Path) -> Path:
    pdf = _Pdf()
    pdf.add_page()
    pdf.font(18, bold=True)
    pdf.cell(0, 9, pdf.text_(basics.get("name", "")), new_x="LMARGIN", new_y="NEXT")
    pdf.font(11)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 6, pdf.text_(variant.label), new_x="LMARGIN", new_y="NEXT")
    loc = basics.get("location") or {}
    contact = [basics.get("email"), basics.get("phone"), loc.get("city") if isinstance(loc, dict) else loc]
    contact += [p.get("url") for p in basics.get("profiles", []) if p.get("url")]
    pdf.font(9)
    pdf.multi_cell(0, 5, pdf.text_("  ·  ".join(c for c in contact if c)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    if variant.summary:
        pdf.heading("Summary")
        pdf.para(variant.summary)
    if variant.skills:
        pdf.heading("Skills")
        for group in variant.skills:
            if group.keywords:
                pdf.font(10, bold=True)
                pdf.cell(pdf.get_string_width(pdf.text_(group.name)) + 2, 5, pdf.text_(group.name))
                pdf.font(10)
                pdf.multi_cell(0, 5, pdf.text_(": " + ", ".join(group.keywords)), new_x="LMARGIN", new_y="NEXT")
    if variant.work:
        pdf.heading("Experience")
        for w in variant.work:
            pdf.font(10.5, bold=True)
            pdf.cell(0, 5.5, pdf.text_(f"{w.position} · {w.name}".strip(" ·")), new_x="LMARGIN", new_y="NEXT")
            dates = _date_range(w.startDate, w.endDate)
            if dates:
                pdf.font(9)
                pdf.set_text_color(90, 90, 90)
                pdf.cell(0, 4.5, pdf.text_(dates), new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
            if w.summary:
                pdf.para(w.summary)
            for h in w.highlights:
                pdf.bullet(h)
            pdf.ln(1.5)
    if variant.education:
        pdf.heading("Education")
        for e in variant.education:
            line = " · ".join(x for x in (e.studyType, e.area, e.institution, e.endDate) if x)
            pdf.para(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path
