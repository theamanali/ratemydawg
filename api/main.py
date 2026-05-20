import os
import re
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
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from nameparser import HumanName
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()

AZURE_CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
AZURE_TENANT_ID = os.environ["AZURE_TENANT_ID"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 365

from cryptography.hazmat.primitives.serialization import Encoding

# Support both file path and raw PEM content env vars
_key_path = os.environ.get("AZURE_KEY_PATH")
AZURE_PRIVATE_KEY = open(_key_path, "rb").read() if _key_path else os.environ["AZURE_PRIVATE_KEY"].encode()

_cert_path = os.environ.get("AZURE_CERT_PATH")
_cert_pem = open(_cert_path, "rb").read() if _cert_path else os.environ["AZURE_CERT"].encode()
_cert = x509.load_pem_x509_certificate(_cert_pem)
AZURE_THUMBPRINT = hashlib.sha1(_cert.public_bytes(Encoding.DER)).hexdigest()

_msal_app = msal.ConfidentialClientApplication(
    client_id=AZURE_CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
    client_credential={"private_key": AZURE_PRIVATE_KEY, "thumbprint": AZURE_THUMBPRINT},
)


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
app = FastAPI(title="RateMyDawg API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool = None


def get_pool():
    global _pool
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
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()
    finally:
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


@app.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
def auth_login(request: Request, body: LoginRequest):
    result = _msal_app.acquire_token_by_authorization_code(
        code=body.code,
        scopes=["email"],
        redirect_uri=body.redirect_uri,
    )
    if "error" in result:
        print("MSAL error:", result.get("error"), result.get("error_description"), result.get("error_codes"))
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


def _match_one(name: str) -> list:
    first, middle, last = parse_name(name)
    if not first or not last:
        return []

    base_filters = ["unaccent(lower(first_name)) = %s", "unaccent(lower(last_name)) = %s"]
    base_params = [norm(first), norm(last)]

    def fetch(extra_filters=None, extra_params=None):
        filters = base_filters + (extra_filters or [])
        params = base_params + (extra_params or [])
        with db_cursor() as cur:
            cur.execute(
                f"SELECT * FROM professors WHERE {' AND '.join(filters)} ORDER BY rmp_rating_count DESC NULLS LAST",
                params,
            )
            return cur.fetchall()

    if middle:
        results = fetch()
        if len(results) <= 1:
            return results
        query_middle = norm(middle)
        for char_count in range(1, len(query_middle) + 1):
            prefix = query_middle[:char_count]
            filtered = [r for r in results if r["middle_name"] and norm(r["middle_name"]).startswith(prefix)]
            if len(filtered) == 1:
                return filtered
            if filtered:
                results = filtered
        return results
    else:
        results = fetch(["middle_name IS NULL"])
        return results if results else fetch()


class BatchMatchRequest(BaseModel):
    names: list[str]


@app.post("/professors/match/batch", tags=["Professors"])
@limiter.limit("30/minute")
def match_professors_batch(request: Request, body: BatchMatchRequest):
    authenticated = verify_jwt(request) is not None
    results = {}
    for name in body.names:
        matches = [dict(m) for m in _match_one(name)]
        if not authenticated:
            for prof in matches:
                for field in [
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
                ]:
                    prof[field] = None
                prof["cec_locked"] = True
        results[name] = matches
    return results


@app.get("/professors/match", tags=["Professors"])
@limiter.limit("30/minute")
def match_professors(
    request: Request,
    name: str = Query(..., min_length=2),
):
    first, middle, last = parse_name(name)
    if not first or not last:
        raise HTTPException(status_code=400, detail="Could not parse a first and last name from the provided name")

    return _match_one(name)
