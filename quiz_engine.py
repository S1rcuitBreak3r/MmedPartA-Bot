"""
MCQ quiz engine (§5, §6, §8). Exact-match grading (deterministic — no Claude call,
so no typing indicator needed for grading). All state lives in quiz_runs, so a
Railway restart mid-quiz resumes cleanly from current_index.

questions_json holds a compact list of {"mcq_id", "retest_pool_id"} — the question
text/options are always read fresh from mcq_bank at send time, so a run can never
drift from the canonical MCQ content.

Answers arrive as inline-keyboard callbacks (A-E). The callback carries run_id and
question index, so a stale button tap after a restart, or a double-tap, resolves
against the live active run and is ignored if it doesn't match the current question.
"""
from __future__ import annotations

from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db
from config import RETEST_INTERVALS, FINAL_STAGE_STREAK_TO_RESOLVE
from timeutil import sgt_today, to_iso

_LETTERS = ["A", "B", "C", "D", "E"]


def questions_for_lesson(mcq_rows: list[dict]) -> list[dict]:
    return [{"mcq_id": m["id"], "retest_pool_id": None} for m in mcq_rows]


def questions_for_recap(due_items: list[dict]) -> list[dict]:
    return [{"mcq_id": it["mcq_id"], "retest_pool_id": it["id"]} for it in due_items]


def _keyboard(run_id: int, q_index: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(letter, callback_data=f"ans:{run_id}:{q_index}:{letter}")
        for letter in _LETTERS
    ]
    return InlineKeyboardMarkup([buttons])


def _render_question(mcq: dict, q_index: int, total: int) -> str:
    return (
        f"❓ Question {q_index + 1} of {total}\n\n"
        f"{mcq['question_text']}\n\n"
        f"A. {mcq['option_a']}\n"
        f"B. {mcq['option_b']}\n"
        f"C. {mcq['option_c']}\n"
        f"D. {mcq['option_d']}\n"
        f"E. {mcq['option_e']}"
    )


async def _send_question(bot, chat_id: int, run_id: int, q_index: int, total: int, mcq: dict):
    await bot.send_message(
        chat_id=chat_id,
        text=_render_question(mcq, q_index, total),
        reply_markup=_keyboard(run_id, q_index),
    )


async def start_quiz(bot, user: dict, quiz_type: str, lesson_seq, questions: list[dict]) -> int:
    """Create the run, pause the user (invariant §4/§10), send question 1."""
    run_id = db.create_quiz_run(user["id"], quiz_type, lesson_seq, questions)
    db.set_paused(user["id"], True)
    total = len(questions)
    first = db.get_mcq(questions[0]["mcq_id"])
    label = "RECAP" if quiz_type == "recap" else "QUIZ"
    await bot.send_message(
        chat_id=user["telegram_chat_id"],
        text=f"🧠 {label} — {total} question{'s' if total != 1 else ''}, one at a time. Here we go.",
    )
    await _send_question(bot, user["telegram_chat_id"], run_id, 0, total, first)
    return run_id


def _date_add(days: int) -> str:
    return to_iso(sgt_today() + timedelta(days=days))


async def process_answer(bot, user: dict, run: dict, selected_option: str) -> str:
    """Grade the answer at run['current_index'], update the ladder, send feedback, and
    either advance or finish. Returns 'in_progress' | 'completed'. Caller (bot.py) is
    responsible for the on-completion run_user_check recheck under the scheduler lock.
    """
    idx = run["current_index"]
    q = run["questions"][idx]
    mcq = db.get_mcq(q["mcq_id"])
    total = len(run["questions"])
    selected = selected_option.upper()
    correct_opt = mcq["correct_option"].upper()
    is_correct = selected == correct_opt

    today = to_iso(sgt_today())
    retest_pool_id = q.get("retest_pool_id")

    if retest_pool_id:
        # This was a retest question (§9 recap flow).
        if is_correct:
            db.advance_retest_correct(
                retest_pool_id, RETEST_INTERVALS, FINAL_STAGE_STREAK_TO_RESOLVE, today, _date_add
            )
        else:
            db.upsert_retest_wrong(user["id"], mcq["id"], today, _date_add(1))
    elif not is_correct:
        # Fresh lesson question answered wrong → enters the pool at day 1 (§5).
        retest_pool_id = db.upsert_retest_wrong(user["id"], mcq["id"], today, _date_add(1))

    db.log_answer(run["id"], user["id"], mcq["id"], idx, selected, is_correct, retest_pool_id)

    new_score = run["score"] + (1 if is_correct else 0)

    if is_correct:
        feedback = f"✅ Correct ({correct_opt}). {mcq['explanation']}"
    else:
        feedback = (
            f"❌ Not quite — you chose {selected}, the answer is {correct_opt}.\n"
            f"{mcq['explanation']}"
        )
    await bot.send_message(chat_id=user["telegram_chat_id"], text=feedback)

    next_idx = idx + 1
    if next_idx < total:
        db.update_quiz_progress(run["id"], next_idx, new_score)
        next_mcq = db.get_mcq(run["questions"][next_idx]["mcq_id"])
        await _send_question(bot, user["telegram_chat_id"], run["id"], next_idx, total, next_mcq)
        return "in_progress"

    db.complete_quiz_run(run["id"], new_score)
    db.set_paused(user["id"], False)
    await bot.send_message(
        chat_id=user["telegram_chat_id"],
        text=f"🏁 Done — {new_score}/{total}. Any wrong answers are now scheduled for spaced retest (see /recap).",
    )
    return "completed"


async def skip_quiz(bot, user: dict) -> bool:
    """/skipquiz (§13): end the run at the current point. Already-answered questions keep
    their scoring/retest treatment; unanswered ones are simply not scored and not added
    to the retest pool. Clears the pause, reports the partial score."""
    run = db.get_active_quiz_run(user["id"])
    if not run:
        return False
    answered = run["current_index"]  # questions fully processed so far
    db.complete_quiz_run(run["id"], run["score"])
    db.set_paused(user["id"], False)
    await bot.send_message(
        chat_id=user["telegram_chat_id"],
        text=f"⏭ Quiz ended early — {run['score']}/{answered} answered scored. "
             f"The remaining questions were skipped (not added to retest).",
    )
    return True


async def cancel_quiz(bot, user: dict) -> bool:
    """/cancelquiz (§13): discard the run entirely, and clear is_paused so the user is not
    left stuck paused with no run to finish (the §4 invariant)."""
    run = db.get_active_quiz_run(user["id"])
    if not run:
        return False
    db.cancel_quiz_run(run["id"], status="cancelled")
    db.set_paused(user["id"], False)
    await bot.send_message(chat_id=user["telegram_chat_id"], text="🚫 Quiz cancelled.")
    return True
