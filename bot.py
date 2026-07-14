"""
Main entrypoint (§7, §8, §13, §14). Wires python-telegram-bot to the scheduler and
quiz engine. `concurrent_updates=True` so one user's slow handler doesn't head-of-line
block the other candidates; a small per-user lock still serializes a single user's
rapid-fire answers so the same question can't be double-graded.

Run with: python bot.py
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
)

import db
import pdf_export
import quiz_engine
import scheduler as sched
import syllabus_data
from config import ADMIN_TELEGRAM_USERNAME, TELEGRAM_BOT_TOKEN
from timeutil import sgt_today, to_iso, sgt_now
from datetime import timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# httpx logs the full request URL (incl. the bot token) at INFO — keep it at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)

_user_locks: dict[int, asyncio.Lock] = {}


def _user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


# --------------------------------------------------------------------------- #
# Auth helpers (§7)
# --------------------------------------------------------------------------- #

def _auth_user(update: Update):
    if not update.effective_chat:
        return None
    return db.get_authorized_user(update.effective_chat.id)


async def _require_user(update: Update):
    user = _auth_user(update)
    if not user:
        await update.effective_message.reply_text("Not authorized. Contact Dr Tan to be added.")
        return None
    return user


async def _require_admin(update: Update):
    user = await _require_user(update)
    if not user:
        return None
    if user["role"] != "admin":
        await update.effective_message.reply_text("Admin only.")
        return None
    return user


def _resolve_target(caller: dict, args) -> dict | None:
    """Self by default; admins may target another user by display name."""
    if args and caller["role"] == "admin":
        return db.get_user_by_display_name(" ".join(args))
    return caller


HELP_TEXT = (
    "M.Med Anaesthesiology Part A exam-prep bot.\n\n"
    "Two lessons a day (09:30 & 14:30 SGT), each with a 5-question MCQ quiz, paced to you.\n\n"
    "Commands:\n"
    "/status — where you stand\n"
    "/recap — retest questions you've gotten wrong (when due)\n"
    "/skipquiz — end the current quiz early\n"
    "/cancelquiz — discard the current quiz\n"
    "/myexport — PDF of your history + retest ladder\n"
    "/mcqcount — bank size\n"
    "/whoami — your linked record"
)


# --------------------------------------------------------------------------- #
# Onboarding (§7)
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    username = (update.effective_user.username or "") if update.effective_user else ""

    existing = db.get_authorized_user(chat.id)
    if existing:
        await update.message.reply_text(f"Welcome back, {existing['display_name']}.\n\n{HELP_TEXT}")
        return

    linkable = db.find_linkable_by_username(username) if username else None
    if linkable:
        db.link_user(linkable["id"], chat.id)
        # Seed the pace marker so the first lesson is owed immediately (§8).
        from timeutil import initial_pace_marker, marker_to_fields
        pd, ps = marker_to_fields(initial_pace_marker(sgt_now()))
        db.set_pace_marker(linkable["id"], pd, ps)
        await update.message.reply_text(
            f"You're linked, {linkable['display_name']}. Your first lesson will arrive shortly.\n\n{HELP_TEXT}"
        )
        # Deliver promptly rather than waiting for the next cron tick.
        user = db.get_user_by_id(linkable["id"])
        if user["role"] == "candidate":
            asyncio.create_task(sched.recheck_user(context.bot, user))
        return

    await update.message.reply_text("Not authorized. Contact Dr Tan to be added.")


# --------------------------------------------------------------------------- #
# Self-service commands
# --------------------------------------------------------------------------- #

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _require_user(update):
        await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    await update.message.reply_text(_status_text(user))


def _status_text(user: dict) -> str:
    prog = db.get_progress(user["id"])
    run = db.get_active_quiz_run(user["id"])
    pending = db.pending_retest_count(user["id"])
    lines = [
        f"{user['display_name']} ({user['role']})",
        f"Lessons delivered: {prog['current_sequence_number']}",
        f"Last delivered: {prog['last_delivered_at'] or '—'} ({prog['last_slot'] or '—'})",
        f"Paused: {'yes' if user['is_paused'] else 'no'}",
        f"Retest pool: {pending} pending",
    ]
    if run:
        lines.append(f"Quiz in progress: {run['quiz_type']}, "
                     f"question {run['current_index'] + 1}/{run['total_questions']}")
    else:
        lines.append("No quiz in progress.")
    return "\n".join(lines)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    await update.message.reply_text(
        f"chat_id: {user['telegram_chat_id']}\nrole: {user['role']}\n"
        f"whitelist_status: {user['whitelist_status']}\nusername: @{user['telegram_username'] or '—'}"
    )


async def cmd_mcqcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_user(update):
        return
    c = db.mcq_counts()
    by_area = ", ".join(f"{k}: {v}" for k, v in sorted(c["by_area"].items())) or "—"
    by_source = ", ".join(f"{k}: {v}" for k, v in sorted(c["by_source"].items())) or "—"
    await update.message.reply_text(
        f"MCQ bank: {c['total']} total\nBy topic: {by_area}\nBy source: {by_source}\n"
        f"Answered attempts (all users): {c['attempts']}"
    )


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = await _require_user(update)
    if not caller:
        return
    target = _resolve_target(caller, context.args)
    if not target:
        await update.message.reply_text("No such user.")
        return
    if not target.get("telegram_chat_id"):
        await update.message.reply_text(f"{target['display_name']} hasn't linked their chat yet.")
        return
    if db.get_active_quiz_run(target["id"]):
        await update.message.reply_text("A quiz is already in progress — finish it or /cancelquiz first.")
        return
    today = to_iso(sgt_today())
    due = db.due_retest_items(target["id"], today)
    if not due:
        pending = db.pending_retest_count(target["id"])
        nxt = db.next_due_date(target["id"])
        if pending:
            await update.message.reply_text(
                f"Nothing due for retest right now. You have {pending} item(s) in your pool — "
                f"next one is due {nxt}.")
        else:
            await update.message.reply_text("Nothing due for retest — your pool is empty.")
        return
    questions = quiz_engine.questions_for_recap(due)
    await quiz_engine.start_quiz(context.bot, target, "recap", None, questions)
    if target["id"] != caller["id"]:
        await update.message.reply_text(f"Started a {len(due)}-question recap for {target['display_name']}.")


async def cmd_skipquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    async with _user_lock(user["id"]):
        ok = await quiz_engine.skip_quiz(context.bot, user)
    if not ok:
        await update.message.reply_text("No quiz is currently in progress.")
        return
    fresh = db.get_user_by_id(user["id"])
    if fresh["role"] == "candidate":
        asyncio.create_task(sched.recheck_user(context.bot, fresh))


async def cmd_cancelquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    async with _user_lock(user["id"]):
        ok = await quiz_engine.cancel_quiz(context.bot, user)
    if not ok:
        await update.message.reply_text("No quiz is currently in progress.")


async def cmd_myexport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    from telegram.constants import ChatAction
    from typing_util import typing_indicator
    async with typing_indicator(context.bot, user["telegram_chat_id"], ChatAction.UPLOAD_DOCUMENT):
        path = await asyncio.to_thread(pdf_export.build_user_export_pdf, user)
    with open(path, "rb") as fh:
        await context.bot.send_document(chat_id=user["telegram_chat_id"], document=fh,
                                        filename="my_revision_report.pdf")


# --------------------------------------------------------------------------- #
# Admin commands (§7, §13)
# --------------------------------------------------------------------------- #

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /adduser <telegram_username> <display name>")
        return
    username = context.args[0].lstrip("@")
    display_name = " ".join(context.args[1:])
    db.create_user(telegram_username=username, display_name=display_name,
                   role="candidate", whitelist_status="pending", added_by=admin["id"])
    await update.message.reply_text(
        f"Added {display_name} (@{username}) as pending. They send /start to link.")


async def cmd_linkuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /linkuser <display name> <telegram_chat_id>")
        return
    try:
        chat_id = int(context.args[-1])
    except ValueError:
        await update.message.reply_text("The last argument must be a numeric chat id (from @userinfobot).")
        return
    display_name = " ".join(context.args[:-1])
    target = db.get_user_by_display_name(display_name)
    if not target:
        await update.message.reply_text(f"No pending user named '{display_name}'. Add them with /adduser first.")
        return
    db.link_user(target["id"], chat_id)
    from timeutil import initial_pace_marker, marker_to_fields
    pd, ps = marker_to_fields(initial_pace_marker(sgt_now()))
    db.set_pace_marker(target["id"], pd, ps)
    await update.message.reply_text(f"Linked {display_name} to chat id {chat_id} (now active).")


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    lines = []
    for u in db.list_users():
        prog = db.get_progress(u["id"])
        seq = prog["current_sequence_number"] if prog else 0
        lines.append(f"#{u['id']} {u['display_name']} ({u['role']}, {u['whitelist_status']}) "
                     f"seq={seq} paused={'y' if u['is_paused'] else 'n'}")
    await update.message.reply_text("\n".join(lines) or "No users.")


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    target = db.get_user_by_display_name(" ".join(context.args)) if context.args else None
    if not target:
        await update.message.reply_text("Usage: /progress <display name>")
        return
    await update.message.reply_text(_status_text(target))


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    target = db.get_user_by_display_name(" ".join(context.args)) if context.args else None
    if not target:
        await update.message.reply_text("Usage: /removeuser <display name>")
        return
    db.revoke_user(target["id"])
    await update.message.reply_text(f"Revoked {target['display_name']}.")


async def cmd_resetprogress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    target = db.get_user_by_display_name(" ".join(context.args)) if context.args else None
    if not target:
        await update.message.reply_text("Usage: /resetprogress <display name>")
        return
    db.reset_progress(target["id"])
    await update.message.reply_text(
        f"Reset {target['display_name']}: sequence 0, retest/quiz history cleared, pause cleared.")


async def cmd_forcelesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = await _require_user(update)
    if not caller:
        return
    target = _resolve_target(caller, context.args)
    if not target or not target.get("telegram_chat_id"):
        await update.message.reply_text("No such linked user.")
        return
    result = await sched.force_lesson(context.bot, target)
    if result == "paused":
        await context.bot.send_message(
            chat_id=target["telegram_chat_id"],
            text="You have unanswered questions from your last lesson. Reply to continue, or /skipquiz.")
        if target["id"] != caller["id"]:
            await update.message.reply_text(f"{target['display_name']} is paused; sent the reminder.")


async def cmd_pausetest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = await _require_user(update)
    if not caller:
        return
    target = _resolve_target(caller, context.args)
    if not target or not target.get("telegram_chat_id"):
        await update.message.reply_text("No such linked user.")
        return
    db.set_paused(target["id"], True)
    await context.bot.send_message(
        chat_id=target["telegram_chat_id"],
        text="You have unanswered questions from your last lesson. Reply to continue before the next lesson.")


async def cmd_forceretest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await _require_user(update)
    if not user:
        return
    days = 0
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /forceretest [days_overdue]")
            return
    target_date = to_iso(sgt_today() - timedelta(days=days))
    db.force_retest_due(user["id"], target_date)
    await update.message.reply_text(
        f"Set all your pending retest items to due {target_date}. Run /recap to surface them.")


async def cmd_exportmcqs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await _require_admin(update)
    if not admin:
        return
    from telegram.constants import ChatAction
    from typing_util import typing_indicator
    async with typing_indicator(context.bot, admin["telegram_chat_id"], ChatAction.UPLOAD_DOCUMENT):
        path = await asyncio.to_thread(pdf_export.build_mcq_bank_pdf)
    with open(path, "rb") as fh:
        await context.bot.send_document(chat_id=admin["telegram_chat_id"], document=fh,
                                        filename="mcq_bank.pdf")


# --------------------------------------------------------------------------- #
# Quiz answer callbacks (inline keyboard A-E)
# --------------------------------------------------------------------------- #

async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = _auth_user(update)
    if not user:
        await query.answer("Not authorized.", show_alert=False)
        return
    try:
        _, run_id_s, q_index_s, option = query.data.split(":")
        run_id, q_index = int(run_id_s), int(q_index_s)
    except (ValueError, AttributeError):
        await query.answer()
        return

    async with _user_lock(user["id"]):
        run = db.get_active_quiz_run(user["id"])
        # Stale / duplicate tap, or a button from before a restart: ignore gracefully.
        if not run or run["id"] != run_id or run["current_index"] != q_index:
            await query.answer("Already answered.", show_alert=False)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
            return
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        status = await quiz_engine.process_answer(context.bot, user, run, option)

    if status == "completed":
        fresh = db.get_user_by_id(user["id"])
        if fresh["role"] == "candidate":
            asyncio.create_task(sched.recheck_user(context.bot, fresh))


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

async def _post_init(application: Application):
    db.init_db()
    seeded = db.seed_syllabus_topics(syllabus_data.iter_seed_rows())
    if seeded:
        logger.info("Seeded syllabus_topics with %s rows.", seeded)
    if not db.get_admin():
        db.create_user(telegram_username=ADMIN_TELEGRAM_USERNAME, display_name="Admin",
                       role="admin", whitelist_status="active")
        logger.info("Bootstrapped admin row for @%s (send /start to link).", ADMIN_TELEGRAM_USERNAME)

    scheduler = sched.build_scheduler(application.bot)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    # Startup catch-up (§8) — recover any cron tick a restart may have eaten.
    asyncio.create_task(sched.run_all(application.bot))


def main():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("mcqcount", cmd_mcqcount))
    application.add_handler(CommandHandler("recap", cmd_recap))
    application.add_handler(CommandHandler("skipquiz", cmd_skipquiz))
    application.add_handler(CommandHandler("cancelquiz", cmd_cancelquiz))
    application.add_handler(CommandHandler("myexport", cmd_myexport))
    application.add_handler(CommandHandler("forceretest", cmd_forceretest))
    application.add_handler(CommandHandler("forcelesson", cmd_forcelesson))
    application.add_handler(CommandHandler("pausetest", cmd_pausetest))
    # Admin
    application.add_handler(CommandHandler("adduser", cmd_adduser))
    application.add_handler(CommandHandler("linkuser", cmd_linkuser))
    application.add_handler(CommandHandler("listusers", cmd_listusers))
    application.add_handler(CommandHandler("progress", cmd_progress))
    application.add_handler(CommandHandler("removeuser", cmd_removeuser))
    application.add_handler(CommandHandler("resetprogress", cmd_resetprogress))
    application.add_handler(CommandHandler("exportmcqs", cmd_exportmcqs))
    # Quiz answers
    application.add_handler(CallbackQueryHandler(on_answer, pattern=r"^ans:"))

    logger.info("Bot starting…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
