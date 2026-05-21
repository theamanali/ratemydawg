# RateMyDawg Extension

Plasmo Chrome MV3 extension that injects professor rating badges into the UW MyPlan course registration page. Each instructor gets inline RateMyProfessors scores (Quality, Difficulty, Would Take Again) alongside UW Course Evaluation Scores. CES scores are gated behind UW sign-in via Microsoft OAuth.

## Setup

```bash
cd extension
pnpm install
```

## Environment files

Create the appropriate `.env` file before running or building:

```
# .env.development  (for pnpm dev — hits local API)
PLASMO_PUBLIC_API_BASE=http://localhost:8000

# .env.production  (for pnpm build:prod — hits production API)
PLASMO_PUBLIC_API_BASE=https://api.ratemydawg.com
```

## Dev

```bash
pnpm dev
```

Load `build/chrome-mv3-dev` in `chrome://extensions` (enable Developer mode → Load unpacked).

Requires the local API server running at `http://localhost:8000` — see [api/README.md](../api/README.md).

## Production build

Strips dev host permissions (`localhost`), builds, and zips for store submission:

```bash
pnpm build:prod
# output: build/chrome-mv3-prod.zip
```

## Standard build

Hits the production API (`api.ratemydawg.com`), localhost permission included:

```bash
pnpm build
# output: build/chrome-mv3-dev
```
