"""
Orchestration (§8). One per-user check function, `_check_user`, invoked from FIVE
call sites — the AM/PM crons, the hourly safety net, startup, and the on-quiz-
completion recheck — all serialized by ONE asyncio.Lock so concurrent generation of
the same shared lesson_queue sequence can't race the UNIQUE constraint.

Delivery is gated by a PACE MARKER (timeutil): every slot boundary grants one credit,
each delivery consumes exactly one (marker advances by one slot, never jumps to "now"),
which is what makes catch-up deliver one session per completion instead of a flood.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.error import TelegramError

import curriculum
import db
import lesson_generator
import quiz_engine
from claude_client import ClaudeError
from config import (
    MAX_RETRIES, RETRY_BACKOFF_SECONDS, TIMEZONE,
    AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE, PM_TRIGGER_HOUR, PM_TRIGGER_MINUTE, SAFETY_NET_MINUTE,
    OVERDUE_NUDGE_THRESHOLD_DAYS, FAILURE_ALERT_THRESHOLD, FAILURE_ALERT_COOLDOWN_HOURS,
)
from timeutil import (
    sgt_now, sgt_today, to_iso, from_iso, slot_of, slot_marker, marker_to_fields,
    fields_to_marker, initial_pace_marker,
)
from typing_util import typing_indicator

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3500

# The single serialization lock, held across every per-user check and every generation.
_run_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Delivery with chunking + retry (ported from the reference bot, per-chat)
# --------------------------------------------------------------------------- #

def _chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, remaining = [], text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_with_retry(bot, chat_id: int, text: str):
    for chunk in _chunk_text(text):
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await bot.send_message(chat_id=chat_id, text=chunk)
                last_exc = None
                break
            except TelegramError as exc:
                last_exc = exc
                logger.warning("Telegram send failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        if last_exc is not None:
            raise last_exc


# --------------------------------------------------------------------------- #
# Lesson generation — the persist-before-send durability point (§8)
# --------------------------------------------------------------------------- #

async def ensure_sequence_generated(seq: int, bot=None, chat_id=None):
    """Return the lesson_queue row for `seq`, generating+persisting it first if absent.
    Callers MUST hold _run_lock. When bot/chat_id are given, a typing indicator is shown
    during the (potentially multi-second) Claude call."""
    existing = db.get_lesson_by_seq(seq)
    if existing:
        return existing

    topic = curriculum.choose_next_topic()
    if bot is not None and chat_id is not None:
        async with typing_indicator(bot, chat_id):
            data = await lesson_generator.generate_lesson_data(topic)
    else:
        data = await lesson_generator.generate_lesson_data(topic)

    rendered = lesson_generator.render_lesson(seq, data)
    try:
        db.insert_lesson_and_mcqs(
            seq=seq,
            syllabus_topic_id=topic["id"],
            topic_area=data["topic_area"],
            content_json=json.dumps(data, ensure_ascii=False),
            rendered_text=rendered,
            reference_citation=str(data.get("reference_citation") or "") or None,
            ambiguity_flag=bool(data.get("ambiguity_flag")),
            ambiguity_note=str(data.get("ambiguity_note") or "") or None,
            mcqs=data["mcqs"],
            source="ai_generated",
        )
        db.mark_topic_covered(topic["id"], seq)
    except Exception:
        # Another holder may have inserted concurrently (shouldn't happen under the lock,
        # but be defensive): fall back to whatever is now persisted.
        existing = db.get_lesson_by_seq(seq)
        if existing:
            return existing
        raise
    return db.get_lesson_by_seq(seq)


def _overdue_nudge_line(user_id: int) -> str | None:
    cutoff = to_iso(sgt_today() - timedelta(days=OVERDUE_NUDGE_THRESHOLD_DAYS))
    n = db.overdue_retest_count(user_id, cutoff)
    if n > 0:
        return f"🔔 You have {n} item(s) overdue for retest — run /recap when you get a chance."
    return None


# --------------------------------------------------------------------------- #
# The per-user check (§8 pseudocode). Caller holds _run_lock.
# --------------------------------------------------------------------------- #

async def _check_user(bot, user: dict, now):
    user = db.get_user_by_id(user["id"]) or user
    uid = user["id"]
    chat_id = user["telegram_chat_id"]
    prog = db.get_progress(uid)

    pace_marker = fields_to_marker(prog.get("pace_date"), prog.get("pace_slot"))
    if pace_marker is None:
        pace_marker = initial_pace_marker(now)
        pd, ps = marker_to_fields(pace_marker)
        db.set_pace_marker(uid, pd, ps)

    current_marker = slot_marker(now)

    # (1) Due-check first, so no-op ticks are silent and reminders can be throttled.
    if not (pace_marker < current_marker):
        return

    # (2) A delivery is due, but an unanswered quiz blocks it → remind, once per slot.
    if user["is_paused"] or db.get_active_quiz_run(uid):
        cur_marker_str = ":".join(marker_to_fields(current_marker))
        if prog.get("last_reminder_marker") == cur_marker_str:
            return
        run = db.get_active_quiz_run(uid)
        if run and run["quiz_type"] == "recap":
            msg = "You have an unanswered recap question. Reply to continue before the next lesson."
        else:
            msg = "You have unanswered questions from your last lesson. Reply to continue before the next lesson."
        await send_with_retry(bot, chat_id, msg)
        db.set_last_reminder_marker(uid, cur_marker_str)
        return

    # (3) Deliver the next lesson.
    next_seq = prog["current_sequence_number"] + 1
    lesson = await ensure_sequence_generated(next_seq, bot=bot, chat_id=chat_id)  # persist point

    rendered = lesson["rendered_text"]
    nudge = _overdue_nudge_line(uid)
    if nudge:
        rendered = f"{rendered}\n\n{nudge}"

    await send_with_retry(bot, chat_id, rendered)  # delivery AFTER persistence

    mcqs = db.get_mcqs_for_seq(next_seq)
    quiz_type = "daily_" + slot_of(now)
    questions = quiz_engine.questions_for_lesson(mcqs)
    await quiz_engine.start_quiz(bot, user, quiz_type, next_seq, questions)

    # Consume exactly one slot credit (never jump to "now"), then persist progress.
    new_pace = pace_marker + 1
    if new_pace == current_marker:
        last_slot = marker_to_fields(new_pace)[1]      # caught up: label with the real slot
    else:
        last_slot = "catchup"                           # still behind
    pd, ps = marker_to_fields(new_pace)
    db.record_delivery(uid, next_seq, to_iso(now.date()) + "T" + now.strftime("%H:%M:%S"),
                       last_slot, pd, ps)


async def _pre_generate_leading(bot):
    """Efficiency (§8): pre-generate the leading candidate's next sequence so the common
    delivery path is a cache hit, not a live Claude call. Caller holds _run_lock. Cost is
    identical (same generation, just earlier). Best-effort — a failure here never aborts."""
    candidates = db.list_active_candidates()
    if not candidates:
        return
    leading = max(c_prog["current_sequence_number"]
                  for c_prog in (db.get_progress(c["id"]) for c in candidates))
    target = leading + 1
    if db.get_lesson_by_seq(target) is None:
        try:
            await ensure_sequence_generated(target)  # no typing indicator; background work
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-generation of sequence %s failed (non-fatal): %s", target, exc)


# --------------------------------------------------------------------------- #
# Admin failure alerting (§8)
# --------------------------------------------------------------------------- #

async def _handle_user_failure(bot, user: dict, exc: Exception):
    uid = user["id"]
    count = db.increment_failures(uid)
    logger.error("run_user_check failed for user %s (%s): %s", uid, user.get("display_name"), exc)
    if count < FAILURE_ALERT_THRESHOLD:
        return
    prog = db.get_progress(uid)
    last_alert = prog.get("last_failure_alert_at")
    if last_alert:
        try:
            from datetime import datetime, timezone
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_alert)
            if delta.total_seconds() < FAILURE_ALERT_COOLDOWN_HOURS * 3600:
                return
        except ValueError:
            pass
    admin = db.get_admin()
    if not admin or not admin.get("telegram_chat_id"):
        return
    try:
        await bot.send_message(
            chat_id=admin["telegram_chat_id"],
            text=(f"⚠️ Lesson generation/delivery has failed {count} times in a row for "
                  f"{user.get('display_name')} — last error: {exc}. Check Railway logs."),
        )
        db.mark_alert_sent(uid)
    except TelegramError as e:
        logger.error("Failed to send admin failure alert: %s", e)


# --------------------------------------------------------------------------- #
# The five call sites
# --------------------------------------------------------------------------- #

async def run_all(bot):
    """Batch trigger (crons, hourly safety net, startup). Per-user fault isolation:
    one user's failure never aborts the loop."""
    async with _run_lock:
        now = sgt_now()
        for user in db.list_active_candidates():
            try:
                await _check_user(bot, user, now)
            except (ClaudeError, TelegramError, Exception) as exc:  # noqa: BLE001
                await _handle_user_failure(bot, user, exc)
        await _pre_generate_leading(bot)


async def recheck_user(bot, user: dict):
    """On-quiz-completion recheck for a single user, under the same lock. A no-op if the
    user is now caught up; delivers the next session if they were genuinely behind."""
    async with _run_lock:
        now = sgt_now()
        try:
            await _check_user(bot, user, now)
        except (ClaudeError, TelegramError, Exception) as exc:  # noqa: BLE001
            await _handle_user_failure(bot, user, exc)
        await _pre_generate_leading(bot)


async def force_lesson(bot, user: dict) -> str:
    """/forcelesson (§13): deliver the next lesson now, ignoring the pace/slot timing gate
    but STILL respecting the pause gate. Consumes one slot credit so the real scheduler
    doesn't double-deliver. Returns a short status string for the caller to relay."""
    async with _run_lock:
        now = sgt_now()
        fresh = db.get_user_by_id(user["id"])
        if fresh["is_paused"] or db.get_active_quiz_run(fresh["id"]):
            return "paused"
        prog = db.get_progress(fresh["id"])
        pace_marker = fields_to_marker(prog.get("pace_date"), prog.get("pace_slot"))
        if pace_marker is None:
            pace_marker = initial_pace_marker(now)
        current_marker = slot_marker(now)

        next_seq = prog["current_sequence_number"] + 1
        chat_id = fresh["telegram_chat_id"]
        lesson = await ensure_sequence_generated(next_seq, bot=bot, chat_id=chat_id)

        rendered = lesson["rendered_text"]
        nudge = _overdue_nudge_line(fresh["id"])
        if nudge:
            rendered = f"{rendered}\n\n{nudge}"
        await send_with_retry(bot, chat_id, rendered)

        mcqs = db.get_mcqs_for_seq(next_seq)
        quiz_type = "daily_" + slot_of(now)
        await quiz_engine.start_quiz(bot, fresh, quiz_type, next_seq,
                                     quiz_engine.questions_for_lesson(mcqs))

        # Consume exactly one credit (may push the marker ahead of the clock, which just
        # means the next scheduled boundary correctly no-ops for this user).
        new_pace = pace_marker + 1
        pd, ps = marker_to_fields(new_pace)
        last_slot = "catchup" if new_pace < current_marker else ps
        db.record_delivery(fresh["id"], next_seq,
                           to_iso(now.date()) + "T" + now.strftime("%H:%M:%S"), last_slot, pd, ps)
        await _pre_generate_leading(bot)
        return "delivered"


# --------------------------------------------------------------------------- #
# APScheduler wiring
# --------------------------------------------------------------------------- #

def build_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_all, CronTrigger(hour=AM_TRIGGER_HOUR, minute=AM_TRIGGER_MINUTE, timezone=TIMEZONE),
        args=[bot], id="daily_am", misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_all, CronTrigger(hour=PM_TRIGGER_HOUR, minute=PM_TRIGGER_MINUTE, timezone=TIMEZONE),
        args=[bot], id="daily_pm", misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_all, CronTrigger(minute=SAFETY_NET_MINUTE, timezone=TIMEZONE),
        args=[bot], id="hourly_safety_net", misfire_grace_time=600,
    )
    return scheduler
