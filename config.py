"""
Central configuration. Everything secret comes from environment variables —
never hardcode tokens/keys here. Ported from the reference bot's config.py,
extended with multi-user, spaced-repetition, and scheduling constants (§3, §5, §8).
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # no-op on Railway (no .env there); reads local .env when present


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"FATAL: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

# The admin's Telegram @username (no leading @), used for first-boot self-bootstrap (§13).
ADMIN_TELEGRAM_USERNAME = _require("ADMIN_TELEGRAM_USERNAME").lstrip("@")

DATABASE_PATH = os.environ.get("DATABASE_PATH", "mmed_bot.db")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

TIMEZONE = "Asia/Singapore"  # UTC+8, no daylight saving

# --- Scheduling (§8) -------------------------------------------------------------
# One daily delivery, unlocked at this trigger time. This constant anchors BOTH the
# cron trigger and the pace-marker math (current_marker in timeutil.py). Keep the
# tuple precision — a bare hour>= check is the reference bot's midnight-rollover bug.
AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE = 9, 30
SAFETY_NET_MINUTE = 45  # hourly safety-net cron

# --- Spaced repetition (§5) ------------------------------------------------------
RETEST_INTERVALS = [1, 3, 7, 16]          # days, indexed 0..3
FINAL_STAGE_STREAK_TO_RESOLVE = 2         # correct answers at the 16-day interval before retiring
OVERDUE_NUDGE_THRESHOLD_DAYS = 3          # days past next_eligible_date before the nudge fires (§9)

# --- Quiz (§8) -------------------------------------------------------------------
DAILY_QUESTIONS = 5                       # always exactly 5 lesson-native MCQs

# --- Reliability (§3, §7 reference bot) ------------------------------------------
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3                  # multiplied by attempt number (3s, 6s, 9s)

# --- Admin failure alerting (§8) -------------------------------------------------
FAILURE_ALERT_THRESHOLD = 2               # consecutive top-level failures before DMing admin
FAILURE_ALERT_COOLDOWN_HOURS = 24         # min gap between alerts for the same stuck user

# --- Typing indicator (§8, §14) --------------------------------------------------
TYPING_REFRESH_SECONDS = 4                # re-send chat action every N seconds during a long wait
