"""
Topic selection for the shared lesson_queue (§2, §6). Unlike the reference bot's
strictly-ordered CEFR list, this is a WEIGHTED round-robin: pick the subject whose
actual share of generated lessons trails its target share (the official §2 weighting)
by the most, then the next-uncovered / least-recently-covered topic within it.

No Claude calls happen here — this only reads the DB and chooses what to teach next;
lesson_generator.py does the content generation.
"""
from __future__ import annotations

import db
from syllabus_data import SUBJECT_TARGET_WEIGHTS

# The Pharmacology + Biostatistics bucket shares one weighting target (§2). For the
# gap calculation we treat Biostatistics topics as part of the Pharmacology subject
# bucket, so the combined share is measured against the single 33% target.
_WEIGHT_BUCKET = {
    "Physiology": "Physiology",
    "Pharmacology": "Pharmacology",
    "Biostatistics": "Pharmacology",
    "Physics and Equipment": "Physics and Equipment",
    "Clinical Medicine": "Clinical Medicine",
    "Anatomy": "Anatomy",
}


def choose_next_topic():
    """Return the syllabus_topics row (dict) that should back the next lesson_queue entry.

    Rule (§2): the subject bucket with the largest (target_share - actual_share) gap;
    within it, the not-yet-covered topic with the fewest times_covered, tie-broken by
    the smallest last_covered_seq (least recently covered) then id.
    """
    topics = db.get_all_topics()
    if not topics:
        raise RuntimeError("syllabus_topics is empty — seed it before generating lessons (§11).")

    subject_counts = db.subject_counts_in_queue()
    total_generated = sum(subject_counts.values())

    # Actual share per weight-bucket.
    bucket_counts: dict[str, int] = {}
    for subj, cnt in subject_counts.items():
        bucket = _WEIGHT_BUCKET.get(subj, subj)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + cnt

    def bucket_gap(bucket: str) -> float:
        target = SUBJECT_TARGET_WEIGHTS.get(bucket, 0.0) / 100.0
        actual = (bucket_counts.get(bucket, 0) / total_generated) if total_generated else 0.0
        return target - actual

    # Rank each candidate topic by (its bucket's gap desc, times_covered asc,
    # last_covered_seq asc, id asc). Largest gap first so under-served subjects catch up.
    def sort_key(t):
        bucket = _WEIGHT_BUCKET.get(t["subject"], t["subject"])
        return (
            -bucket_gap(bucket),
            t["times_covered"],
            t["last_covered_seq"] if t["last_covered_seq"] is not None else -1,
            t["id"],
        )

    return sorted(topics, key=sort_key)[0]


def topic_area_for(topic: dict) -> str:
    return topic["topic_area"]
