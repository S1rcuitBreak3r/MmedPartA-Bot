"""
Generates lesson + 5-MCQ content via Claude and renders the Telegram message (§6).

Design principle (ported from the reference bot): Claude returns structured JSON
field VALUES only — the bot owns every character of the rendered message, so format
is identical every time and the renderer is unit-testable without the API.

The JSON contract is enforced by a SEMANTIC validator passed into ask_json (§6/§10),
so a syntactically-valid but contract-violating response triggers a targeted retry.
"""
from __future__ import annotations

import json

from claude_client import ask_json

TOPIC_AREAS = {"Pharmacology", "Physiology", "Equipment", "Others"}

AMBIGUITY_LINE = "Ask an appropriate M.Med examiner to resolve this area of controversy."

PERSONA_SYSTEM_PROMPT = """\
You are writing exam-prep content for candidates sitting the Singapore M.Med \
(Anaesthesiology) Part A examination (single-best-answer MCQs, 5 options A-E).

Rules, no exceptions:
- Content must be accurate to current anaesthesia practice. Where a fact is \
contestable or guideline-dependent, cite a real source (textbook, guideline, or \
primary literature) — omit the citation rather than invent one.
- Never state a fact you are not confident is correct. If a topic has a genuine, \
CURRENT split between textbook/exam-expected teaching and the evidence (two positions \
both still defensible today, not merely older-vs-newer), set ambiguity_flag true and \
explain the tension in ambiguity_note, including which side the exam likely expects.
- Write single-best-answer MCQs: exactly one unambiguously correct option, four \
plausible distractors. Vary the position of the correct option across the five \
questions; do not make it always 'A'.
- The lesson_body should be a focused ~10-minute read: concrete, mechanistic, \
exam-relevant. British spelling, as used in the SG/UK exam tradition.
- No filler, no hype.
"""


def build_validator(valid_topic_titles: set[str]):
    """Return a validate(obj)->None|str closure enforcing the §6 JSON contract."""
    def validate(obj) -> str | None:
        if not isinstance(obj, dict):
            return "top level must be a JSON object"
        for field in ("topic_area", "syllabus_topic", "lesson_title", "lesson_body", "mcqs"):
            if field not in obj:
                return f"missing required field '{field}'"
        if obj["topic_area"] not in TOPIC_AREAS:
            return f"topic_area must be one of {sorted(TOPIC_AREAS)}, got {obj['topic_area']!r}"
        if not str(obj.get("lesson_body", "")).strip():
            return "lesson_body must be non-empty"
        if valid_topic_titles and obj["syllabus_topic"] not in valid_topic_titles:
            return f"syllabus_topic must exactly match the provided topic title"
        if bool(obj.get("ambiguity_flag")) and not str(obj.get("ambiguity_note", "")).strip():
            return "ambiguity_flag is true but ambiguity_note is empty"
        mcqs = obj.get("mcqs")
        if not isinstance(mcqs, list) or len(mcqs) != 5:
            return f"mcqs must be a list of exactly 5 items, got {len(mcqs) if isinstance(mcqs, list) else type(mcqs).__name__}"
        for i, m in enumerate(mcqs):
            if not isinstance(m, dict):
                return f"mcq #{i+1} must be an object"
            for opt in ("question", "option_a", "option_b", "option_c", "option_d", "option_e",
                        "correct_option", "explanation"):
                if opt not in m or not str(m[opt]).strip():
                    return f"mcq #{i+1} field '{opt}' is missing or empty"
            if str(m["correct_option"]).strip().upper() not in {"A", "B", "C", "D", "E"}:
                return f"mcq #{i+1} correct_option must be one of A-E, got {m['correct_option']!r}"
        return None
    return validate


async def generate_lesson_data(topic: dict) -> dict:
    """topic is a syllabus_topics row. Returns the validated JSON contract object (§6)."""
    user_message = f"""\
Write today's Part A lesson and its 5 MCQs.

Subject: {topic['subject']}
Topic (use this EXACT string for the syllabus_topic field): {topic['topic_title']}
User-facing topic_area label to use: {topic['topic_area']}

Respond with ONLY this JSON shape:
{{
  "topic_area": "{topic['topic_area']}",
  "syllabus_topic": "{topic['topic_title']}",
  "lesson_title": "<short>",
  "lesson_body": "<~10-minute read, concrete and exam-focused>",
  "reference_citation": "<a real textbook/guideline reference, or empty string>",
  "ambiguity_flag": false,
  "ambiguity_note": "<the tension and which side the exam expects, or empty string>",
  "mcqs": [
    {{"question": "...", "option_a": "...", "option_b": "...", "option_c": "...",
      "option_d": "...", "option_e": "...", "correct_option": "A",
      "explanation": "...", "reference_citation": "..."}}
  ]
}}
mcqs must contain EXACTLY 5 questions.
"""
    validator = build_validator({topic["topic_title"]})
    # A full lesson_body + 5 MCQs (question+5 options+explanation+citation each) plus JSON
    # overhead comfortably exceeds the original 3000-token budget in practice — give real
    # headroom; you only pay for tokens actually generated, not the ceiling.
    return await ask_json(PERSONA_SYSTEM_PROMPT, user_message, max_tokens=4096, validate=validator)


def render_lesson(sequence_number: int, data: dict) -> str:
    """Bot-owned rendering (§6). Appends the fixed examiner-referral line verbatim when
    ambiguity_flag is true — never paraphrased by the model."""
    citation = str(data.get("reference_citation") or "").strip()
    body = f"""📘 LESSON {sequence_number} — [{data['topic_area']}] {data['lesson_title']}

{data['lesson_body'].strip()}"""

    if citation:
        body += f"\n\nReference: {citation}"

    if bool(data.get("ambiguity_flag")):
        note = str(data.get("ambiguity_note") or "").strip()
        if note:
            body += f"\n\n⚠️ Area of controversy: {note}"
        body += f"\n\n{AMBIGUITY_LINE}"

    body += "\n\n———\nNow answer the 5 MCQs below."
    return body
