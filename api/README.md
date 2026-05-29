# API

FastAPI backend that serves professor data to the Chrome extension. Handles Microsoft (Azure AD) OAuth code exchange, issues HS256 JWTs, and queries a PostgreSQL connection pool.

Hosted on Railway, proxied through Cloudflare at `api.ratemydawg.com`.

## Environment variables

```
DATABASE_URL          PostgreSQL connection string
AZURE_CLIENT_ID       Azure AD app client ID
AZURE_TENANT_ID       Azure AD tenant (e.g. uw.edu)
AZURE_KEY_PATH        Path to private key PEM file  (use this or AZURE_PRIVATE_KEY)
AZURE_PRIVATE_KEY     Raw private key PEM content   (use this or AZURE_KEY_PATH)
AZURE_CERT_PATH       Path to certificate PEM file  (use this or AZURE_CERT)
AZURE_CERT            Raw certificate PEM content   (use this or AZURE_CERT_PATH)
JWT_SECRET            Secret for signing JWTs
TRUSTED_PROXY=1       Set this on Railway — enables CF-Connecting-IP for rate limiting
CORS_ORIGINS          Comma-separated allowed origins (default: https://myplan.uw.edu)
CORS_ORIGIN_REGEX     Regex for allowed origins (default: chrome-extension://.*)
```

## Local dev

```bash
cd api
source .venv/bin/activate
uvicorn main:app --reload
# http://localhost:8000
# http://localhost:8000/docs  ← interactive API docs
```

## One-time DB setup

Run once against the production database (Railway query tab or psql):

```sql
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text AS
  $func$SELECT public.unaccent('public.unaccent'::regdictionary, $1)$func$
LANGUAGE sql IMMUTABLE STRICT;

CREATE INDEX IF NOT EXISTS idx_professors_name
  ON professors (f_unaccent(lower(last_name)), f_unaccent(lower(first_name)));
```

## Deployment

Deploys automatically from `main` via Railway. Add `TRUSTED_PROXY=1` to Railway environment variables so the rate limiter reads real client IPs from Cloudflare's `CF-Connecting-IP` header.
