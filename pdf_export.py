"""
PDF export (§14). Pure formatting of data already in the DB — no Claude call.
Uses fpdf2 (pure-Python, no system libs). The bot lays out every heading and the
answer-key formatting from raw fields; nothing free-text goes in unformatted.

fpdf2's core fonts are Latin-1 only, so text is transliterated for a handful of
common medical/typographic symbols rather than bundling a Unicode TTF — keeps the
Railway/Nixpacks build dependency-free while staying readable.
"""
from __future__ import annotations

import os

from fpdf import FPDF

import db
from lesson_generator import AMBIGUITY_LINE
from timeutil import sgt_now

EXPORT_DIR = os.path.join(os.path.dirname(db.DATABASE_PATH) or ".", "exports")

_TRANSLIT = {
    "≥": ">=", "≤": "<=", "→": "->", "←": "<-", "×": "x",
    "·": ".", "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "α": "alpha", "β": "beta",
    "γ": "gamma", "δ": "delta", "μ": "micro", "…": "...",
    "•": "-", "≠": "!=", "±": "+/-", "≈": "~", "°": "deg",
}


def _san(text) -> str:
    s = str(text or "")
    for k, v in _TRANSLIT.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


class _PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")

    def line(self, txt, size=10, style="", h=5):
        """A left-aligned wrapped paragraph that always returns the cursor to the left
        margin (fpdf2's multi_cell otherwise leaves x at the right edge, which breaks the
        next full-width call)."""
        self.set_font("Helvetica", style, size)
        self.multi_cell(0, h, _san(txt), new_x="LMARGIN", new_y="NEXT")

    def gap(self, h=2):
        self.ln(h)


def _ensure_dir():
    os.makedirs(EXPORT_DIR, exist_ok=True)


def _timestamp() -> str:
    return sgt_now().strftime("%Y%m%d-%H%M")


def build_mcq_bank_pdf() -> str:
    """/exportmcqs — whole bank, grouped by topic_area, with a table of contents.
    Returns the archived file path."""
    _ensure_dir()
    rows = db.all_mcqs_with_lesson()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["topic_area"], []).append(r)

    pdf = _PDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page()
    pdf.line("M.Med Anaesthesiology Part A — MCQ Bank", size=18, style="B", h=10)
    pdf.line(f"Generated {sgt_now().strftime('%d %b %Y %H:%M')} SGT  |  {len(rows)} question(s) total")
    pdf.gap(4)
    pdf.line("Contents", size=12, style="B", h=8)
    for area, items in grouped.items():
        pdf.line(f"  {area} — {len(items)} question(s)", size=11)

    for area, items in grouped.items():
        pdf.add_page()
        pdf.line(area, size=15, style="B", h=9)
        pdf.gap(1)
        for n, m in enumerate(items, 1):
            _render_bank_question(pdf, n, m)

    path = os.path.join(EXPORT_DIR, f"{_timestamp()}_mcqbank_all.pdf")
    pdf.output(path)
    return path


def _render_bank_question(pdf: _PDF, n: int, m: dict):
    pdf.line(f"Q{n}. {m['question_text']}", size=11, style="B", h=6)
    for letter in ("a", "b", "c", "d", "e"):
        opt = letter.upper()
        marker = "  <-- correct" if opt == m["correct_option"].upper() else ""
        pdf.line(f"   {opt}. {m['option_' + letter]}{marker}", size=10)
    pdf.line(f"Answer: {m['correct_option'].upper()}. {m['explanation']}", size=9, style="I")
    if m.get("reference_citation"):
        pdf.line(f"Reference: {m['reference_citation']}", size=9, style="I")
    if m.get("lesson_ambiguity_flag"):
        note = m.get("lesson_ambiguity_note")
        if note:
            pdf.line(f"Area of controversy: {note}", size=9, style="I")
        pdf.line(AMBIGUITY_LINE, size=9, style="I")
    pdf.gap(2)


def build_user_export_pdf(user: dict) -> str:
    """/myexport — one user's answered history + current retest-ladder positions."""
    _ensure_dir()
    history = db.answer_history_for_user(user["id"])
    ladder = db.retest_ladder_for_user(user["id"])

    pdf = _PDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.line(f"Revision Report — {user['display_name']}", size=18, style="B", h=10)
    pdf.line(f"Generated {sgt_now().strftime('%d %b %Y %H:%M')} SGT")
    pdf.gap(3)

    pdf.line("Answer history", size=13, style="B", h=8)
    if not history:
        pdf.line("No answers logged yet.")
    for a in history:
        when = a["answered_at"][:10]
        verdict = "correct" if a["is_correct"] else "WRONG"
        pdf.line(f"[{when}] {a['question_text']}", size=10, style="B")
        pdf.line(f"   Your answer: {a['selected_option']}  |  Correct: {a['correct_option']}  |  {verdict}", size=10)
        pdf.gap(1)

    pdf.gap(2)
    pdf.line("Retest pool (spaced-repetition ladder)", size=13, style="B", h=8)
    if not ladder:
        pdf.line("Nothing pending — all clear.")
    for it in ladder:
        stage = it["interval_index"] + 1
        pdf.line(f"- {it['question_text']}", size=10)
        pdf.line(f"   Ladder stage {stage}/4, next due {it['next_eligible_date']}, "
                 f"retested {it['times_retested']}x", size=10)
        pdf.gap(1)

    path = os.path.join(EXPORT_DIR, f"{_timestamp()}_myexport_{user['id']}.pdf")
    pdf.output(path)
    return path
