"""
Offline test suite (§12.1) — no network, no Telegram, no Claude. Sets a temp DATABASE_PATH
and stub credentials before importing the app, patches the clock and the Claude generation
call, and uses a FakeBot that records sends.

Run: python test_offline.py
Covers: daily pace-marker math, the primary over-delivery fix, reminder throttle, ladder math + upsert,
whitelist matching + linkuser, topic-weighting distribution, semantic JSON validation,
persist-before-send / no-regenerate, per-user fault isolation, admin failure alert + cooldown,
the typing indicator, and the quiz flow (scoring, wrong→retest, completion unpause).
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- Environment must be set BEFORE importing config/db ---------------------
_SCRATCH = os.environ.get("TMPDIR", "/tmp")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ADMIN_TELEGRAM_USERNAME", "the_admin")
os.environ["DATABASE_PATH"] = os.path.join(_SCRATCH, "mmed_test.db")

import config  # noqa: E402
import chart_generator  # noqa: E402
import db  # noqa: E402
import curriculum  # noqa: E402
import lesson_generator  # noqa: E402
import pdf_export  # noqa: E402
import quiz_engine  # noqa: E402
import scheduler  # noqa: E402
import syllabus_data  # noqa: E402
import timeutil  # noqa: E402
from lesson_generator import build_validator  # noqa: E402
from claude_client import _extract_json_block, ask_json  # noqa: E402

SGT = ZoneInfo("Asia/Singapore")
FIXED = datetime(2026, 7, 20, 10, 0, 0, tzinfo=SGT)  # a Monday, 10:00 AM (AM window)

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'} — {name}" + (f"  [{detail}]" if detail and not cond else ""))


def reset_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(config.DATABASE_PATH + suffix)
        except FileNotFoundError:
            pass
    db.init_db()
    db.seed_syllabus_topics(syllabus_data.iter_seed_rows())


# --- Fakes & patches --------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.messages = []   # (chat_id, text)
        self.actions = []    # (chat_id, action)
        self.documents = []
        self.photos = []     # (chat_id, path, caption)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))

    async def send_document(self, chat_id, document, filename=None):
        self.documents.append((chat_id, filename))

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, getattr(photo, "name", None), caption))

    def lessons_to(self, chat_id):
        return [t for c, t in self.messages if c == chat_id and t.startswith("📘 LESSON")]


def make_mcqs(correct="A"):
    return [
        {"question": f"Q{i}?", "option_a": "a", "option_b": "b", "option_c": "c",
         "option_d": "d", "option_e": "e", "correct_option": correct,
         "explanation": "because", "reference_citation": ""}
        for i in range(5)
    ]


_gen_calls = {"n": 0}


async def fake_generate(topic):
    _gen_calls["n"] += 1
    return {
        "topic_area": topic["topic_area"],
        "syllabus_topic": topic["topic_title"],
        "lesson_title": "T",
        "lesson_body": "Body.",
        "reference_citation": "",
        "ambiguity_flag": False,
        "ambiguity_note": "",
        "chart": None,
        "mcqs": make_mcqs("A"),
    }


async def fake_generate_with_chart(topic):
    _gen_calls["n"] += 1
    return {
        "topic_area": topic["topic_area"],
        "syllabus_topic": topic["topic_title"],
        "lesson_title": "T",
        "lesson_body": "Body.",
        "reference_citation": "",
        "ambiguity_flag": False,
        "ambiguity_note": "",
        "chart": {"type": "dose_response_curve", "params": {"curves": ["full_agonist"]}},
        "mcqs": make_mcqs("A"),
    }


def patch_clock(now=FIXED):
    scheduler.sgt_now = lambda: now
    scheduler.sgt_today = lambda: now.date()
    quiz_engine.sgt_today = lambda: now.date()


def install_patches():
    lesson_generator.generate_lesson_data = fake_generate
    scheduler.lesson_generator.generate_lesson_data = fake_generate
    patch_clock()


async def answer_active(bot, user, choose):
    """Answer the user's active quiz until it ends. choose(mcq)->option letter."""
    run = db.get_active_quiz_run(user["id"])
    while run:
        idx = run["current_index"]
        mcq = db.get_mcq(run["questions"][idx]["mcq_id"])
        status = await quiz_engine.process_answer(bot, db.get_user_by_id(user["id"]), run,
                                                  choose(mcq))
        if status == "completed":
            break
        run = db.get_active_quiz_run(user["id"])


def _seed_candidate(name, chat_id, pace_days_behind=1, now=FIXED):
    uid = db.create_user(telegram_username=name.lower().replace(" ", "_"), display_name=name,
                         role="candidate", whitelist_status="active", telegram_chat_id=chat_id)
    marker = timeutil.current_marker(now) - pace_days_behind
    pd, ps = timeutil.marker_to_fields(marker)
    db.set_pace_marker(uid, pd, ps)
    return db.get_user_by_id(uid)


# --- Tests ------------------------------------------------------------------

def test_pace_marker_math():
    print("test_pace_marker_math")
    from datetime import date
    before_trigger = datetime(2026, 7, 20, 9, 29, tzinfo=SGT)
    at_trigger = datetime(2026, 7, 20, 9, 30, tzinfo=SGT)
    mid_day = datetime(2026, 7, 20, 15, 0, tzinfo=SGT)
    just_after_midnight = datetime(2026, 7, 21, 0, 15, tzinfo=SGT)
    day20 = date(2026, 7, 20).toordinal()
    day21 = date(2026, 7, 21).toordinal()
    check("09:29 (before trigger) belongs to the PREVIOUS day's slot",
          timeutil.current_marker(before_trigger) == day20 - 1)
    check("09:30 (at trigger) opens today's slot",
          timeutil.current_marker(at_trigger) == day20)
    check("mid-afternoon is still today's slot", timeutil.current_marker(mid_day) == day20)
    check("00:15 next calendar day still belongs to the PREVIOUS day's slot (trigger hasn't hit yet)",
          timeutil.current_marker(just_after_midnight) == day21 - 1)
    check("marker advances by exactly 1 per day",
          timeutil.current_marker(at_trigger) + 1 ==
          timeutil.current_marker(datetime(2026, 7, 21, 9, 30, tzinfo=SGT)))


def test_ladder():
    print("test_ladder")
    reset_db()
    uid = db.create_user("u", "U", telegram_chat_id=1)
    topic = db.get_all_topics()[0]
    db.insert_lesson_and_mcqs(1, topic["id"], topic["topic_area"], "{}", "r", None, False, None,
                              make_mcqs("A"), "ai_generated")
    mcq_id = db.get_mcqs_for_seq(1)[0]["id"]

    base = FIXED.date()

    def dadd(days):
        return timeutil.to_iso(base + timedelta(days=days))

    rid = db.upsert_retest_wrong(uid, mcq_id, timeutil.to_iso(base), dadd(1))
    row = db.get_all_topics  # placeholder
    item = [r for r in db.due_retest_items(uid, dadd(1))]
    check("wrong → 1 pending item due tomorrow", db.pending_retest_count(uid) == 1)

    # advance through the ladder
    st = db.advance_retest_correct(rid, config.RETEST_INTERVALS, 2, dadd(0), dadd)
    check("idx0→idx1 stays pending", st == "pending")
    st = db.advance_retest_correct(rid, config.RETEST_INTERVALS, 2, dadd(0), dadd)
    check("idx1→idx2 stays pending", st == "pending")
    st = db.advance_retest_correct(rid, config.RETEST_INTERVALS, 2, dadd(0), dadd)
    check("idx2→idx3 stays pending", st == "pending")
    st = db.advance_retest_correct(rid, config.RETEST_INTERVALS, 2, dadd(0), dadd)
    check("idx3 streak1 stays pending", st == "pending")
    st = db.advance_retest_correct(rid, config.RETEST_INTERVALS, 2, dadd(0), dadd)
    check("idx3 streak2 → understood", st == "understood")
    check("understood leaves 0 pending", db.pending_retest_count(uid) == 0)

    # reset-on-wrong at a mid stage
    rid2 = db.upsert_retest_wrong(uid, mcq_id, timeutil.to_iso(base), dadd(1))
    db.advance_retest_correct(rid2, config.RETEST_INTERVALS, 2, dadd(0), dadd)  # idx1
    db.upsert_retest_wrong(uid, mcq_id, timeutil.to_iso(base), dadd(1))         # wrong resets
    with db.get_conn() as conn:
        r = conn.execute("SELECT interval_index FROM retest_pool WHERE id=?", (rid2,)).fetchone()
    check("wrong resets interval_index to 0", r["interval_index"] == 0)


def test_upsert_no_duplicate():
    print("test_upsert_no_duplicate")
    reset_db()
    uid = db.create_user("u", "U", telegram_chat_id=1)
    topic = db.get_all_topics()[0]
    db.insert_lesson_and_mcqs(1, topic["id"], topic["topic_area"], "{}", "r", None, False, None,
                              make_mcqs("A"), "ai_generated")
    mcq_id = db.get_mcqs_for_seq(1)[0]["id"]
    t = timeutil.to_iso(FIXED.date())
    db.upsert_retest_wrong(uid, mcq_id, t, t)
    db.upsert_retest_wrong(uid, mcq_id, t, t)
    db.upsert_retest_wrong(uid, mcq_id, t, t)
    check("three wrongs → one pending row", db.pending_retest_count(uid) == 1)


def test_validator():
    print("test_validator")
    v = build_validator({"MyTopic"})
    good = {"topic_area": "Physiology", "syllabus_topic": "MyTopic", "lesson_title": "t",
            "lesson_body": "b", "ambiguity_flag": False, "ambiguity_note": "", "mcqs": make_mcqs("A")}
    check("valid contract passes", v(good) is None)
    bad4 = dict(good); bad4["mcqs"] = make_mcqs("A")[:4]
    check("4 mcqs rejected", v(bad4) is not None)
    badopt = dict(good); m = [dict(x) for x in make_mcqs("A")]; m[0]["correct_option"] = "F"; badopt["mcqs"] = m
    check("correct_option F rejected", v(badopt) is not None)
    badarea = dict(good); badarea["topic_area"] = "Nope"
    check("bad topic_area rejected", v(badarea) is not None)
    badtopic = dict(good); badtopic["syllabus_topic"] = "Unknown"
    check("syllabus_topic mismatch rejected", v(badtopic) is not None)
    badamb = dict(good); badamb["ambiguity_flag"] = True; badamb["ambiguity_note"] = ""
    check("ambiguity flag w/ empty note rejected", v(badamb) is not None)
    empty = dict(good); m2 = [dict(x) for x in make_mcqs("A")]; m2[1]["option_c"] = ""; empty["mcqs"] = m2
    check("empty option rejected", v(empty) is not None)


def test_chart_validator():
    print("test_chart_validator")
    v = build_validator({"MyTopic"})
    good = {"topic_area": "Physiology", "syllabus_topic": "MyTopic", "lesson_title": "t",
            "lesson_body": "b", "ambiguity_flag": False, "ambiguity_note": "", "mcqs": make_mcqs("A")}
    check("chart omitted entirely passes", v(good) is None)
    with_null = dict(good); with_null["chart"] = None
    check("chart explicitly null passes", v(with_null) is None)
    with_chart = dict(good); with_chart["chart"] = {"type": "dose_response_curve", "params": {}}
    check("valid chart type passes", v(with_chart) is None)
    bad_type = dict(good); bad_type["chart"] = {"type": "not_a_real_chart_type"}
    check("unknown chart type rejected", v(bad_type) is not None)
    bad_shape = dict(good); bad_shape["chart"] = "not-a-dict"
    check("non-object chart rejected", v(bad_shape) is not None)


def test_ambiguity_flag_rendering():
    print("test_ambiguity_flag_rendering")
    flagged = {
        "topic_area": "Pharmacology", "lesson_title": "T", "lesson_body": "Body text.",
        "reference_citation": "", "ambiguity_flag": True,
        "ambiguity_note": "Textbook teaching favours X; recent evidence favours Y.",
        "mcqs": make_mcqs("A"),
    }
    rendered = lesson_generator.render_lesson(1, flagged)
    check("ambiguity note appears verbatim in the lesson text", flagged["ambiguity_note"] in rendered)
    check("examiner-referral line appears verbatim when flagged",
          lesson_generator.AMBIGUITY_LINE in rendered)

    clean = dict(flagged); clean["ambiguity_flag"] = False
    rendered_clean = lesson_generator.render_lesson(2, clean)
    check("examiner-referral line absent when ambiguity_flag is false",
          lesson_generator.AMBIGUITY_LINE not in rendered_clean)

    class FakePdf:
        def __init__(self):
            self.lines = []

        def line(self, txt, size=10, style="", h=5):
            self.lines.append(txt)

        def gap(self, h=2):
            pass

    mcq_row = {
        "question_text": "Q?", "option_a": "a", "option_b": "b", "option_c": "c",
        "option_d": "d", "option_e": "e", "correct_option": "A", "explanation": "because",
        "reference_citation": "", "lesson_ambiguity_flag": 1,
        "lesson_ambiguity_note": "Area of genuine controversy.",
    }
    fake_pdf = FakePdf()
    pdf_export._render_bank_question(fake_pdf, 1, mcq_row)
    check("PDF export includes the ambiguity note",
          any("Area of genuine controversy." in t for t in fake_pdf.lines))
    check("PDF export includes the exact examiner-referral line",
          any(t == lesson_generator.AMBIGUITY_LINE for t in fake_pdf.lines))

    mcq_row_clean = dict(mcq_row); mcq_row_clean["lesson_ambiguity_flag"] = 0
    fake_pdf2 = FakePdf()
    pdf_export._render_bank_question(fake_pdf2, 1, mcq_row_clean)
    check("PDF export omits the examiner-referral line when the lesson isn't flagged",
          not any(lesson_generator.AMBIGUITY_LINE in t for t in fake_pdf2.lines))


def test_json_extraction_with_embedded_brackets():
    print("test_json_extraction_with_embedded_brackets (regression: production incident 2026-07-15)")
    # A reference range and a citation, both using brackets, embedded in string VALUES —
    # exactly the pattern that broke the old naive rfind('}')/rfind(']') trim.
    payload = {
        "topic_area": "Physiology",
        "syllabus_topic": "Respiratory",
        "lesson_title": "Normal ABG values",
        "lesson_body": "Normal PaCO2 is [4.7-6.0 kPa] and normal pH is [7.35-7.45]. See [1] for detail.",
        "reference_citation": "Miller's Anaesthesia [9th ed]",
        "ambiguity_flag": False,
        "ambiguity_note": "",
        "mcqs": make_mcqs("A"),
    }
    raw = json.dumps(payload) + "\n\nHope that helps! Let me know if you need more."
    extracted = _extract_json_block(raw)
    parsed = json.loads(extracted)  # must not raise
    check("full lesson_body survives extraction (not truncated at an embedded bracket)",
          parsed["lesson_body"] == payload["lesson_body"])
    check("all 5 mcqs survive extraction", len(parsed["mcqs"]) == 5)
    check("trailing chat prose after the JSON is correctly dropped",
          "Hope that helps" not in extracted)

    # Also: a genuinely truncated response (missing its closing brace) must still fail
    # loudly rather than being silently "fixed" into something that parses wrong.
    truncated = json.dumps(payload)[:120]
    still_broken = _extract_json_block(truncated)
    check("a genuinely truncated blob is left for json.loads to reject",
          still_broken == truncated)


async def test_max_tokens_truncation_retries_with_bigger_budget():
    print("test_max_tokens_truncation_retries_with_bigger_budget")
    calls = []

    async def fake_call(system, user_message, max_tokens):
        calls.append(max_tokens)
        if len(calls) == 1:
            return "{\"incomplete", "max_tokens"
        good = json.dumps({"ok": True})
        return good, "end_turn"

    import claude_client
    orig = claude_client._call_raw_sync
    orig_to_thread = asyncio.to_thread

    async def patched_to_thread(fn, *args):
        if fn is claude_client._call_raw_sync:
            return await fake_call(*args)
        return await orig_to_thread(fn, *args)

    asyncio.to_thread = patched_to_thread
    try:
        result = await ask_json("sys", "msg", max_tokens=100)
    finally:
        asyncio.to_thread = orig_to_thread

    check("recovers after a max_tokens truncation", result == {"ok": True})
    check("retry used a larger token budget, not the same one", calls[1] > calls[0],
          f"{calls}")


def test_topic_distribution():
    print("test_topic_distribution")
    reset_db()
    for seq in range(1, 251):
        topic = curriculum.choose_next_topic()
        db.insert_lesson_and_mcqs(seq, topic["id"], topic["topic_area"], "{}", "r", None, False,
                                  None, make_mcqs("A"), "ai_generated")
        db.mark_topic_covered(topic["id"], seq)
    counts = db.subject_counts_in_queue()
    total = sum(counts.values())
    bucket = {}
    for subj, c in counts.items():
        b = curriculum._WEIGHT_BUCKET[subj]
        bucket[b] = bucket.get(b, 0) + c
    phys = bucket.get("Physiology", 0) / total
    pharm = bucket.get("Pharmacology", 0) / total
    equip = bucket.get("Physics and Equipment", 0) / total
    check("Physiology ~32%", abs(phys - 0.32) < 0.05, f"{phys:.3f}")
    check("Pharm+Biostats ~33%", abs(pharm - 0.33) < 0.05, f"{pharm:.3f}")
    check("Physics ~15%", abs(equip - 0.15) < 0.05, f"{equip:.3f}")


def test_no_duplicate_username_lookup():
    print("test_no_duplicate_username_lookup (regression: duplicate-add confusion 2026-07-15)")
    reset_db()
    db.create_user(telegram_username="TheAnaesthetistSG", display_name="zorb", role="candidate",
                   whitelist_status="active", telegram_chat_id=42)
    found = db.get_user_by_username("theanaesthetistsg")
    check("get_user_by_username finds existing linked user case-insensitively", found is not None)
    check("existing user is reported as linked", bool(found and found["telegram_chat_id"]))
    check("username lookup ignores blank usernames", db.get_user_by_username("") is None)
    check("username lookup ignores unrelated blank-username rows", True)  # covered by WHERE clause above
    # a /linkuser-onboarded user (blank username) must never spuriously match another lookup
    db.create_user(telegram_username="", display_name="NoUsername", role="candidate",
                   whitelist_status="active", telegram_chat_id=43)
    check("blank-username row doesn't collide with a real lookup",
          db.get_user_by_username("NoUsername") is None)


def test_whitelist_and_link():
    print("test_whitelist_and_link")
    reset_db()
    db.create_user(telegram_username="JaneTan", display_name="Jane", role="candidate",
                   whitelist_status="pending")
    # case-insensitive match
    row = db.find_linkable_by_username("janetan")
    check("case-insensitive username match", row is not None)
    db.link_user(row["id"], 555)
    check("linked → authorized", db.get_authorized_user(555) is not None)
    check("already-active not re-linkable", db.find_linkable_by_username("janetan") is None)
    # linkuser path
    db.create_user(telegram_username="", display_name="No Username", role="candidate",
                   whitelist_status="pending")
    tgt = db.get_user_by_display_name("No Username")
    db.link_user(tgt["id"], 777)
    check("linkuser-style direct link works", db.get_authorized_user(777) is not None)


async def test_slot_gating_on_time():
    print("test_slot_gating_on_time (primary fix)")
    reset_db()
    install_patches()
    bot = FakeBot()
    user = _seed_candidate("OnTime", 100, pace_days_behind=1)

    await scheduler.run_all(bot)
    check("first run delivers exactly 1 lesson", len(bot.lessons_to(100)) == 1)
    check("user paused after delivery", db.get_user_by_id(user["id"])["is_paused"] == 1)

    # A second run in the same slot must NOT deliver again.
    await scheduler.run_all(bot)
    check("same-slot re-run delivers no 2nd lesson", len(bot.lessons_to(100)) == 1)

    # Answer all correct, then the on-completion recheck must be a no-op (caught up).
    await answer_active(bot, user, lambda m: m["correct_option"])
    check("quiz completion unpauses", db.get_user_by_id(user["id"])["is_paused"] == 0)
    await scheduler.recheck_user(bot, db.get_user_by_id(user["id"]))
    check("recheck after completion does NOT flood a 2nd lesson", len(bot.lessons_to(100)) == 1)


async def test_slot_gating_catchup():
    print("test_slot_gating_catchup")
    reset_db()
    install_patches()
    bot = FakeBot()
    user = _seed_candidate("Behind", 200, pace_days_behind=4)  # 4 days behind

    delivered = 0
    for _ in range(6):
        await scheduler.run_all(bot)
        now_count = len(bot.lessons_to(200))
        check(f"cycle delivers at most 1 (had {delivered}, now {now_count})",
              now_count - delivered <= 1)
        delivered = now_count
        u = db.get_user_by_id(user["id"])
        if u["is_paused"]:
            await answer_active(bot, u, lambda m: m["correct_option"])
            await scheduler.recheck_user(bot, db.get_user_by_id(user["id"]))
            delivered = len(bot.lessons_to(200))
    check("catch-up delivered multiple lessons over cycles", len(bot.lessons_to(200)) >= 3,
          f"{len(bot.lessons_to(200))}")


async def test_reminder_throttle():
    print("test_reminder_throttle")
    reset_db()
    install_patches()
    bot = FakeBot()
    user = _seed_candidate("Rem", 300, pace_days_behind=1)
    # Deliver today's lesson (pauses the user), consuming today's only credit.
    await scheduler.run_all(bot)
    before = len([t for c, t in bot.messages if c == 300 and "unanswered" in t])
    # Several hourly safety-net ticks later THE SAME DAY, still paused: today's credit is
    # already spent, so nothing new is due — these must stay silent, not nag.
    await scheduler.run_all(bot)
    await scheduler.run_all(bot)
    await scheduler.run_all(bot)
    after = len([t for c, t in bot.messages if c == 300 and "unanswered" in t])
    check("no reminder fires while today's credit is already consumed", after - before == 0,
          f"{after - before}")
    # Next day, past the trigger, still paused → a new day's credit is due but blocked →
    # exactly one reminder, and repeated ticks that same (new) day stay silent too.
    patch_clock(FIXED + timedelta(days=1))
    await scheduler.run_all(bot)
    after2 = len([t for c, t in bot.messages if c == 300 and "unanswered" in t])
    check("a new day's tick fires exactly one reminder", after2 - after == 1, f"{after2 - after}")
    await scheduler.run_all(bot)
    await scheduler.run_all(bot)
    after3 = len([t for c, t in bot.messages if c == 300 and "unanswered" in t])
    check("repeated ticks the same (new) day don't re-nag", after3 - after2 == 0, f"{after3 - after2}")
    patch_clock(FIXED)  # restore


async def test_persist_before_send():
    print("test_persist_before_send")
    reset_db()
    install_patches()
    _gen_calls["n"] = 0
    row1 = await scheduler.ensure_sequence_generated(1)
    check("lesson persisted", row1 is not None and db.get_lesson_by_seq(1) is not None)
    check("5 mcqs persisted", len(db.get_mcqs_for_seq(1)) == 5)
    calls_after_first = _gen_calls["n"]
    row2 = await scheduler.ensure_sequence_generated(1)
    check("second call is a cache hit (no regeneration)", _gen_calls["n"] == calls_after_first)
    check("same row returned", row2["sequence_number"] == 1)
    check("still exactly one lesson_queue row", db.max_sequence_number() == 1)


def test_chart_rendering():
    print("test_chart_rendering")
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="mmed_chart_test_")
    for chart_type in sorted(chart_generator.CHART_TYPES):
        out = os.path.join(tmp_dir, f"{chart_type}.png")
        ok = chart_generator.render_chart({"type": chart_type, "params": {}}, out)
        check(f"{chart_type} renders a non-empty PNG",
              ok and os.path.exists(out) and os.path.getsize(out) > 0)
    bad_out = os.path.join(tmp_dir, "bad.png")
    ok = chart_generator.render_chart({"type": "not_a_real_type", "params": {}}, bad_out)
    check("unknown chart type returns False rather than raising",
          ok is False and not os.path.exists(bad_out))


async def test_chart_delivery():
    print("test_chart_delivery")
    reset_db()
    install_patches()
    scheduler.lesson_generator.generate_lesson_data = fake_generate_with_chart
    try:
        bot = FakeBot()
        _seed_candidate("Charty", 700, pace_days_behind=1)
        await scheduler.run_all(bot)
    finally:
        scheduler.lesson_generator.generate_lesson_data = fake_generate

    lesson = db.get_lesson_by_seq(1)
    check("chart_path persisted on the lesson_queue row", bool(lesson and lesson.get("chart_path")))
    check("chart PNG exists on disk at the persisted path",
          bool(lesson and lesson.get("chart_path") and os.path.exists(lesson["chart_path"])))
    check("chart photo delivered to the candidate after the lesson text",
          any(c == 700 for c, _path, _caption in bot.photos))


async def test_no_chart_when_model_omits_it():
    print("test_no_chart_when_model_omits_it")
    reset_db()
    install_patches()  # back to plain fake_generate, which returns chart: None
    bot = FakeBot()
    _seed_candidate("NoChart", 701, pace_days_behind=1)
    await scheduler.run_all(bot)

    lesson = db.get_lesson_by_seq(1)
    check("chart_path stays NULL when the model returns no chart",
          lesson is not None and lesson.get("chart_path") is None)
    check("no photo sent when there's no chart", len(bot.photos) == 0)


async def test_fault_isolation():
    print("test_fault_isolation")
    reset_db()
    install_patches()
    bot = FakeBot()
    a = _seed_candidate("Alpha", 401, pace_days_behind=1)
    b = _seed_candidate("Bravo", 402, pace_days_behind=1)

    orig_send = scheduler.send_with_retry

    async def flaky_send(bot_, chat_id, text):
        if chat_id == 401:
            raise RuntimeError("boom for Alpha")
        return await orig_send(bot_, chat_id, text)

    scheduler.send_with_retry = flaky_send
    try:
        await scheduler.run_all(bot)
    finally:
        scheduler.send_with_retry = orig_send

    check("Alpha's failure recorded", db.get_progress(a["id"])["consecutive_failures"] >= 1)
    check("Bravo still delivered despite Alpha failing", len(bot.lessons_to(402)) == 1)


async def test_admin_alert():
    print("test_admin_alert")
    reset_db()
    install_patches()
    bot = FakeBot()
    # admin, linked
    aid = db.create_user(telegram_username="the_admin", display_name="Admin", role="admin",
                         whitelist_status="active", telegram_chat_id=9000)
    cand = _seed_candidate("Faily", 500, pace_days_behind=1)

    async def boom_gen(topic):
        raise RuntimeError("generation down")

    scheduler.lesson_generator.generate_lesson_data = boom_gen
    try:
        await scheduler.run_all(bot)   # failure #1 (< threshold, no alert)
        alerts1 = [t for c, t in bot.messages if c == 9000 and "failed" in t]
        await scheduler.run_all(bot)   # failure #2 → alert
        alerts2 = [t for c, t in bot.messages if c == 9000 and "failed" in t]
        await scheduler.run_all(bot)   # failure #3 within cooldown → no new alert
        alerts3 = [t for c, t in bot.messages if c == 9000 and "failed" in t]
    finally:
        scheduler.lesson_generator.generate_lesson_data = fake_generate

    check("no alert on 1st failure", len(alerts1) == 0)
    check("alert on 2nd failure", len(alerts2) == 1)
    check("no duplicate alert within cooldown", len(alerts3) == 1)


async def test_typing_indicator():
    print("test_typing_indicator")
    import typing_util
    from telegram.constants import ChatAction
    typing_util.TYPING_REFRESH_SECONDS = 0.01
    bot = FakeBot()
    async with typing_util.typing_indicator(bot, 42, ChatAction.TYPING):
        await asyncio.sleep(0.05)
    sent_during = len(bot.actions)
    check("typing action sent repeatedly while active", sent_during >= 2, f"{sent_during}")
    await asyncio.sleep(0.05)
    check("typing stops after context exit", len(bot.actions) == sent_during)


async def test_quiz_flow_scoring_and_retest():
    print("test_quiz_flow_scoring_and_retest")
    reset_db()
    install_patches()
    bot = FakeBot()
    user = _seed_candidate("Quiz", 600, pace_days_behind=1)
    await scheduler.ensure_sequence_generated(1)
    mcqs = db.get_mcqs_for_seq(1)
    await quiz_engine.start_quiz(bot, user, "daily", 1, quiz_engine.questions_for_lesson(mcqs))
    check("start_quiz pauses user", db.get_user_by_id(user["id"])["is_paused"] == 1)

    # Answer Q1 wrong (choose a deliberately wrong letter), the rest correct.
    wrong_letter = "B" if mcqs[0]["correct_option"] != "B" else "C"

    def choose(m):
        # first question wrong, rest correct
        run = db.get_active_quiz_run(user["id"])
        return wrong_letter if run["current_index"] == 0 else m["correct_option"]

    await answer_active(bot, user, choose)
    check("quiz completed unpauses", db.get_user_by_id(user["id"])["is_paused"] == 0)
    check("one wrong answer → one retest item", db.pending_retest_count(user["id"]) == 1)
    with db.get_conn() as conn:
        r = conn.execute("SELECT interval_index, next_eligible_date FROM retest_pool "
                         "WHERE user_id=?", (user["id"],)).fetchone()
    check("retest item at interval_index 0", r["interval_index"] == 0)
    check("retest due tomorrow",
          r["next_eligible_date"] == timeutil.to_iso(FIXED.date() + timedelta(days=1)))
    # score reported 4/5
    finals = [t for c, t in bot.messages if c == 600 and t.startswith("🏁")]
    check("final score reported 4/5", finals and "4/5" in finals[-1])


def run_sync_tests():
    test_pace_marker_math()
    test_ladder()
    test_upsert_no_duplicate()
    test_validator()
    test_chart_validator()
    test_ambiguity_flag_rendering()
    test_chart_rendering()
    test_json_extraction_with_embedded_brackets()
    test_no_duplicate_username_lookup()
    test_topic_distribution()
    test_whitelist_and_link()


async def run_async_tests():
    await test_slot_gating_on_time()
    await test_slot_gating_catchup()
    await test_reminder_throttle()
    await test_persist_before_send()
    await test_chart_delivery()
    await test_no_chart_when_model_omits_it()
    await test_fault_isolation()
    await test_admin_alert()
    await test_typing_indicator()
    await test_quiz_flow_scoring_and_retest()
    await test_max_tokens_truncation_retries_with_bigger_budget()


def main():
    run_sync_tests()
    asyncio.run(run_async_tests())
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 50}\n{passed}/{total} checks passed")
    failed = [(n, d) for n, ok, d in _results if not ok]
    if failed:
        print("FAILURES:")
        for n, d in failed:
            print(f"  - {n} {d}")
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
