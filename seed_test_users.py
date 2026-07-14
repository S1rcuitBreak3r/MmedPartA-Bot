"""
Seed N synthetic candidate rows with placeholder (negative) chat ids for DB-level
pacing/spaced-rep exercise without touching the Telegram API (§13). Delivery code
that sends to these ids is expected to be run only in offline tests where the bot is
mocked. Run: python seed_test_users.py [N]
"""
import sys

import db
import syllabus_data


def main(n: int = 3):
    db.init_db()
    db.seed_syllabus_topics(syllabus_data.iter_seed_rows())
    for i in range(1, n + 1):
        display = f"Test User {i}"
        if db.get_user_by_display_name(display):
            continue
        uid = db.create_user(
            telegram_username=f"test_user_{i}", display_name=display,
            role="candidate", whitelist_status="active", telegram_chat_id=-i,
        )
        print(f"Created {display} (user_id={uid}, chat_id={-i})")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    main(count)
