import base64
import html
import json
import os
import re
import unicodedata
import psycopg2
import psycopg2.extras
from collections import defaultdict
from dotenv import load_dotenv
from nameparser import HumanName
from rapidfuzz.fuzz import token_set_ratio

load_dotenv()

DB_URL = os.environ["DATABASE_URL"]

INVALID_DEPTS = {
    "Select department", "Not Specified", "TA",
    "Academic Services", "Student Affairs", "Continuing Education", "Study Abroad", "Safety",
}
DEPT_ALIASES = {
    "Gender Women & Sexuality Studies": "Gender, Women, & Sexuality Studies",
    "Physical Ed": "Physical Education",
    "Cinema": "Film",
    "Women": "Gender, Women, & Sexuality Studies",
    "Business": "Business Administration",
    "Computer Science & Engineering": "Computer Science & Electrical Engineering",
    "Biological Sciences": "Biology",
    "Forest Resources": "Forestry",
    "Speech Pathology & Audiology": "Speech & Hearing Sciences",
    "Women's Studies": "Gender, Women, & Sexuality Studies",
}

GRADE_KEYS = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F",
              "Pass", "Fail", "Incomplete", "Drop/Withdrawal", "Audit/No Grade",
              "Rather not say", "Not sure yet"]

TAG_ALIASES = {
    "Cares About Students": "Caring",
    "Respected By Students": "Respected",
}


def normalize_tag(tag):
    tag = tag.strip().title()
    tag = re.sub(r"'([A-Z])", lambda m: "'" + m.group(1).lower(), tag)
    return TAG_ALIASES.get(tag, tag)


GRADE_ALIASES = {
    "Not_Sure_Yet":  "Not sure yet",
    "Rather_Not_Say": "Rather not say",
    "Audit/No_Grade": "Audit/No Grade",
}
QUARTER_MAP = {"WI": ("Winter", 1), "SP": ("Spring", 3), "SU": ("Summer", 7), "AU": ("Autumn", 9)}
NAME_TO_MONTH = {name: month for name, month in QUARTER_MAP.values()}
FUZZY_THRESHOLD = 0.68
HYBRID_THRESHOLD = 85   # rapidfuzz token_set_ratio
HYBRID_MIN_SHARED = 2   # minimum shared tokens >= 4 chars


def normalize_dept(dept):
    if not dept or dept in INVALID_DEPTS:
        return None
    dept = html.unescape(dept)
    dept = re.sub(r'\bamp\b', '&', dept)
    dept = re.sub(r'  +', ' & ', dept)
    dept = ' '.join(dept.split())
    if dept == dept.lower():
        dept = dept.title()
    dept = re.sub(r'\band\b', '&', dept, flags=re.IGNORECASE)
    return DEPT_ALIASES.get(dept, dept) or None


def parse_quarter(raw):
    if not raw or len(raw) < 3:
        return None, None
    prefix = raw[:2].upper()
    try:
        year = 2000 + int(raw[2:])
    except ValueError:
        return None, None
    name, _ = QUARTER_MAP.get(prefix, (None, None))
    return name, year


def quarter_sort_key(quarter, year):
    return year * 100 + NAME_TO_MONTH.get(quarter, 0) if quarter and year else 0


def compute_derived(ratings):
    grade_counts = {g: 0 for g in GRADE_KEYS}
    rating_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    diff_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    online_yes = online_total = 0
    att_yes = att_total = 0
    tag_counts = {}
    course_counts = {}

    for r in ratings:
        grade = GRADE_ALIASES.get(r["grade"], r["grade"])
        if grade in grade_counts:
            grade_counts[grade] += 1
        hr = r["quality_rating"]
        if hr and 1 <= hr <= 5:
            rating_counts[str(hr)] += 1
        dr = r["difficulty_rating"]
        if dr and 1 <= dr <= 5:
            diff_counts[str(dr)] += 1
        if r.get("is_online") is not None:
            online_total += 1
            if r["is_online"]:
                online_yes += 1
        att = r.get("attendance_mandatory")
        if att in ("mandatory", "Y", "non mandatory", "N"):
            att_total += 1
            if att in ("mandatory", "Y"):
                att_yes += 1
        for tag in (r.get("rating_tags") or "").split("--"):
            tag = normalize_tag(tag)
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        course = normalize_course(r.get("class")) or "Other"
        course_counts[course] = course_counts.get(course, 0) + 1

    is_online_pct = round(online_yes / online_total * 100, 2) if online_total else None
    att_pct = round(att_yes / att_total * 100, 2) if att_total else None
    tag_dist = json.dumps(sorted(
        [{"tag": k, "count": v} for k, v in tag_counts.items()],
        key=lambda x: -x["count"]
    )) if tag_counts else None

    rmp_courses = json.dumps(sorted(
        [{"code": k, "count": v} for k, v in course_counts.items()],
        key=lambda x: -x["count"]
    )) if course_counts else None

    return (
        json.dumps([{"grade": g, "count": grade_counts[g]} for g in GRADE_KEYS]),
        json.dumps(rating_counts),
        json.dumps(diff_counts),
        is_online_pct,
        att_pct,
        tag_dist,
        rmp_courses,
    )


def weighted(values, weights):
    pairs = [(v, w) for v, w in zip(values, weights) if v is not None and w]
    if not pairs:
        return None
    return round(sum(v * w for v, w in pairs) / sum(w for _, w in pairs), 2)


def _wavg(pairs):
    valid = [(v, w) for v, w in pairs if v is not None]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    if total_w == 0:
        return round(sum(v for v, _ in valid) / len(valid), 4)
    return round(sum(v * w for v, w in valid) / total_w, 4)


def _rmp_dedup_key(p):
    first = re.sub(r'\(.*?\)', '', p["first_name"] or '').strip()
    last = re.sub(r'\s*-\s*', '-', p["last_name"] or '')
    return norm_name(first, None, last)


def _fl_key(name):
    first, _, last = parse_name(name)
    return (strip_accents((first or '').lower()), strip_accents((last or '').lower()))


def _shared_long_tokens(a, b):
    ta = {t for t in a.split() if len(t) >= 4}
    tb = {t for t in b.split() if len(t) >= 4}
    return len(ta & tb)


def rmp_url(rmp_id):
    if not rmp_id:
        return None
    decoded = base64.b64decode(rmp_id + '==').decode('utf-8')
    legacy_id = decoded.split('-')[-1]
    return f"https://www.ratemyprofessors.com/professor/{legacy_id}"


def normalize_course(raw):
    if not raw:
        return None
    s = raw.strip().upper().replace(' ', '')
    m = re.fullmatch(r'([A-Z&/]{2,6})(\d{3})[A-Z]?', s)
    return m.group(1) + m.group(2) if m else None


def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def norm_name(first, middle, last):
    combined = ((first or '') + ' ' + (middle or '') + ' ' + (last or '')).strip().lower()
    return strip_accents(' '.join(combined.split()))


def normalize_initials(s):
    s = re.sub(r'([A-Za-z]\.)(?=[A-Za-z])', r'\1 ', s)
    parts = s.split()
    return ' '.join(p.rstrip('.') + '.' if re.fullmatch(r'[A-Za-z]\.?', p) else p for p in parts)


def parse_name(full_name, capitalize=False):
    n = HumanName(full_name or "")
    if capitalize:
        n.capitalize()
    middle = normalize_initials(n.middle) if n.middle else None
    return n.first or None, middle, n.last or None


def apply_cec_name(prof, cec_name):
    first, middle, last = parse_name(cec_name)
    prof["first_name"]  = first
    prof["middle_name"] = middle
    prof["last_name"]   = last


def pg_trigrams(s):
    trgms = set()
    for word in re.split(r'[^a-z0-9]+', s):
        if not word:
            continue
        padded = '  ' + word + ' '
        for i in range(len(padded) - 2):
            trgms.add(padded[i:i+3])
    return frozenset(trgms)


def pg_similarity(ta, tb):
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union


def combined_school_name(school_ids, school_names):
    names = sorted(set(school_names.get(sid, sid) for sid in school_ids if sid))
    return "All campuses" if len(names) == 3 else " and ".join(names)


RESPONSE_ORDER = ["very_poor", "poor", "fair", "good", "very_good", "excellent", "median"]

MEDIAN_QUESTIONS = [
    ("Instructor's contribution",  "instructor_contribution_median_weighted"),
    ("Instructor's effectiveness", "instructor_effectiveness_median_weighted"),
    ("The course as a whole",      "course_as_whole_median_weighted"),
    ("The course content",         "course_content_median_weighted"),
    ("Amount learned",             "amount_learned_median_weighted"),
    ("Instuctor's interest",       "instructor_interest_median_weighted"),
    ("Grading techniques",         "grading_techniques_median_weighted"),
]


def _eval_avg_median(questions):
    if not questions:
        return None
    vals = [q_data.get("median") for q_data in questions.values()
            if isinstance(q_data, dict) and q_data.get("median") is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _get_median(questions, question_name):
    if not questions or question_name not in questions:
        return None
    q = questions[question_name]
    if isinstance(q, dict):
        return q.get("median")
    if isinstance(q, list):
        for item in q:
            if item.get("level") == "median":
                return item.get("value")
    return None

def _format_questions(questions):
    if not questions:
        return None
    if isinstance(questions, str):
        questions = json.loads(questions)
    return json.dumps({
        q: [{"level": k, "value": r.get(k)} for k in RESPONSE_ORDER if k in r]
        for q, r in questions.items()
    })


def init_db(conn):
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id SERIAL PRIMARY KEY,
            first_name TEXT,
            middle_name TEXT,
            last_name TEXT,
            title TEXT,
            school TEXT,
            departments JSONB,
            rmp_rating_count INTEGER,
            cec_eval_count INTEGER,
            cec_surveyed_count INTEGER,
            cec_enrolled_count INTEGER,
            avg_eval_median_weighted REAL,
            avg_instructor_contribution_median_weighted REAL,
            avg_instructor_effectiveness_median_weighted REAL,
            avg_course_as_whole_median_weighted REAL,
            avg_course_content_median_weighted REAL,
            avg_amount_learned_median_weighted REAL,
            avg_instructor_interest_median_weighted REAL,
            avg_grading_techniques_median_weighted REAL,
            rating_tags_distribution JSONB,
            avg_quality_rating REAL,
            avg_difficulty_rating REAL,
            would_take_again_percent REAL,
            is_online_percent REAL,
            attendance_is_mandatory_percent REAL,
            grade_distribution JSONB,
            rating_distribution JSONB,
            difficulty_distribution JSONB,
            rmp_courses JSONB,
            rmp_url TEXT,
            cec_courses JSONB,
            updated_at TIMESTAMP,
            source TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cec_evaluations (
            professor_id INTEGER,
            url TEXT PRIMARY KEY,
            course_name TEXT,
            course_code TEXT,
            section TEXT,
            instructor_name TEXT,
            title TEXT,
            quarter TEXT,
            year INTEGER,
            form_type TEXT,
            surveyed INTEGER,
            enrolled INTEGER,
            questions JSONB,
            eval_avg_median REAL,
            instructor_contribution_median REAL,
            instructor_effectiveness_median REAL,
            course_as_whole_median REAL,
            course_content_median REAL,
            amount_learned_median REAL,
            instructor_interest_median REAL,
            grading_techniques_median REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rmp_ratings (
            id TEXT PRIMARY KEY,
            professor_id INTEGER,
            class TEXT,
            rmp_class TEXT,
            date TEXT,
            comment TEXT,
            quality_rating INTEGER,
            difficulty_rating INTEGER,
            grade TEXT,
            would_take_again BOOLEAN,
            is_online BOOLEAN,
            attendance_is_mandatory BOOLEAN,
            textbook_used BOOLEAN,
            rating_tags TEXT[]
        )
    """)
    conn.commit()
    cur.close()


def main():
    conn = psycopg2.connect(DB_URL, sslmode="require")
    init_db(conn)

    # ── READ PHASE ──
    print("Reading raw data...")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rmp_professors_raw")
    rmp_profs_raw = cur.fetchall()
    cur.execute("SELECT * FROM rmp_ratings_raw")
    all_ratings_raw = cur.fetchall()
    cur.execute("SELECT * FROM cec_evaluations_raw")
    cec_evals_raw = cur.fetchall()
    cur.execute("SELECT rmp_id, name FROM schools WHERE rmp_id IS NOT NULL")
    school_names = {r["rmp_id"]: r["name"] for r in cur.fetchall()}
    cur.close()
    print(f"  RMP: {len(rmp_profs_raw):,} professors, {len(all_ratings_raw):,} ratings")
    print(f"  CEC: {len(cec_evals_raw):,} evaluations")

    ratings_by_prof = defaultdict(list)
    for r in all_ratings_raw:
        ratings_by_prof[r["professor_id"]].append(r)

    # ── STEP 1: DEDUPLICATION ──
    print("\nDeduplicating RMP professors...")

    name_to_profs = defaultdict(list)
    for p in rmp_profs_raw:
        name_to_profs[_rmp_dedup_key(p)].append(p)

    # Merge groups where first and last name are swapped (RMP data entry error)
    seen_reversed = set()
    for key in list(name_to_profs.keys()):
        if key in seen_reversed:
            continue
        parts = key.split()
        if len(parts) == 2:
            rev = f"{parts[1]} {parts[0]}"
            if rev in name_to_profs:
                name_to_profs[key].extend(name_to_profs.pop(rev))
                seen_reversed.add(rev)

    loser_ids = set()
    winner_overrides = {}
    winner_to_all_rmp_ids = {}
    new_school_names = set()
    same_school_groups = []
    cross_campus_groups = []

    for name, profs in name_to_profs.items():
        if len(profs) <= 1:
            continue
        profs = sorted(profs, key=lambda p: p["rmp_rating_count"] or 0, reverse=True)
        winner = profs[0]

        school_ids = [p["school_id"] for p in profs]
        if len(set(school_ids)) == 1:
            same_school_groups.append(profs)
        else:
            cross_campus_groups.append(profs)

        for loser in profs[1:]:
            loser_ids.add(loser["id"])

        winner_to_all_rmp_ids[winner["id"]] = [p["id"] for p in profs]

        rmp_rating_count_list = [p["rmp_rating_count"] for p in profs]
        school = combined_school_name(school_ids, school_names)
        new_school_names.add(school)
        unique_depts = list(dict.fromkeys(normalize_dept(p["department"]) for p in profs if p["department"]))
        unique_depts = [d for d in unique_depts if d]

        winner_overrides[winner["id"]] = {
            "avg_quality_rating":         weighted([p["avg_quality_rating"] for p in profs], rmp_rating_count_list),
            "avg_difficulty_rating":     weighted([p["avg_difficulty_rating"] for p in profs], rmp_rating_count_list),
            "would_take_again_percent": weighted([p["would_take_again"] for p in profs], rmp_rating_count_list),
            "rmp_rating_count":        sum(n for n in rmp_rating_count_list if n),
            "school":             school,
            "departments":        json.dumps(unique_depts),
        }

    same_school_total = sum(len(g) for g in same_school_groups)
    cross_campus_total = sum(len(g) for g in cross_campus_groups)
    print(f"  Same-school:  {same_school_total} profiles combined into {len(same_school_groups)} professors")
    print(f"  Cross-campus: {cross_campus_total} profiles combined into {len(cross_campus_groups)} professors")
    print(f"  Total: {len(loser_ids)} duplicates removed, {len(same_school_groups) + len(cross_campus_groups)} professors merged")
    print(f"  RMP professors after dedup: {len(rmp_profs_raw) - len(loser_ids):,}")

    # ── STEP 2: BUILD RMP PROFESSORS ──
    print("\nBuilding professor data...")

    def get_all_ratings(rmp_id):
        all_ids = winner_to_all_rmp_ids.get(rmp_id, [rmp_id])
        return [r for pid in all_ids for r in ratings_by_prof.get(pid, [])]

    professors = []
    rmp_norm_to_prof = {}

    for p in rmp_profs_raw:
        if p["id"] in loser_ids:
            continue
        ov = winner_overrides.get(p["id"], {})
        ratings = get_all_ratings(p["id"])
        grade_dist, rating_dist, diff_dist, is_online_pct, att_pct, tag_dist, rmp_courses = compute_derived(ratings)
        _first, _middle, _last = parse_name(f"{p['first_name'] or ''} {p['last_name'] or ''}", capitalize=True)
        prof = {
            "rmp_id":                 p["id"],
            "rmp_url":                rmp_url(p["id"]),
            "school":                 ov.get("school", school_names.get(p["school_id"])),
            "first_name":             _first,
            "middle_name":            _middle,
            "last_name":              _last,
            "departments":            ov.get("departments", json.dumps([normalize_dept(p["department"])] if normalize_dept(p["department"]) else [])),
            "avg_quality_rating":             ov.get("avg_quality_rating", p["avg_quality_rating"]),
            "avg_difficulty_rating":         ov.get("avg_difficulty_rating", p["avg_difficulty_rating"]),
            "rmp_rating_count":            ov.get("rmp_rating_count", p["rmp_rating_count"]),
            "cec_eval_count":             None,
            "cec_surveyed_count":         None,
            "cec_enrolled_count":         None,
            "avg_eval_median_weighted":            None,
            **{f"avg_{col}": None for _, col in MEDIAN_QUESTIONS},
            "would_take_again_percent": ov.get("would_take_again_percent", p["would_take_again"]),
            "cec_courses":                None,
            "updated_at":             p["updated_at"],
            "grade_distribution":     grade_dist,
            "rating_distribution":    rating_dist,
            "difficulty_distribution": diff_dist,
            "rmp_courses":                rmp_courses,
            "is_online_percent":          is_online_pct,
            "attendance_is_mandatory_percent": att_pct,
            "rating_tags_distribution":   tag_dist,
            "title":                  None,
            "source":                 "rmp",
        }
        name = norm_name(_first, _middle, _last)
        rmp_norm_to_prof.setdefault(name, []).append(prof)
        professors.append(prof)

    print(f"  Built {len(professors):,} professors from RMP")

    # ── STEP 3: CEC MATCHING ──
    print("\nMatching CEC instructors to RMP professors...")

    # Precompute trigrams for all RMP professor names
    rmp_norm_trgms = {name: pg_trigrams(name) for name in rmp_norm_to_prof}

    # ── CEC name deduplication ──
    # Group variants by (first, last), then split any group where two variants
    # exact-match *different* RMP professors — that's a reliable signal they're
    # different people sharing the same first+last name.
    def _exact_rmp_id(name):
        normalized = strip_accents(' '.join(name.strip().lower().split()))
        matches = rmp_norm_to_prof.get(normalized, [])
        return matches[0]["rmp_id"] if len(matches) == 1 else None

    _cec_name_groups = defaultdict(list)
    for name in {e["instructor_name"] for e in cec_evals_raw if e["instructor_name"]}:
        _cec_name_groups[_fl_key(name)].append(name)

    cec_variant_to_canonical = {}
    for group in _cec_name_groups.values():
        # Sub-group by exact RMP match; None (no match) stays with the matched sub-group
        # unless every variant is unmatched, in which case they all stay together.
        by_rmp = defaultdict(list)
        for name in group:
            by_rmp[_exact_rmp_id(name)].append(name)

        matched_ids = [k for k in by_rmp if k is not None]
        if len(matched_ids) > 1:
            # Multiple distinct RMP profs → split: each matched ID is its own group,
            # unmatched variants go with whichever matched group shares their (first, last)
            # but since we can't tell, keep unmatched as a separate group too.
            subgroups = list(by_rmp.values())
        else:
            # All agree (same match or all unmatched) → one group
            subgroups = [group]

        for subgroup in subgroups:
            canonical = max(subgroup, key=lambda n: (1 if parse_name(n)[1] else 0, len(n)))
            for variant in subgroup:
                cec_variant_to_canonical[variant] = canonical

    cec_names = list(set(cec_variant_to_canonical.values()))
    cec_name_to_prof = {}
    merged_prof_ids = set()
    exact_matches = fuzzy_matches = late_merges = new_profs = 0

    for cec_name in cec_names:
        normalized = strip_accents(' '.join(cec_name.strip().lower().split()))

        # Exact match
        profs = rmp_norm_to_prof.get(normalized, [])
        if len(profs) == 1:
            prof = profs[0]
            prof["source"] = "both"
            apply_cec_name(prof, cec_name)
            cec_name_to_prof[cec_name] = prof
            exact_matches += 1
            continue

        # Fuzzy match using precomputed trigrams
        cec_trgms = pg_trigrams(normalized)
        scored = sorted(
            [(name, sim)
             for name, trgms in rmp_norm_trgms.items()
             if (sim := pg_similarity(cec_trgms, trgms)) > FUZZY_THRESHOLD],
            key=lambda x: -x[1],
        )[:5]

        if len(scored) == 1:
            prof = rmp_norm_to_prof[scored[0][0]][0]
            prof["source"] = "both"
            apply_cec_name(prof, cec_name)
            cec_name_to_prof[cec_name] = prof
            fuzzy_matches += 1
            continue

        if len(scored) > 1:
            winner_name, _ = scored[0]
            winner_prof = rmp_norm_to_prof[winner_name][0]

            # Check inter-match similarity — similar candidates are the same person
            similar_losers = [
                rmp_norm_to_prof[name][0]
                for name, _ in scored[1:]
                if pg_similarity(rmp_norm_trgms[winner_name], rmp_norm_trgms[name]) > FUZZY_THRESHOLD
            ]

            if similar_losers:
                all_profs = [winner_prof] + similar_losers
                loser_rmp_ids = [p["rmp_id"] for p in similar_losers if p["rmp_id"]]
                combined_ratings = get_all_ratings(winner_prof["rmp_id"])
                for rmp_id in loser_rmp_ids:
                    combined_ratings.extend(ratings_by_prof.get(rmp_id, []))

                grade_dist, rating_dist, diff_dist, is_online_pct, att_pct, tag_dist, rmp_courses = compute_derived(combined_ratings)
                rmp_rating_count_list = [p["rmp_rating_count"] for p in all_profs]
                winner_prof.update({
                    "avg_quality_rating":             weighted([p["avg_quality_rating"] for p in all_profs], rmp_rating_count_list),
                    "avg_difficulty_rating":          weighted([p["avg_difficulty_rating"] for p in all_profs], rmp_rating_count_list),
                    "would_take_again_percent": weighted([p["would_take_again_percent"] for p in all_profs], rmp_rating_count_list),
                    "rmp_rating_count":             sum(n for n in rmp_rating_count_list if n),
                    "grade_distribution":      grade_dist,
                    "rating_distribution":     rating_dist,
                    "difficulty_distribution": diff_dist,
                    "rmp_courses":                 rmp_courses,
                    "is_online_percent":           is_online_pct,
                    "attendance_is_mandatory_percent": att_pct,
                    "rating_tags_distribution":    tag_dist,
                    "source":                  "both",
                })
                apply_cec_name(winner_prof, cec_name)

                # Redirect stale mappings and remove losers
                for loser in similar_losers:
                    for k, v in list(cec_name_to_prof.items()):
                        if v is loser:
                            cec_name_to_prof[k] = winner_prof
                    merged_prof_ids.add(id(loser))
                    loser_name = norm_name(loser["first_name"], loser.get("middle_name"), loser["last_name"])
                    rmp_norm_to_prof.pop(loser_name, None)
                    rmp_norm_trgms.pop(loser_name, None)

                cec_name_to_prof[cec_name] = winner_prof
                late_merges += 1
                continue

        # Hybrid match — token set ratio with shared-token guard

        hybrid_scored = sorted(
            [(name, score)
             for name in rmp_norm_to_prof
             if (score := token_set_ratio(normalized, name)) >= HYBRID_THRESHOLD
             and _shared_long_tokens(normalized, name) >= HYBRID_MIN_SHARED],
            key=lambda x: -x[1],
        )

        if len(hybrid_scored) == 1:
            prof = rmp_norm_to_prof[hybrid_scored[0][0]][0]
            prof["source"] = "both"
            apply_cec_name(prof, cec_name)
            cec_name_to_prof[cec_name] = prof
            fuzzy_matches += 1
            continue

        # No match — new CEC-only professor
        _first, _middle, _last = parse_name(cec_name)
        prof = {
            "rmp_id": None,
            "rmp_url": None,
            "school": None,
            "first_name": _first,
            "middle_name": _middle,
            "last_name": _last,
            "departments": None,
            "avg_quality_rating": None, "avg_difficulty_rating": None,
            "rmp_rating_count": 0, "cec_eval_count": None,
            "cec_surveyed_count": None, "cec_enrolled_count": None,
            "avg_eval_median_weighted": None,
            **{f"avg_{col}": None for _, col in MEDIAN_QUESTIONS},
            "would_take_again_percent": None,
            "cec_courses": None,
            "updated_at": None,
            "grade_distribution": None, "rating_distribution": None,
            "difficulty_distribution": None, "rmp_courses": None,
            "is_online_percent": None, "attendance_is_mandatory_percent": None, "rating_tags_distribution": None,
            "title": None, "source": "cec",
        }
        professors.append(prof)
        cec_name_to_prof[cec_name] = prof
        new_profs += 1

    professors = [p for p in professors if id(p) not in merged_prof_ids]

    print(f"  Exact matches:  {exact_matches:,}")
    print(f"  Fuzzy matches:  {fuzzy_matches:,}")
    print(f"  Late merges:    {late_merges:,}")
    print(f"  New professors: {new_profs:,}")

    # ── STEP 4 & 5: CEC EVALUATIONS + TITLES ──
    print("\nBuilding CEC evaluations and computing titles...")

    title_tracker = {}
    cec_eval_counts = defaultdict(int)
    cec_surveyed_counts = defaultdict(int)
    cec_enrolled_counts = defaultdict(int)
    cec_course_data = defaultdict(lambda: defaultdict(set))
    cec_course_sections = defaultdict(lambda: defaultdict(int))
    cec_median_accum = defaultdict(lambda: defaultdict(list))  # instructor -> col -> [(val, surveyed)]
    cec_eval_avg_accum = defaultdict(list)                     # instructor -> [(eval_avg, surveyed)]
    cec_eval_instructors = []  # canonical names, used for professor_id linking
    cec_eval_rows = []
    for e in cec_evals_raw:
        raw_instructor = e["instructor_name"]
        canonical_instructor = cec_variant_to_canonical.get(raw_instructor, raw_instructor) if raw_instructor else None
        quarter, year = parse_quarter(e["quarter"])

        if canonical_instructor and e["title"]:
            key = quarter_sort_key(quarter, year)
            if key > title_tracker.get(canonical_instructor, (0, None))[0]:
                title_tracker[canonical_instructor] = (key, e["title"])

        if canonical_instructor:
            cec_eval_counts[canonical_instructor] += 1
            cec_surveyed_counts[canonical_instructor] += e["surveyed"] or 0
            cec_enrolled_counts[canonical_instructor] += e["enrolled"] or 0
            if e["course_code"] and quarter and year:
                cec_course_data[canonical_instructor][e["course_code"]].add((quarter, year))
                cec_course_sections[canonical_instructor][e["course_code"]] += 1

        surveyed = e["surveyed"] or 0
        eval_avg = _eval_avg_median(e["questions"])
        medians = [_get_median(e["questions"], q) for q, _ in MEDIAN_QUESTIONS]
        if canonical_instructor:
            if eval_avg is not None:
                cec_eval_avg_accum[canonical_instructor].append((eval_avg, surveyed))
            for (_, col), val in zip(MEDIAN_QUESTIONS, medians):
                if val is not None:
                    cec_median_accum[canonical_instructor][col].append((val, surveyed))
        cec_eval_instructors.append(canonical_instructor)
        cec_eval_rows.append((
            e["url"], e["course_name"], e["course_code"], e["section"],
            raw_instructor, e["title"], quarter, year, e["form_type"],
            e["surveyed"], e["enrolled"],
            _format_questions(e["questions"]),
            eval_avg,
            *medians,
        ))

    for instructor, (_, title) in title_tracker.items():
        prof = cec_name_to_prof.get(instructor)
        if prof is not None:
            prof["title"] = title

    # Aggregate cec_eval_count, surveyed/enrolled totals, and cec_courses per professor
    prof_cec_counts = defaultdict(int)
    prof_cec_surveyed = defaultdict(int)
    prof_cec_enrolled = defaultdict(int)
    prof_cec_courses = defaultdict(lambda: defaultdict(set))
    prof_cec_sections = defaultdict(lambda: defaultdict(int))
    prof_median_accum = defaultdict(lambda: defaultdict(list))  # pid -> col -> [(val, surveyed)]
    prof_eval_avg_accum = defaultdict(list)  # pid -> [(eval_avg, surveyed)]
    for instructor, prof in cec_name_to_prof.items():
        pid = id(prof)
        prof_cec_counts[pid] += cec_eval_counts.get(instructor, 0)
        prof_cec_surveyed[pid] += cec_surveyed_counts.get(instructor, 0)
        prof_cec_enrolled[pid] += cec_enrolled_counts.get(instructor, 0)
        for code, quarters in cec_course_data.get(instructor, {}).items():
            prof_cec_courses[pid][code].update(quarters)
            prof_cec_sections[pid][code] += cec_course_sections[instructor].get(code, 0)
        for col, pairs in cec_median_accum.get(instructor, {}).items():
            prof_median_accum[pid][col].extend(pairs)
        prof_eval_avg_accum[pid].extend(cec_eval_avg_accum.get(instructor, []))

    for prof in professors:
        pid = id(prof)
        count = prof_cec_counts.get(pid)
        prof["cec_eval_count"] = count if count else None
        prof["cec_surveyed_count"] = prof_cec_surveyed.get(pid) or None
        prof["cec_enrolled_count"] = prof_cec_enrolled.get(pid) or None
        courses = prof_cec_courses.get(pid, {})
        prof["cec_courses"] = json.dumps(sorted(
            [{"code": code,
              "quarters_count": len(quarters),
              "total_sections_count": prof_cec_sections[pid].get(code, 0),
              "quarters": sorted(f"{q} {y}" for q, y in quarters)}
             for code, quarters in courses.items()],
            key=lambda x: -x["quarters_count"]
        )) if courses else None
        prof["avg_eval_median_weighted"] = _wavg(prof_eval_avg_accum.get(pid, []))
        for _, col in MEDIAN_QUESTIONS:
            prof[f"avg_{col}"] = _wavg(prof_median_accum[pid].get(col, []))

    print(f"  Built {len(cec_eval_rows):,} evaluations")

    # ── WRITE PHASE ──
    print("\nWriting to database...")
    plain_cur = conn.cursor()

    plain_cur.execute("TRUNCATE professors, cec_evaluations, rmp_ratings RESTART IDENTITY CASCADE")

    # Insert combined school names
    combined_names = new_school_names - set(school_names.values())
    if combined_names:
        psycopg2.extras.execute_values(plain_cur,
            "INSERT INTO schools (rmp_id, name) VALUES %s ON CONFLICT (name) DO NOTHING",
            [(None, name) for name in combined_names]
        )

    # Insert professors
    returned = psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO professors
        (first_name, middle_name, last_name, title, school, departments,
         rmp_rating_count, cec_eval_count, cec_surveyed_count, cec_enrolled_count,
         avg_eval_median_weighted, avg_instructor_contribution_median_weighted, avg_instructor_effectiveness_median_weighted,
         avg_course_as_whole_median_weighted, avg_course_content_median_weighted, avg_amount_learned_median_weighted,
         avg_instructor_interest_median_weighted, avg_grading_techniques_median_weighted,
         rating_tags_distribution, avg_quality_rating, avg_difficulty_rating, would_take_again_percent,
         is_online_percent, attendance_is_mandatory_percent,
         grade_distribution, rating_distribution, difficulty_distribution,
         rmp_courses, rmp_url, cec_courses, updated_at, source)
        VALUES %s
        RETURNING id
    """, [(
        p["first_name"], p["middle_name"], p["last_name"], p["title"], p["school"],
        p["departments"], p["rmp_rating_count"], p["cec_eval_count"],
        p["cec_surveyed_count"], p["cec_enrolled_count"], p["avg_eval_median_weighted"],
        p["avg_instructor_contribution_median_weighted"], p["avg_instructor_effectiveness_median_weighted"],
        p["avg_course_as_whole_median_weighted"], p["avg_course_content_median_weighted"], p["avg_amount_learned_median_weighted"],
        p["avg_instructor_interest_median_weighted"], p["avg_grading_techniques_median_weighted"],
        p["rating_tags_distribution"],
        p["avg_quality_rating"], p["avg_difficulty_rating"], p["would_take_again_percent"],
        p["is_online_percent"], p["attendance_is_mandatory_percent"],
        p["grade_distribution"], p["rating_distribution"], p["difficulty_distribution"],
        p["rmp_courses"], p["rmp_url"], p["cec_courses"], p["updated_at"], p["source"],
    ) for p in professors], fetch=True)

    # Map each prof dict to its serial id by insertion order
    prof_to_serial = {id(p): row[0] for p, row in zip(professors, returned)}

    # Map each CEC instructor name to its professor's serial id
    cec_name_to_serial = {
        cec_name: sid
        for cec_name, prof in cec_name_to_prof.items()
        if (sid := prof_to_serial.get(id(prof)))
    }

    print(f"  Inserted {len(professors):,} professors")

    # Insert CEC evaluations with resolved professor_ids
    resolved_evals = [
        (cec_name_to_serial.get(inst) if inst else None,) + row
        for inst, row in zip(cec_eval_instructors, cec_eval_rows)
    ]

    psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO cec_evaluations
        (professor_id, url, course_name, course_code, section, instructor_name, title,
         quarter, year, form_type, surveyed, enrolled, questions,
         eval_avg_median, instructor_contribution_median, instructor_effectiveness_median,
         course_as_whole_median, course_content_median, amount_learned_median,
         instructor_interest_median, grading_techniques_median)
        VALUES %s
        ON CONFLICT (url) DO UPDATE SET
            professor_id = EXCLUDED.professor_id,
            quarter = EXCLUDED.quarter,
            year = EXCLUDED.year
    """, resolved_evals)

    # Insert RMP ratings with resolved professor_ids
    rmp_rating_rows = []
    for prof in professors:
        if not prof["rmp_id"]:
            continue
        sid = prof_to_serial.get(id(prof))
        if not sid:
            continue
        for r in get_all_ratings(prof["rmp_id"]):
            att = r.get("attendance_mandatory")
            att_bool = True if att in ("mandatory", "Y") else (False if att in ("non mandatory", "N") else None)
            tags_raw = r.get("rating_tags")
            tags = [normalize_tag(t) for t in tags_raw.split("--") if t.strip()] if tags_raw else None
            rmp_rating_rows.append((
                r["id"], sid, normalize_course(r.get("class")), r["class"], r["date"], r["comment"],
                r["quality_rating"], r["difficulty_rating"],
                (GRADE_ALIASES.get(r["grade"], r["grade"]) or None), r["would_take_again"], r["is_online"],
                att_bool, r.get("textbook_used"), tags,
            ))

    psycopg2.extras.execute_values(plain_cur, """
        INSERT INTO rmp_ratings
        (id, professor_id, class, rmp_class, date, comment, quality_rating,
         difficulty_rating, grade, would_take_again, is_online,
         attendance_is_mandatory, textbook_used, rating_tags)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """, rmp_rating_rows)

    conn.commit()
    plain_cur.close()

    # ── SUMMARY ──
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) FROM professors")
    total = cur.fetchone()["count"]
    cur.execute("SELECT source, COUNT(*) FROM professors GROUP BY source ORDER BY source")
    by_source = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM cec_evaluations WHERE professor_id IS NOT NULL")
    linked = cur.fetchone()["count"]
    cur.close()

    titles_updated = sum(1 for p in professors if p["title"])

    print(f"\nDone.")
    print(f"  Total professors: {total:,}")
    for row in by_source:
        print(f"    {row['source']}: {row['count']:,}")
    print(f"  CEC evaluations linked: {linked:,}")
    print(f"  Titles set: {titles_updated:,}")

    conn.close()


if __name__ == "__main__":
    main()
