"""
SQLite persistence layer (§4). One short-lived connection per call via a context
manager, ported from the reference bot — but hardened for concurrency (§3):
WAL journal mode + a busy timeout, because up to 5 candidates plus the scheduler
write concurrently here, unlike the single-user reference bot.

All schema lives in SCHEMA; init_db() applies it idempotently (CREATE ... IF NOT
EXISTS). No migration framework — this is a fresh build, not an upgrade.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER UNIQUE,
    telegram_username TEXT,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'candidate',
    whitelist_status TEXT NOT NULL DEFAULT 'pending',
    is_paused INTEGER NOT NULL DEFAULT 0,
    paused_since TEXT,
    added_by INTEGER,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS syllabus_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_exam TEXT NOT NULL,
    subject TEXT NOT NULL,
    topic_title TEXT NOT NULL,
    is_core INTEGER NOT NULL DEFAULT 1,
    weight_pct REAL,
    topic_area TEXT NOT NULL,
    times_covered INTEGER NOT NULL DEFAULT 0,
    last_covered_seq INTEGER
);

CREATE TABLE IF NOT EXISTS lesson_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_number INTEGER NOT NULL UNIQUE,
    syllabus_topic_id INTEGER REFERENCES syllabus_topics(id),
    topic_area TEXT NOT NULL,
    content_json TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    reference_citation TEXT,
    ambiguity_flag INTEGER NOT NULL DEFAULT 0,
    ambiguity_note TEXT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcq_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_sequence_number INTEGER NOT NULL REFERENCES lesson_queue(sequence_number),
    question_text TEXT NOT NULL,
    option_a TEXT NOT NULL, option_b TEXT NOT NULL, option_c TEXT NOT NULL,
    option_d TEXT NOT NULL, option_e TEXT NOT NULL,
    correct_option TEXT NOT NULL,
    explanation TEXT NOT NULL,
    topic_area TEXT NOT NULL,
    syllabus_topic_id INTEGER REFERENCES syllabus_topics(id),
    reference_citation TEXT,
    source TEXT NOT NULL DEFAULT 'ai_generated'
);

CREATE TABLE IF NOT EXISTS user_progress (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    current_sequence_number INTEGER NOT NULL DEFAULT 0,
    last_delivered_at TEXT,
    last_slot TEXT,
    pace_date TEXT,
    pace_slot TEXT,
    last_reminder_marker TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_alert_at TEXT
);

CREATE TABLE IF NOT EXISTS quiz_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    quiz_type TEXT NOT NULL,
    lesson_sequence_number INTEGER,
    status TEXT NOT NULL DEFAULT 'in_progress',
    questions_json TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    total_questions INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS retest_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    mcq_id INTEGER NOT NULL REFERENCES mcq_bank(id),
    date_of_error TEXT NOT NULL,
    interval_index INTEGER NOT NULL DEFAULT 0,
    streak_at_current_interval INTEGER NOT NULL DEFAULT 0,
    next_eligible_date TEXT NOT NULL,
    times_retested INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    date_resolved TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_retest_pool_pending_unique
    ON retest_pool(user_id, mcq_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_retest_pool_user_status
    ON retest_pool(user_id, status);

CREATE INDEX IF NOT EXISTS idx_quiz_runs_user_status
    ON quiz_runs(user_id, status);

CREATE INDEX IF NOT EXISTS idx_mcq_bank_seq
    ON mcq_bank(lesson_sequence_number);

CREATE TABLE IF NOT EXISTS quiz_answer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_run_id INTEGER NOT NULL REFERENCES quiz_runs(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    mcq_id INTEGER NOT NULL REFERENCES mcq_bank(id),
    question_index INTEGER NOT NULL,
    selected_option TEXT NOT NULL,
    is_correct INTEGER NOT NULL,
    answered_at TEXT NOT NULL,
    retest_pool_id INTEGER
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DATABASE_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Concurrency hardening (§3): WAL lets readers and a writer coexist; busy_timeout
    # makes a write wait briefly on a lock instead of instantly raising "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# bot_state
# --------------------------------------------------------------------------- #

def get_state(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #

def create_user(telegram_username, display_name, role="candidate",
                whitelist_status="pending", telegram_chat_id=None, added_by=None):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO users (telegram_chat_id, telegram_username, display_name,
                                  role, whitelist_status, added_by, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (telegram_chat_id, telegram_username, display_name, role,
             whitelist_status, added_by, _now()),
        )
        user_id = cur.lastrowid
        conn.execute("INSERT INTO user_progress (user_id) VALUES (?)", (user_id,))
        return user_id


def get_user_by_chat_id(chat_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_display_name(name: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(display_name) = LOWER(?)", (name,)
        ).fetchone()
        return dict(row) if row else None


def find_linkable_by_username(username: str):
    """A row awaiting a chat-id link for this username (case-insensitive) — covers both
    a pending candidate and the bootstrapped-but-not-yet-linked admin (§7)."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM users
               WHERE LOWER(telegram_username) = LOWER(?)
                 AND telegram_chat_id IS NULL
                 AND whitelist_status IN ('pending', 'active')""",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def link_user(user_id: int, chat_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET telegram_chat_id = ?, whitelist_status = 'active' WHERE id = ?",
            (chat_id, user_id),
        )


def get_authorized_user(chat_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_chat_id = ? AND whitelist_status = 'active'",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def list_users():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def list_active_candidates():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE whitelist_status = 'active' AND role = 'candidate' "
            "ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_admin():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def set_paused(user_id: int, paused: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET is_paused = ?, paused_since = ? WHERE id = ?",
            (1 if paused else 0, _now() if paused else None, user_id),
        )


def revoke_user(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET whitelist_status = 'revoked' WHERE id = ?", (user_id,))


# --------------------------------------------------------------------------- #
# user_progress
# --------------------------------------------------------------------------- #

def get_progress(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_progress WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def set_pace_marker(user_id: int, pace_date: str, pace_slot: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_progress SET pace_date = ?, pace_slot = ? WHERE user_id = ?",
            (pace_date, pace_slot, user_id),
        )


def record_delivery(user_id: int, sequence_number: int, last_delivered_at: str,
                    last_slot: str, pace_date: str, pace_slot: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE user_progress
               SET current_sequence_number = ?, last_delivered_at = ?, last_slot = ?,
                   pace_date = ?, pace_slot = ?, consecutive_failures = 0
               WHERE user_id = ?""",
            (sequence_number, last_delivered_at, last_slot, pace_date, pace_slot, user_id),
        )


def set_last_reminder_marker(user_id: int, marker: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_progress SET last_reminder_marker = ? WHERE user_id = ?",
            (marker, user_id),
        )


def reset_progress(user_id: int):
    """Full reset for /resetprogress (§13): sequence to 0, clear pace/reminder markers,
    wipe this user's retest_pool + quiz history, cancel any active run, clear pause."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE user_progress
               SET current_sequence_number = 0, last_delivered_at = NULL, last_slot = NULL,
                   pace_date = NULL, pace_slot = NULL, last_reminder_marker = NULL,
                   consecutive_failures = 0, last_failure_alert_at = NULL
               WHERE user_id = ?""",
            (user_id,),
        )
        conn.execute("DELETE FROM retest_pool WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE quiz_runs SET status = 'cancelled', completed_at = ? "
            "WHERE user_id = ? AND status = 'in_progress'",
            (_now(), user_id),
        )
        conn.execute("UPDATE users SET is_paused = 0, paused_since = NULL WHERE id = ?", (user_id,))


def increment_failures(user_id: int) -> int:
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_progress SET consecutive_failures = consecutive_failures + 1 "
            "WHERE user_id = ?",
            (user_id,),
        )
        row = conn.execute(
            "SELECT consecutive_failures FROM user_progress WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["consecutive_failures"] if row else 0


def mark_alert_sent(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_progress SET last_failure_alert_at = ? WHERE user_id = ?",
            (_now(), user_id),
        )


# --------------------------------------------------------------------------- #
# syllabus_topics
# --------------------------------------------------------------------------- #

def seed_syllabus_topics(rows):
    """Idempotent seed: only inserts if the table is empty."""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM syllabus_topics").fetchone()["c"]
        if n > 0:
            return 0
        conn.executemany(
            """INSERT INTO syllabus_topics
               (source_exam, subject, topic_title, is_core, weight_pct, topic_area)
               VALUES (?, ?, ?, ?, ?, ?)""",
            list(rows),
        )
        return conn.total_changes


def get_all_topics():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM syllabus_topics ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def mark_topic_covered(topic_id: int, sequence_number: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE syllabus_topics SET times_covered = times_covered + 1, last_covered_seq = ? "
            "WHERE id = ?",
            (sequence_number, topic_id),
        )


def subject_counts_in_queue():
    """{subject: number of lesson_queue rows tagged to that subject} for the weighting rule."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT st.subject AS subject, COUNT(*) AS c
               FROM lesson_queue lq JOIN syllabus_topics st ON lq.syllabus_topic_id = st.id
               GROUP BY st.subject"""
        ).fetchall()
        return {r["subject"]: r["c"] for r in rows}


# --------------------------------------------------------------------------- #
# lesson_queue + mcq_bank (persist-before-send point, §8)
# --------------------------------------------------------------------------- #

def get_lesson_by_seq(seq: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lesson_queue WHERE sequence_number = ?", (seq,)
        ).fetchone()
        return dict(row) if row else None


def max_sequence_number() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(sequence_number) AS m FROM lesson_queue").fetchone()
        return row["m"] or 0


def insert_lesson_and_mcqs(seq, syllabus_topic_id, topic_area, content_json, rendered_text,
                           reference_citation, ambiguity_flag, ambiguity_note, mcqs, source):
    """The durability point (§8): lesson_queue row + its 5 mcq_bank rows written in ONE
    transaction, BEFORE any Telegram delivery. Returns the list of inserted mcq ids."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO lesson_queue
               (sequence_number, syllabus_topic_id, topic_area, content_json, rendered_text,
                reference_citation, ambiguity_flag, ambiguity_note, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seq, syllabus_topic_id, topic_area, content_json, rendered_text,
             reference_citation, int(ambiguity_flag), ambiguity_note, _now()),
        )
        mcq_ids = []
        for m in mcqs:
            cur = conn.execute(
                """INSERT INTO mcq_bank
                   (lesson_sequence_number, question_text, option_a, option_b, option_c,
                    option_d, option_e, correct_option, explanation, topic_area,
                    syllabus_topic_id, reference_citation, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (seq, m["question"], m["option_a"], m["option_b"], m["option_c"],
                 m["option_d"], m["option_e"], m["correct_option"].upper(), m["explanation"],
                 topic_area, syllabus_topic_id, m.get("reference_citation") or None, source),
            )
            mcq_ids.append(cur.lastrowid)
        return mcq_ids


def get_mcqs_for_seq(seq: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcq_bank WHERE lesson_sequence_number = ? ORDER BY id ASC", (seq,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_mcq(mcq_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM mcq_bank WHERE id = ?", (mcq_id,)).fetchone()
        return dict(row) if row else None


def mcq_counts():
    """For /mcqcount: totals by topic_area and by source, plus answered-attempts total."""
    with get_conn() as conn:
        by_area = {
            r["topic_area"]: r["c"]
            for r in conn.execute(
                "SELECT topic_area, COUNT(*) AS c FROM mcq_bank GROUP BY topic_area"
            ).fetchall()
        }
        by_source = {
            r["source"]: r["c"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS c FROM mcq_bank GROUP BY source"
            ).fetchall()
        }
        total = conn.execute("SELECT COUNT(*) AS c FROM mcq_bank").fetchone()["c"]
        attempts = conn.execute("SELECT COUNT(*) AS c FROM quiz_answer_log").fetchone()["c"]
        return {"total": total, "by_area": by_area, "by_source": by_source, "attempts": attempts}


def all_mcqs_with_lesson():
    """For /exportmcqs: every mcq joined to its lesson's ambiguity fields, grouped later."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT m.*, lq.ambiguity_flag AS lesson_ambiguity_flag,
                      lq.ambiguity_note AS lesson_ambiguity_note
               FROM mcq_bank m
               JOIN lesson_queue lq ON m.lesson_sequence_number = lq.sequence_number
               ORDER BY m.topic_area ASC, m.lesson_sequence_number ASC, m.id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# quiz_runs
# --------------------------------------------------------------------------- #

def create_quiz_run(user_id, quiz_type, lesson_sequence_number, questions: list):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO quiz_runs
               (user_id, quiz_type, lesson_sequence_number, status, questions_json,
                current_index, score, total_questions, started_at)
               VALUES (?, ?, ?, 'in_progress', ?, 0, 0, ?, ?)""",
            (user_id, quiz_type, lesson_sequence_number, json.dumps(questions),
             len(questions), _now()),
        )
        return cur.lastrowid


def get_active_quiz_run(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM quiz_runs WHERE user_id = ? AND status = 'in_progress' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["questions"] = json.loads(d["questions_json"])
        return d


def update_quiz_progress(run_id: int, current_index: int, score: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE quiz_runs SET current_index = ?, score = ? WHERE id = ?",
            (current_index, score, run_id),
        )


def complete_quiz_run(run_id: int, score: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE quiz_runs SET status = 'completed', score = ?, completed_at = ? WHERE id = ?",
            (score, _now(), run_id),
        )


def cancel_quiz_run(run_id: int, status: str = "cancelled"):
    with get_conn() as conn:
        conn.execute(
            "UPDATE quiz_runs SET status = ?, completed_at = ? WHERE id = ?",
            (status, _now(), run_id),
        )


# --------------------------------------------------------------------------- #
# retest_pool (Leitner ladder, §5) — all mutations are single atomic UPDATEs
# --------------------------------------------------------------------------- #

def upsert_retest_wrong(user_id, mcq_id, date_of_error, next_eligible_date):
    """Record a wrong answer (§5). Upsert: if a pending row exists for (user_id, mcq_id),
    reset it in place; otherwise insert. Backed by idx_retest_pool_pending_unique."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM retest_pool WHERE user_id = ? AND mcq_id = ? AND status = 'pending'",
            (user_id, mcq_id),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE retest_pool
                   SET interval_index = 0, streak_at_current_interval = 0,
                       next_eligible_date = ?, times_retested = times_retested + 1,
                       date_of_error = ?
                   WHERE id = ?""",
                (next_eligible_date, date_of_error, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            """INSERT INTO retest_pool
               (user_id, mcq_id, date_of_error, interval_index, streak_at_current_interval,
                next_eligible_date, times_retested, status)
               VALUES (?, ?, ?, 0, 0, ?, 1, 'pending')""",
            (user_id, mcq_id, date_of_error, next_eligible_date),
        )
        return cur.lastrowid


def advance_retest_correct(retest_id: int, intervals, final_streak_to_resolve, today_iso,
                           date_add):
    """Advance a pending item up the ladder on a correct retest answer (§5). Single atomic
    read+update inside one connection. `date_add(days)->iso` supplied by caller (timeutil).
    Returns the new status ('pending'|'understood')."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM retest_pool WHERE id = ?", (retest_id,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        idx = row["interval_index"]
        streak = row["streak_at_current_interval"]
        times = row["times_retested"] + 1
        last = len(intervals) - 1
        if idx < last:
            idx += 1
            next_date = date_add(intervals[idx])
            conn.execute(
                """UPDATE retest_pool
                   SET interval_index = ?, streak_at_current_interval = 0,
                       next_eligible_date = ?, times_retested = ?
                   WHERE id = ?""",
                (idx, next_date, times, retest_id),
            )
            return "pending"
        # at the final (longest) interval
        streak += 1
        if streak >= final_streak_to_resolve:
            conn.execute(
                """UPDATE retest_pool
                   SET streak_at_current_interval = ?, times_retested = ?,
                       status = 'understood', date_resolved = ?
                   WHERE id = ?""",
                (streak, times, _now(), retest_id),
            )
            return "understood"
        next_date = date_add(intervals[last])
        conn.execute(
            """UPDATE retest_pool
               SET streak_at_current_interval = ?, next_eligible_date = ?, times_retested = ?
               WHERE id = ?""",
            (streak, next_date, times, retest_id),
        )
        return "pending"


def due_retest_items(user_id: int, today_iso: str):
    """Pending items eligible now (next_eligible_date <= today), oldest-due first (§9)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT rp.*, m.question_text, m.option_a, m.option_b, m.option_c, m.option_d,
                      m.option_e, m.correct_option, m.explanation, m.topic_area
               FROM retest_pool rp JOIN mcq_bank m ON rp.mcq_id = m.id
               WHERE rp.user_id = ? AND rp.status = 'pending' AND rp.next_eligible_date <= ?
               ORDER BY rp.next_eligible_date ASC, rp.id ASC""",
            (user_id, today_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def pending_retest_count(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM retest_pool WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        return row["c"]


def next_due_date(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(next_eligible_date) AS d FROM retest_pool "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        return row["d"] if row else None


def overdue_retest_count(user_id: int, cutoff_iso: str):
    """Pending items whose next_eligible_date <= cutoff (today - threshold): overdue (§9)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM retest_pool "
            "WHERE user_id = ? AND status = 'pending' AND next_eligible_date <= ?",
            (user_id, cutoff_iso),
        ).fetchone()
        return row["c"]


def force_retest_due(user_id: int, target_date_iso: str):
    """Test helper (/forceretest): set every pending row's next_eligible_date."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE retest_pool SET next_eligible_date = ? WHERE user_id = ? AND status = 'pending'",
            (target_date_iso, user_id),
        )


def retest_ladder_for_user(user_id: int):
    """For /myexport: current ladder position of each pending item."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT rp.*, m.question_text FROM retest_pool rp
               JOIN mcq_bank m ON rp.mcq_id = m.id
               WHERE rp.user_id = ? AND rp.status = 'pending'
               ORDER BY rp.next_eligible_date ASC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# quiz_answer_log
# --------------------------------------------------------------------------- #

def log_answer(quiz_run_id, user_id, mcq_id, question_index, selected_option,
               is_correct, retest_pool_id=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO quiz_answer_log
               (quiz_run_id, user_id, mcq_id, question_index, selected_option,
                is_correct, answered_at, retest_pool_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (quiz_run_id, user_id, mcq_id, question_index, selected_option,
             int(is_correct), _now(), retest_pool_id),
        )


def answer_history_for_user(user_id: int):
    """For /myexport: full answered history with the mcq details."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT qal.*, m.question_text, m.correct_option, m.explanation, m.topic_area
               FROM quiz_answer_log qal JOIN mcq_bank m ON qal.mcq_id = m.id
               WHERE qal.user_id = ? ORDER BY qal.answered_at ASC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
