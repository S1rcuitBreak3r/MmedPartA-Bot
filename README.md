# MMed Anaesthesiology Part A — Exam-Prep Telegram Bot

A multi-user Telegram bot that delivers **two M.Med (Anaesthesiology) Part A lessons a
day** (09:30 and 14:30 SGT), each followed by a 5-question single-best-answer MCQ quiz,
individually paced per candidate, with per-user spaced-repetition retesting of wrong
answers and PDF export of the question bank and personal history.

Built for 4 candidates + 1 admin (Dr Tan). Architecture is ported from the working
single-user `telegram-italian-tutor-bot`, generalised to multiple users, MCQs, and a
Leitner-style spaced-repetition ladder. Full design rationale is in
`../MMed_Exam_Bot_Spec.md` (see its Revision Log for the two reliability review passes).

---

## What it does

- **Twice-daily delivery**, paced per user via a *slot-credit pace marker*: each 09:30/14:30
  boundary grants one lesson credit; each delivery consumes exactly one. A fast candidate
  can't blow through the queue in a sitting; a candidate who fell behind catches up one
  lesson per completed quiz, not in a flood.
- **Exam-weighted rotation** across the six official SG subjects (Physiology 32%,
  Pharmacology+Biostatistics 33%, Physics & Equipment 15%, Clinical Medicine 10%,
  Anatomy 10%), seeded from the official NUS DGMS source (`syllabus_data.py`).
- **Spaced repetition**: a wrong answer enters a retest ladder at intervals
  [1, 3, 7, 16] days; must be answered correctly at each and twice at 16 days before it
  retires. Surface due items on demand with `/recap`.
- **PDF export**: `/exportmcqs` (admin, whole bank) and `/myexport` (self, personal
  history + ladder).

## Reliability properties (why the design is the way it is)

- One shared lesson is generated once and cached; every user sees byte-identical content.
- **Persist-before-send**: content is written to the DB *before* any Telegram delivery, so
  a failed send never regenerates (and never rewrites) what another user already saw.
- One `asyncio.Lock` serialises all generation across all five trigger paths; the SQLite
  layer runs in WAL mode with a busy timeout for safe concurrent writes.
- Per-user fault isolation: one candidate's outage never blocks the others in a tick, and
  the admin gets a Telegram DM if a candidate stays stuck across retries.
- Quiz state lives entirely in SQLite, so a Railway restart mid-quiz resumes cleanly.

---

## Commands

**Everyone (whitelisted):** `/start` `/help` `/status` `/whoami` `/recap` `/skipquiz`
`/cancelquiz` `/myexport` `/mcqcount` `/forcelesson` `/pausetest` `/forceretest [days]`

**Admin only:** `/adduser <username> <display name>` · `/linkuser <display name> <chat_id>`
· `/listusers` · `/progress <name>` · `/removeuser <name>` · `/resetprogress <name>` ·
`/exportmcqs` · admin-on-target forms of `/recap <name>`, `/forcelesson <name>`,
`/pausetest <name>`

`/linkuser` is the fallback for a candidate whose Telegram account has no `@username`:
they get their numeric id from **@userinfobot** and the admin links it directly.

---

## Deploy on Railway

1. **New bot token** from @BotFather (a *separate* bot from the Italian/AI tutor bots).
2. **New Railway project** → Deploy from GitHub repo → this repo.
3. **Attach a persistent volume** (Service → Settings → Volumes → New Volume), mount path
   e.g. `/data`. Turn on at least the daily backup schedule (Volume → Backups).
4. **Variables** (Service → Variables):
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `ADMIN_TELEGRAM_USERNAME` — your Telegram @username, no leading `@`
   - `DATABASE_PATH` = `/data/mmed_bot.db` (inside the mounted volume — the default
     container filesystem is wiped on redeploy)
   - `RAILWAY_RUN_UID` = `0` — only if volume writes fail with a permissions error
     (Railway volumes mount as root; some Nixpacks images run as non-root)
5. Redeploy, open **View Logs**, confirm `Bot starting…` with no traceback.
6. In Telegram, send `/start` to the bot **as the admin** — this links your chat id to the
   auto-created admin row. Then `/adduser` each candidate (or `/linkuser` for anyone
   without a username).

The syllabus is seeded automatically on first boot from `syllabus_data.py`. Verified-bank
content from the senior's Notion notes is a tracked follow-up (spec §11), not yet ingested.

---

## Local development & tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python test_offline.py        # 60 offline checks, no network — run before every deploy
```

`test_offline.py` covers the slot/pace math and the primary anti-flood fix, the spaced-rep
ladder, the retest upsert, whitelist matching, topic-weighting distribution, semantic JSON
validation, persist-before-send, per-user fault isolation, the admin alert, the typing
indicator, and the full quiz-scoring flow. `seed_test_users.py [N]` inserts synthetic
candidates for DB-level pacing exercises.

## Files

```
config.py            env vars + tunable constants (slots, intervals, retries, alerts)
timeutil.py          SGT helpers + slot-window / pace-marker math
syllabus_data.py     official SG Part A subjects/topics/weights (seed for syllabus_topics)
db.py                SQLite schema (WAL) + all persistence
claude_client.py     Anthropic wrapper: retries + JSON syntax AND semantic-contract repair
curriculum.py        exam-weighted topic selection for the shared lesson_queue
lesson_generator.py  lesson + 5-MCQ generation, contract validator, bot-owned rendering
quiz_engine.py       inline-keyboard MCQ quiz, exact-match grading, retest ladder updates
typing_util.py       re-sending typing / upload-document chat-action context manager
scheduler.py         per-user pacing, the 5 trigger sites, generation, admin alerting
pdf_export.py        /exportmcqs and /myexport via fpdf2
bot.py               Telegram handlers + entrypoint
seed_test_users.py   synthetic candidates for offline pacing tests
test_offline.py      no-network test suite (§12.1)
```
