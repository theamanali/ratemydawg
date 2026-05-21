import logging
import os
import re
import threading
import time
import unicodedata
import psycopg2
import psycopg2.extras
import psycopg2.pool
import hashlib
import jwt
import msal
import requests as http_requests
from cryptography import x509
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from nameparser import HumanName
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

load_dotenv()

AZURE_CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
AZURE_TENANT_ID = os.environ["AZURE_TENANT_ID"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30

from cryptography.hazmat.primitives.serialization import Encoding

# Support both file path and raw PEM content env vars
_key_path = os.environ.get("AZURE_KEY_PATH")
if _key_path:
    with open(_key_path, "rb") as _f:
        AZURE_PRIVATE_KEY = _f.read()
else:
    AZURE_PRIVATE_KEY = os.environ["AZURE_PRIVATE_KEY"].encode()

_cert_path = os.environ.get("AZURE_CERT_PATH")
if _cert_path:
    with open(_cert_path, "rb") as _f:
        _cert_pem = _f.read()
else:
    _cert_pem = os.environ["AZURE_CERT"].encode()
_cert = x509.load_pem_x509_certificate(_cert_pem)
AZURE_THUMBPRINT = hashlib.sha1(_cert.public_bytes(Encoding.DER)).hexdigest()

_msal_app = msal.ConfidentialClientApplication(
    client_id=AZURE_CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
    client_credential={"private_key": AZURE_PRIVATE_KEY, "thumbprint": AZURE_THUMBPRINT},
)


_TRUSTED_PROXY = os.environ.get("TRUSTED_PROXY", "").strip()


def get_client_ip(request: Request) -> str:
    if _TRUSTED_PROXY:
        # CF-Connecting-IP is set by Cloudflare and cannot be spoofed by the client
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
app = FastAPI(title="RateMyDawg API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "https://myplan.uw.edu").split(",") if o.strip()]
# chrome-extension:// origins (ID varies per install) are allowed for the background auth flow
_CORS_ORIGIN_REGEX = os.environ.get("CORS_ORIGIN_REGEX", r"chrome-extension://.*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=50,
                    dsn=os.environ["DATABASE_URL"],
                    sslmode="require",
                )
    return _pool


@contextmanager
def db_cursor():
    pool = get_pool()
    conn = pool.getconn()
    ok = False
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            ok = True
        finally:
            cur.close()
    finally:
        if not ok:
            conn.rollback()
        pool.putconn(conn)


def normalize_initials(s):
    s = re.sub(r'([A-Za-z]\.)(?=[A-Za-z])', r'\1 ', s)
    parts = s.split()
    return ' '.join(p.rstrip('.') + '.' if re.fullmatch(r'[A-Za-z]\.?', p) else p for p in parts)


def parse_name(full_name):
    n = HumanName(full_name or "")
    middle = normalize_initials(n.middle) if n.middle else None
    return n.first or None, middle, n.last or None


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFD', (s or "").lower().strip()) if unicodedata.category(c) != 'Mn')


class LoginRequest(BaseModel):
    code: str
    redirect_uri: str


def verify_jwt(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


@app.get("/privacy", tags=["Meta"], response_class=HTMLResponse, include_in_schema=False)
def privacy():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — RateMyDawg</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 680px; margin: 60px auto; padding: 0 24px; color: #1a1a2e; line-height: 1.7; }
  h1 { font-size: 1.8rem; margin-bottom: 4px; }
  h2 { font-size: 1.1rem; margin-top: 2rem; color: #4b2e83; }
  p, li { color: #444; }
  a { color: #4b2e83; }
  .updated { color: #888; font-size: 0.9rem; margin-bottom: 2rem; }
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p class="updated">Last updated: May 2026</p>

<p>RateMyDawg is a Chrome extension that displays professor ratings from RateMyProfessors and UW Course Evaluation Scores directly in the UW MyPlan course registration page.</p>

<h2>What we collect</h2>
<p>When you sign in with your UW account, we receive your UW email address and display name from Microsoft via OAuth. These are stored as a signed JWT token in your browser's local extension storage (<code>chrome.storage.local</code>). No data is stored on any server.</p>

<h2>What we do not collect</h2>
<ul>
  <li>Browsing history</li>
  <li>Course selections or registration activity</li>
  <li>Analytics or usage telemetry</li>
  <li>Any data beyond your email and display name</li>
</ul>

<h2>How your data is used</h2>
<p>Your UW email address is used solely to verify you are a UW student and unlock UW Course Evaluation Scores. It is never shared with third parties, sold, or used for any other purpose.</p>

<h2>Data retention</h2>
<p>Your JWT token is stored locally in your browser and expires after 30 days. You can remove it at any time by clicking Sign Out in the extension popup. Uninstalling the extension removes all stored data.</p>

<h2>Third-party services</h2>
<p>Rating data is fetched from <a href="https://www.ratemyprofessors.com">RateMyProfessors</a> and the <a href="https://www.washington.edu/cec">UW Course Evaluations Catalog</a>. No personal information is sent to either service.</p>

<h2>Contact</h2>
<p>Questions or concerns: <a href="mailto:hello@ratemydawg.com">hello@ratemydawg.com</a></p>
</body>
</html>""")


@app.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
def auth_login(request: Request, body: LoginRequest):
    result = _msal_app.acquire_token_by_authorization_code(
        code=body.code,
        scopes=["email"],
        redirect_uri=body.redirect_uri,
    )
    if "error" in result:
        logger.warning("MSAL error: %s %s %s", result.get("error"), result.get("error_description"), result.get("error_codes"))
        raise HTTPException(status_code=401, detail=result.get("error_description", "Failed to exchange code"))

    id_claims = result.get("id_token_claims", {})
    email = id_claims.get("email") or id_claims.get("preferred_username", "")

    if not email.lower().endswith("@uw.edu"):
        raise HTTPException(status_code=403, detail="Must sign in with a UW email address")

    name = id_claims.get("name", email)

    token = jwt.encode({
        "email": email,
        "name": name,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }, JWT_SECRET, algorithm=JWT_ALGORITHM)

    return {"token": token}


@app.post("/auth/refresh", tags=["Auth"])
@limiter.limit("30/minute")
def auth_refresh(request: Request):
    claims = verify_jwt(request)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    token = jwt.encode(
        {"email": claims["email"], "name": claims["name"],
         "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS)},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )
    return {"token": token}


@app.get("/health", tags=["Meta"])
@limiter.limit("30/minute")
def health(request: Request):
    start = time.monotonic()
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "db_latency_ms": round((time.monotonic() - start) * 1000, 2),
    }


def _batch_match(names: list[str]) -> dict[str, list]:
    parsed: dict[str, tuple] = {}
    for name in names:
        first, middle, last = parse_name(name)
        if first and last:
            parsed[name] = (first, middle, last)

    if not parsed:
        return {name: [] for name in names}

    pairs = list({(norm(first), norm(last)) for first, _, last in parsed.values()})

    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM professors"
            " WHERE (f_unaccent(lower(first_name)), f_unaccent(lower(last_name))) IN %s"
            " ORDER BY rmp_rating_count DESC NULLS LAST",
            (tuple(pairs),),
        )
        rows = cur.fetchall()

    by_key: dict[tuple, list] = {}
    for row in rows:
        key = (norm(row["first_name"] or ""), norm(row["last_name"] or ""))
        by_key.setdefault(key, []).append(row)

    results: dict[str, list] = {}
    for name in names:
        if name not in parsed:
            results[name] = []
            continue
        first, middle, last = parsed[name]
        candidates = list(by_key.get((norm(first), norm(last)), []))
        if not middle:
            no_mid = [r for r in candidates if not r["middle_name"]]
            results[name] = no_mid if no_mid else candidates
        elif len(candidates) <= 1:
            results[name] = candidates
        else:
            query_middle = norm(middle)
            filtered = candidates
            for i in range(1, len(query_middle) + 1):
                prefix = query_middle[:i]
                narrowed = [r for r in filtered if r["middle_name"] and norm(r["middle_name"]).startswith(prefix)]
                if len(narrowed) == 1:
                    filtered = narrowed
                    break
                if narrowed:
                    filtered = narrowed
            results[name] = filtered

    return results


_CEC_MASKED_FIELDS = [
    "avg_eval_median_weighted",
    "avg_instructor_contribution_median_weighted",
    "avg_instructor_effectiveness_median_weighted",
    "avg_course_as_whole_median_weighted",
    "avg_course_content_median_weighted",
    "avg_amount_learned_median_weighted",
    "avg_instructor_interest_median_weighted",
    "avg_grading_techniques_median_weighted",
    "cec_eval_count",
    "cec_surveyed_count",
    "cec_enrolled_count",
    "cec_courses",
]


class BatchMatchRequest(BaseModel):
    names: list[str]


@app.post("/professors/match/batch", tags=["Professors"])
@limiter.limit("30/minute")
def match_professors_batch(request: Request, body: BatchMatchRequest):
    authenticated = verify_jwt(request) is not None
    raw = _batch_match(body.names)
    results = {}
    for name, matches in raw.items():
        profs = [dict(m) for m in matches]
        if not authenticated:
            for prof in profs:
                for field in _CEC_MASKED_FIELDS:
                    prof[field] = None
                prof["cec_locked"] = True
        results[name] = profs
    return results
