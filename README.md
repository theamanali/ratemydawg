# RateMyDawg

RateMyDawg is a Chrome extension that injects professor rating badges directly into the UW MyPlan course registration page. Each instructor row gets inline RateMyProfessors scores (Quality, Difficulty, Would Take Again) alongside UW Course Evaluation Scores — unlocked by signing in with a UW account.

## Architecture

```
UW CEC / RateMyProfessors
      ↓
  pipeline/        scrapes & cleans professor data → PostgreSQL
      ↓
    api/           FastAPI · hosted on Railway · proxied via Cloudflare
      ↓
  extension/       Plasmo Chrome MV3 · injects badges into MyPlan
```

## Prerequisites

- Python 3.11+
- Node.js 18+ and [pnpm](https://pnpm.io)
- `psql` (for DB setup)

## Shared environment variable

All three layers read `DATABASE_URL` from the root `.env`:

```
DATABASE_URL=postgresql://user:password@host:port/dbname
```

Copy `.env.example` to `.env` and fill in values before running anything.

## Subprojects

- [api/](api/) — FastAPI backend, MSAL auth, JWT, rate limiting
- [pipeline/](pipeline/) — RMP + CEC scrapers and data cleaner
- [extension/](extension/) — Chrome extension
