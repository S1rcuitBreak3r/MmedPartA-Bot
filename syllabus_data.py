"""
Seed data for `syllabus_topics`, transcribed verbatim from the official live source
(NUS DGMS, "Summary of MMed (Anaesthesiology) Examination – Part A", Dr Tay Kwang Hui):
https://medicine.nus.edu.sg/dgms/wp-content/uploads/sites/30/2021/09/mmed_anaes_a_exam_format_topics.pdf

This replaces the never-saved MMed_Syllabus_Comparison.md referenced in the spec (§2, §11).
The six official SG subjects and their per-subject topic breakdown are below. `topic_area`
is the 4-value user-facing collapse from spec §2 (Others = Clinical Medicine + Anatomy +
Biostatistics). weight_pct is the official Part-A estimated weightage.

Only SG core rows are seeded here (is_core=1). FRCA/ANZCA supplementary gap-fill rows are a
tracked follow-up (spec §11) and are intentionally NOT included until independently verified.
"""

# subject -> (topic_area, weight_pct)
SUBJECT_META = {
    "Physiology": ("Physiology", 32.0),
    "Pharmacology": ("Pharmacology", 33.0),        # shares the 33% bucket with Biostatistics
    "Biostatistics": ("Others", 33.0),             # official weighting lumps Pharmacology + Biostatistics
    "Physics and Equipment": ("Equipment", 15.0),
    "Clinical Medicine": ("Others", 10.0),
    "Anatomy": ("Others", 10.0),
}

# subject -> [topic_title, ...] (verbatim from the source)
SUBJECT_TOPICS = {
    "Physiology": [
        "Respiratory",
        "Cardiovascular",
        "Renal, Cellular, Body Fluids and electrolytes, Acid Base",
        "Nervous system, Musculoskeletal",
        "Liver, Nutrition, Gastrointestinal",
        "Haematology, Immunology",
        "Endocrine and Thermoregulation",
        "Maternal, Fetal and Neonatal",
    ],
    "Pharmacology": [
        "General Pharmacology: Pharmacokinetics, Pharmacodynamics, Variability in drug actions, Pharmaceutical aspects and drug development",
        "Pharmacology of specific drugs: Core Anaesthetic drugs",
        "Drugs used to maintain physiological state (e.g. CVS, Resp drugs)",
        "Drugs used to manage disease conditions and poisoning",
    ],
    "Biostatistics": [
        "Biostatistics / Clinical trials",
    ],
    "Physics and Equipment": [
        "Physics and Measurement (e.g. common gas laws)",
        "Clinical Monitoring (e.g. common monitoring equipment used in OT)",
        "Equipment and safety (e.g. anaesthesia machine, airway equipment, safety in the OT)",
    ],
    "Clinical Medicine": [
        "Acute Medicine (e.g. ACLS, common crisis encountered in OT)",
        "Common issues in Perioperative Medicine (e.g. URTI, airway assessment, management of chronic disease, acute pain management)",
    ],
    "Anatomy": [
        "Head and Neck (including airway)",
        "Cardiovascular / Respiratory anatomy",
        "Neuroanatomy (central and peripheral nervous system)",
    ],
}

# The official weighting table (spec §2) — used by curriculum.py's topic-selection rule.
SUBJECT_TARGET_WEIGHTS = {
    "Physiology": 32.0,
    "Pharmacology": 33.0,   # the Pharmacology+Biostatistics bucket, treated as one for weighting
    "Physics and Equipment": 15.0,
    "Clinical Medicine": 10.0,
    "Anatomy": 10.0,
}


def iter_seed_rows():
    """Yield (source_exam, subject, topic_title, is_core, weight_pct, topic_area) for each
    seed topic — consumed by db.seed_syllabus_topics()."""
    for subject, topics in SUBJECT_TOPICS.items():
        topic_area, weight = SUBJECT_META[subject]
        for title in topics:
            yield ("SG", subject, title, 1, weight, topic_area)
