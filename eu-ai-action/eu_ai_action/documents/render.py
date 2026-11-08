"""Template -> mini-markdown -> DOCX and PDF. Fixed legal wording lives only in the templates."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from docx import Document as DocxDocument
from jinja2 import Environment, FileSystemLoader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import Document

PENDING = {"en": "[PENDING INPUT]", "es": "[PENDIENTE]"}
BANNER = {
    "en": "DRAFT. The rules and template behind this document have not been reviewed by counsel. Not legal advice.",
    "es": "BORRADOR. Las reglas y la plantilla de este documento no han sido revisadas por un abogado. No constituye asesoramiento jurídico.",
}
BANNER_APPROVED = {"en": "Approved by {who} on {when}. Not legal advice.", "es": "Aprobado por {who} el {when}. No constituye asesoramiento jurídico."}
BANNER_REVIEWED = {"en": "Draft for human review. Not legal advice.", "es": "Borrador para revisión humana. No constituye asesoramiento jurídico."}


@dataclass
class Block:
    kind: str  # h1 h2 p ul table
    text: str = ""
    items: list[str] | None = None
    rows: list[list[str]] | None = None


def parse_blocks(md: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("p", " ".join(para).strip()))
            para.clear()

    for line in md.splitlines():
        s = line.rstrip()
        if not s.strip():
            flush()
        elif s.startswith("# "):
            flush()
            blocks.append(Block("h1", s[2:].strip()))
        elif s.startswith("## "):
            flush()
            blocks.append(Block("h2", s[3:].strip()))
        elif s.startswith("- "):
            flush()
            if blocks and blocks[-1].kind == "ul":
                blocks[-1].items.append(s[2:].strip())  # type: ignore[union-attr]
            else:
                blocks.append(Block("ul", items=[s[2:].strip()]))
        elif s.startswith("|"):
            flush()
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            if blocks and blocks[-1].kind == "table":
                blocks[-1].rows.append(cells)  # type: ignore[union-attr]
            else:
                blocks.append(Block("table", rows=[cells]))
        else:
            para.append(s.strip())
    flush()
    return blocks


def _cell(v: str) -> str:
    """Keep table syntax intact: values may not contain pipes or line breaks."""
    return " ".join(v.replace("|", "/").split())


def context(doc: Document, title: str, who_when: tuple[str, str] | None = None) -> dict:
    lang = doc.language
    f, raw = {}, {}
    for name, fv in doc.fields.items():
        f[name] = _cell(fv.value) if fv.value is not None else PENDING[lang]
        raw[name] = fv.value
    if doc.state == "approved" and who_when:
        banner = BANNER_APPROVED[lang].format(who=who_when[0], when=who_when[1])
    elif not doc.counsel_reviewed:
        banner = BANNER[lang]
    else:
        banner = BANNER_REVIEWED[lang]
    return {"f": f, "raw": raw, "title": title, "banner": banner, "lang": lang}


def render_markdown(templates_dir: str, doc: Document, title: str) -> str:
    env = Environment(loader=FileSystemLoader(str(Path(templates_dir) / doc.language)), autoescape=False, keep_trailing_newline=True)
    tpl = env.get_template(f"{doc.doc_type}.md.j2")
    who_when = (doc.approved_by, doc.approved_at) if doc.state == "approved" else None
    return tpl.render(**context(doc, title, who_when))


def footer_text(doc: Document) -> str:
    return f"{doc.id} | state: {doc.state} | rule set {doc.rule_set_version}"


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _docx_runs(par, text: str) -> None:
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            par.add_run(text[pos : m.start()])
        par.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        par.add_run(text[pos:])


def to_docx(blocks: list[Block], doc: Document) -> bytes:
    d = DocxDocument()
    for b in blocks:
        if b.kind == "h1":
            d.add_heading(b.text, level=1)
        elif b.kind == "h2":
            d.add_heading(b.text, level=2)
        elif b.kind == "p":
            _docx_runs(d.add_paragraph(), b.text)
        elif b.kind == "ul":
            for it in b.items or []:
                _docx_runs(d.add_paragraph(style="List Bullet"), it)
        elif b.kind == "table":
            rows = b.rows or []
            t = d.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, c in enumerate(r):
                    t.cell(i, j).text = c
    d.sections[0].footer.paragraphs[0].text = footer_text(doc)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _rl(text: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", escape(text))


def to_pdf(blocks: list[Block], doc: Document) -> bytes:
    styles = getSampleStyleSheet()
    cell = styles["BodyText"]
    story: list = []
    for b in blocks:
        if b.kind == "h1":
            story.append(Paragraph(_rl(b.text), styles["Title"]))
        elif b.kind == "h2":
            story.append(Paragraph(_rl(b.text), styles["Heading2"]))
        elif b.kind == "p":
            story.append(Paragraph(_rl(b.text), styles["BodyText"]))
        elif b.kind == "ul":
            story.append(ListFlowable([ListItem(Paragraph(_rl(i), styles["BodyText"])) for i in b.items or []], bulletType="bullet"))
        elif b.kind == "table":
            data = [[Paragraph(_rl(c), cell) for c in r] for r in b.rows or []]
            t = Table(data, colWidths=[6 * cm, 10.5 * cm] if len(data[0]) == 2 else None)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "#999999"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(t)
        story.append(Spacer(1, 6))
    ft = footer_text(doc)

    def footer(canvas, _d) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(2 * cm, 1 * cm, ft)
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm).build(
        story, onFirstPage=footer, onLaterPages=footer
    )
    return buf.getvalue()


def render_files(templates_dir: str, doc: Document, title: str) -> dict[str, bytes]:
    blocks = parse_blocks(render_markdown(templates_dir, doc, title))
    return {"docx": to_docx(blocks, doc), "pdf": to_pdf(blocks, doc), "md": render_markdown(templates_dir, doc, title).encode("utf-8")}
