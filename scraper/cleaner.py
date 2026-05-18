import html
import json
import os
import re
import psycopg2
import psycopg2.extras
from collections import defaultdict
from nameparser import HumanName

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
FUZZY_THRESHOLD = 0.7


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


def normalize_course(raw):
    if not raw:
        return None
    s = raw.strip().upper().replace(' ', '')
    m = re.fullmatch(r'([A-Z&/]{2,6})(\d{3})[A-Z]?', s)
    return m.group(1) + m.group(2) if m else None


def norm_name(first, middle, last):
    return ' '.join(((first or '') + ' ' + (middle or '') + ' ' + (last or '')).strip().lower().split())


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


def init_db(conn):
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id SERIAL PRIMARY KEY,
            rmp_id TEXT UNIQUE,
            first_name TEXT,
            middle_name TEXT,
            last_name TEXT,
            school TEXT,
            departments JSONB,
            avg_rating REAL,
            avg_difficulty REAL,
            num_ratings INTEGER,
            would_take_again REAL,
            updated_at TIMESTAMP,
            grade_distribution JSONB,
            rating_distribution JSONB,
            difficulty_distribution JSONB,
            rmp_courses JSONB,
            is_online_percent REAL,
            attendance_is_mandatory_percent REAL,
            rating_tags_distribution JSONB,
            title TEXT,
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
            questions JSONB
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
        name_to_profs[norm_name(p["first_name"], None, p["last_name"])].append(p)

    loser_ids = set()
    winner_overrides = {}
    winner_to_all_rmp_ids = {}
    new_school_names = set()
    same_school_groups = []
    cross_campus_groups = []

    for name, profs in name_to_profs.items():
        if len(profs) <= 1:
            continue
        profs = sorted(profs, key=lambda p: p["num_ratings"] or 0, reverse=True)
        winner = profs[0]

        school_ids = [p["school_id"] for p in profs]
        if len(set(school_ids)) == 1:
            same_school_groups.append(profs)
        else:
            cross_campus_groups.append(profs)

        for loser in profs[1:]:
            loser_ids.add(loser["id"])

        winner_to_all_rmp_ids[winner["id"]] = [p["id"] for p in profs]

        num_ratings_list = [p["num_ratings"] for p in profs]
        school = combined_school_name(school_ids, school_names)
        new_school_names.add(school)
        unique_depts = list(dict.fromkeys(normalize_dept(p["department"]) for p in profs if p["department"]))
        unique_depts = [d for d in unique_depts if d]

        winner_overrides[winner["id"]] = {
            "avg_rating":         weighted([p["avg_rating"] for p in profs], num_ratings_list),
            "avg_difficulty":     weighted([p["avg_difficulty"] for p in profs], num_ratings_list),
            "would_take_again":   weighted([p["would_take_again"] for p in profs], num_ratings_list),
            "num_ratings":        sum(n for n in num_ratings_list if n),
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
            "school":                 ov.get("school", school_names.get(p["school_id"])),
            "first_name":             _first,
            "middle_name":            _middle,
            "last_name":              _last,
            "departments":            ov.get("departments", json.dumps([normalize_dept(p["department"])] if normalize_dept(p["department"]) else [])),
            "avg_rating":             ov.get("avg_rating", p["avg_rating"]),
            "avg_difficulty":         ov.get("avg_difficulty", p["avg_difficulty"]),
            "num_ratings":            ov.get("num_ratings", p["num_ratings"]),
            "would_take_again":       ov.get("would_take_again", p["would_take_again"]),
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

    cec_names = list({e["instructor_name"] for e in cec_evals_raw if e["instructor_name"]})
    cec_name_to_prof = {}
    merged_prof_ids = set()
    exact_matches = fuzzy_matches = late_merges = new_profs = 0

    for cec_name in cec_names:
        normalized = ' '.join(cec_name.strip().lower().split())

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
                num_ratings_list = [p["num_ratings"] for p in all_profs]
                winner_prof.update({
                    "avg_rating":             weighted([p["avg_rating"] for p in all_profs], num_ratings_list),
                    "avg_difficulty":          weighted([p["avg_difficulty"] for p in all_profs], num_ratings_list),
                    "would_take_again":        weighted([p["would_take_again"] for p in all_profs], num_ratings_list),
                    "num_ratings":             sum(n for n in num_ratings_list if n),
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

        # No match — new CEC-only professor
        _first, _middle, _last = parse_name(cec_name)
        prof = {
            "rmp_id": None,
            "school": None,
            "first_name": _first,
            "middle_name": _middle,
            "last_name": _last,
            "departments": None,
            "avg_rating": None, "avg_difficulty": None,
            "num_ratings": 0, "would_take_again": None,
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

    title_tracker = {}  # instructor_name -> (sort_key, title_text)
    cec_eval_instructors = []
    cec_eval_rows = []
    for e in cec_evals_raw:
        instructor = e["instructor_name"]
        quarter, year = parse_quarter(e["quarter"])

        if instructor and e["title"]:
            key = quarter_sort_key(quarter, year)
            if key > title_tracker.get(instructor, (0, None))[0]:
                title_tracker[instructor] = (key, e["title"])

        cec_eval_instructors.append(instructor)
        cec_eval_rows.append((
            e["url"], e["course_name"], e["course_code"], e["section"],
            instructor, e["title"], quarter, year, e["form_type"],
            e["surveyed"], e["enrolled"],
            json.dumps(e["questions"]) if isinstance(e["questions"], dict) else e["questions"],
        ))

    for instructor, (_, title) in title_tracker.items():
        prof = cec_name_to_prof.get(instructor)
        if prof is not None:
            prof["title"] = title

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
        (rmp_id, first_name, middle_name, last_name, school, departments, avg_rating, avg_difficulty,
         num_ratings, would_take_again, updated_at, grade_distribution, rating_distribution,
         difficulty_distribution, rmp_courses, is_online_percent, attendance_is_mandatory_percent,
         rating_tags_distribution, title, source)
        VALUES %s
        RETURNING id
    """, [(
        p["rmp_id"], p["first_name"], p["middle_name"], p["last_name"], p["school"],
        p["departments"], p["avg_rating"], p["avg_difficulty"],
        p["num_ratings"], p["would_take_again"], p["updated_at"],
        p["grade_distribution"], p["rating_distribution"],
        p["difficulty_distribution"], p["rmp_courses"],
        p["is_online_percent"], p["attendance_is_mandatory_percent"], p["rating_tags_distribution"],
        p["title"], p["source"],
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
         quarter, year, form_type, surveyed, enrolled, questions)
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
