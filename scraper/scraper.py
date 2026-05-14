import requests
import psycopg2
import psycopg2.extras
import time

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.ratemyprofessors.com/",
    "Origin": "https://www.ratemyprofessors.com",
}
SCHOOLS = [
    {"id": "U2Nob29sLTE1MzA=",  "name": "UW Seattle"},
    {"id": "U2Nob29sLTQ0NjY=",  "name": "UW Bothell"},
    {"id": "U2Nob29sLTQ3NDQ=",  "name": "UW Tacoma"},
]
PROF_BATCH_SIZE = 1000
RATINGS_BATCH_SIZE = 1000
MAX_RETRIES = 3
RETRY_DELAY = 2
DB_URL = "postgresql://localhost/uw_professors"

def init_db(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schools (
            id TEXT PRIMARY KEY,
            name TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS professors (
            id TEXT PRIMARY KEY,
            school_id TEXT,
            first_name TEXT,
            last_name TEXT,
            department TEXT,
            avg_rating REAL,
            avg_difficulty REAL,
            num_ratings INTEGER,
            would_take_again REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (school_id) REFERENCES schools(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id TEXT PRIMARY KEY,
            professor_id TEXT,
            class TEXT,
            date TEXT,
            comment TEXT,
            clarity_rating INTEGER,
            helpful_rating INTEGER,
            difficulty_rating INTEGER,
            grade TEXT,
            would_take_again INTEGER,
            is_online BOOLEAN,
            FOREIGN KEY (professor_id) REFERENCES professors(id)
        )
    """)
    conn.commit()
    cur.close()

def post_with_retry(query):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.post(GRAPHQL_URL, headers=HEADERS, json={"query": query})
            if res.status_code == 200 and res.text:
                return res.json()
            print(f"    Attempt {attempt} failed (status {res.status_code}), retrying in {RETRY_DELAY}s...")
        except Exception as e:
            print(f"    Attempt {attempt} error: {e}, retrying in {RETRY_DELAY}s...")
        time.sleep(RETRY_DELAY)
    print(f"    All {MAX_RETRIES} attempts failed, skipping batch")
    return None

def fetch_professors_page(school_id, cursor=None):
    after = f', after: "{cursor}"' if cursor else ""
    data = post_with_retry(f"""
    query {{
        newSearch {{
            teachers(query: {{ schoolID: "{school_id}" }}, first: {PROF_BATCH_SIZE}{after}) {{
                edges {{
                    node {{
                        id firstName lastName department
                        avgRating avgDifficulty numRatings wouldTakeAgainPercent
                    }}
                }}
                pageInfo {{ hasNextPage endCursor }}
            }}
        }}
    }}
    """)
    return data["data"]["newSearch"]["teachers"] if data else None

def fetch_ratings_batch(professor_ids):
    aliases = "\n".join([
        f"""p{i}: node(id: "{pid}") {{
            ... on Teacher {{
                ratings(first: 1000) {{
                    edges {{
                        node {{
                            id class date comment
                            clarityRating helpfulRating difficultyRating
                            grade wouldTakeAgain isForOnlineClass
                        }}
                    }}
                }}
            }}
        }}"""
        for i, pid in enumerate(professor_ids)
    ])
    return post_with_retry(f"query {{ {aliases} }}")

# --- Main ---
conn = psycopg2.connect(DB_URL)
init_db(conn)

for school in SCHOOLS:
    print(f"\n{'='*50}")
    print(f"School: {school['name']}")
    print(f"{'='*50}")

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO schools (id, name) VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
    """, (school["id"], school["name"]))
    conn.commit()
    cur.close()

    # Step 1: fetch all professors
    print("Step 1: Fetching professor info...")
    professors = []
    cursor = None
    page = 1
    while True:
        data = fetch_professors_page(school["id"], cursor)
        if not data:
            print("  Failed to fetch page, aborting")
            break
        batch = [e["node"] for e in data["edges"]]
        professors.extend(batch)
        print(f"  Page {page}: {len(batch)} professors (total: {len(professors)})")
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
        page += 1
        time.sleep(0.5)

    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO professors (id, school_id, first_name, last_name, department, avg_rating, avg_difficulty, num_ratings, would_take_again)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            avg_rating = EXCLUDED.avg_rating,
            avg_difficulty = EXCLUDED.avg_difficulty,
            num_ratings = EXCLUDED.num_ratings,
            would_take_again = EXCLUDED.would_take_again,
            updated_at = CURRENT_TIMESTAMP
    """, [(
        p["id"], school["id"], p["firstName"], p["lastName"],
        p["department"], p["avgRating"], p["avgDifficulty"],
        p["numRatings"], p["wouldTakeAgainPercent"],
    ) for p in professors])
    conn.commit()
    cur.close()
    print(f"  Saved {len(professors)} professors")

    # Step 2: fetch all ratings
    print("Step 2: Fetching ratings...")
    ids = [p["id"] for p in professors if p["numRatings"] > 0]
    total_ratings = 0
    total_batches = (len(ids) + RATINGS_BATCH_SIZE - 1) // RATINGS_BATCH_SIZE

    for i in range(0, len(ids), RATINGS_BATCH_SIZE):
        batch_ids = ids[i:i + RATINGS_BATCH_SIZE]
        batch_num = (i // RATINGS_BATCH_SIZE) + 1

        start = time.time()
        data = fetch_ratings_batch(batch_ids)
        elapsed = time.time() - start

        if not data:
            print(f"  Batch {batch_num}/{total_batches}: SKIPPED after retries")
            continue

        ratings = []
        for key, val in data.get("data", {}).items():
            if not val or "ratings" not in val:
                continue
            prof_id = batch_ids[int(key[1:])]
            for e in val["ratings"]["edges"]:
                n = e["node"]
                ratings.append((
                    n["id"], prof_id, n["class"], n["date"], n["comment"],
                    n["clarityRating"], n["helpfulRating"], n["difficultyRating"],
                    n["grade"], n["wouldTakeAgain"], n["isForOnlineClass"],
                ))

        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ratings
            (id, professor_id, class, date, comment, clarity_rating, helpful_rating,
             difficulty_rating, grade, would_take_again, is_online)
            VALUES %s
            ON CONFLICT (id) DO NOTHING
        """, ratings)
        conn.commit()
        cur.close()
        total_ratings += len(ratings)
        print(f"  Batch {batch_num}/{total_batches}: {len(ratings)} ratings in {elapsed:.2f}s (total: {total_ratings})")
        time.sleep(0.5)

    print(f"  Done: {len(professors)} professors, {total_ratings} ratings")

conn.close()
print(f"\nAll schools complete!")