# BetWatch — News, Video & Social Intelligence for Betting, Gaming and Regulation

BetWatch collects coverage of betting, online gaming and betting law from Indian and international outlets, YouTube news channels, trade publications and social platforms. It groups posts into **stories**, identifies **who reported first**, tracks **channels, journalists, tone (good/bad news) and counts**, and ranks media channels for **acquisition intelligence**.

```
┌──────────── worker (python -m app.worker) ────────────┐        ┌──────── api (uvicorn app.main:app) ────────┐
│ every 60 s: run due sources (per-source interval,       │        │ /api/dashboard  ← unified dashboard data   │
│ exponential backoff on failures, per-provider pacing)  │        │ /api/stories/{id}  ← story details         │
│   adapters: rss · youtube_channel · google_news ·       │  SQL   │ /api/channels/{id} ← channel profile       │
│             gdelt · youtube_search · reddit · bluesky  ├───────►│ /api/reports/*.pdf  ← ReportLab PDF export│
│   pipeline: canonical URL + fingerprint dedup →         │ Postgres│ /api/keywords     ← tracked search terms  │
│     relevance gate → categories → tone → live flag →    │ (SQLite │ /api/stream       (SSE live update push) │
│     subject links → story clustering                    │  in dev)│ /  dashboard  /channel/{id}  /story/{id}   │
│   enrichment: Google link → publisher URL → byline      │        │ /admin  admin console   /docs  OpenAPI     │
│   summaries: AI (optional) or labelled publisher text   │        └────────────────────────────────────────────┘
└────────────────────────────────────────────────────────┘
```

---

## What's New in this Version

1. **Ranking Bug Fix & Channel Accuracy (Single Source of Truth)**:
   - Fixed the ranking post-count mismatch (e.g. clicking iGaming Business previously showed 0 posts due to active filters).
   - Post-level `matching_posts()` query acts as the single source of truth across both the acquisition rankings and the channel detail view.
   - Ranking cards supply explicit filter navigation parameters (`goto`) and display an explanation message with a "Reset filters" button if a channel has no posts under a specific filter.
2. **Acquisition Panel Reorder**:
   - Reordered acquisition rail:
     1. **Most Mahadev betting posts**
     2. **Most betting news posts**
   - Removed unbacked placeholder "Most views on betting news" card.
   - Added direct **"View channel"** links for each ranked outlet leading to its dossier.
3. **Story Hover Preview & Dedicated Story Page**:
   - 150ms close-delay popover card preview on hovering any feed row.
   - Dedicated Story Details page at `/story/{story_id}` featuring full executive summary, highlights, covering channel chips linking to dossiers, and tabbed posts archive (Websites, Video, Social).
4. **Saved Keyword Searches with Auto-Collection**:
   - `POST /api/keywords`: Save and automatically trigger immediate collection for arbitrary search keywords (e.g. "Parimatch", "Tiger Exchange").
   - Sidebar displays live status badges: `Collecting…` (yellow), `Ready (N posts)` (green), or `Failed` (red) with edit, pause, and delete controls.
5. **Channel Profile & Acquisition Dossiers (`/channel/{outlet_id}`)**:
   - **Wikidata Corporate Identity**: Enriched automatically via Wikidata SPARQL API (P571 Founded, P127 Parent org, P159 HQ, P856 Website, P414/P249 Ticker, P2002/P2003/P2013 Social handles).
   - **Audience & Reach (NO FAKE DATA Golden Rule)**: Real metrics for YouTube, Instagram, X/Twitter, Web Traffic, and Market Cap. If an API key is unconfigured, displays clear *"Not available: needs [KEY_NAME]"* badge with verification source.
   - **Track Record & Regulatory History**: Legal disputes, court rulings, and Google Fact Check Tools API results.
   - **Admin Management**: `/admin` interface to add/delete manual legal facts and sync/override Wikidata QIDs.
   - **Database Architecture**: Alembic migration (`a1b2c3d4e5f6_add_outlet_profile_tables.py`) adding `outlet_profiles`, `outlet_links`, `outlet_metrics`, and `outlet_manual_facts`.
6. **Publication-Quality PDF Reports (ReportLab)**:
   - **Subject Report (`GET /api/reports/subject.pdf`)**: Cover header, executive KPI metrics, top covering channels table, chronological story breakdown with tone badges and post lists, and audit methodology.
   - **Channel Dossier (`GET /api/reports/channel/{id}.pdf`)**: Corporate profile, audience verification status table, track record, coverage analytics, and full collected posts archive.
7. **Professional UI/UX Polish**:
   - CSS design tokens in `:root` and complete dark mode support (`[data-theme="dark"]`).
   - Theme toggle button in header persisted via `localStorage`.
   - Shimmering skeleton loaders during feed and channel data fetching.
   - Sticky filter bar with interactive **Active Filter Chips** row (one-click removal of individual active filters or full clear).
   - Indian number formatting (e.g., `1,23,456` instead of `123456`).
   - Relative time formatting ("2h ago", "1d ago") with exact IST/UTC timestamp tooltip on hover.
   - 44px touch targets and responsive mobile breakpoints (390px, 768px, 1280px, 1440px).

---

## Quick Start

### 1. Local Development (FastAPI + SQLite)

Requires **Python 3.11 or newer** (the code uses `datetime.UTC` and `X | None` types; Python 3.7–3.10 fail on import).
A `.venv` folder copied from another computer will not work — create it fresh on each machine.

```bash
cd backend
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\activate   # Windows PowerShell
# source .venv/bin/activate  # Linux / macOS

pip install -r requirements.txt
cp ../.env.example ../.env  # Set ADMIN_TOKEN

# Seed default sources and tracked subjects
python -m app.seed

# Start the FastAPI server
uvicorn app.main:app --port 8000 --host 127.0.0.1
```

Open in your browser:
- **Dashboard**: [http://localhost:8000](http://localhost:8000)
- **Admin Console**: [http://localhost:8000/admin](http://localhost:8000/admin) (Admin token: check `ADMIN_TOKEN` in your `.env`)
- **API Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)

### 2. Docker Compose (PostgreSQL Production)

```bash
cp .env.example .env
docker compose up -d --build
```

---

## Configuration & API Keys

All keys are optional. Features gracefully fall back to free feeds or transparently show missing-key reasons without breaking:

| Key | Purpose | If Not Present |
|---|---|---|
| `ADMIN_TOKEN` | Secures `/api/admin/*` and `/admin` console | Defaults to random string generated in `.env` |
| `ANTHROPIC_API_KEY` | High-quality story summaries & editorial tone | Falls back to rule-based keyword tone and publisher excerpts |
| `YOUTUBE_API_KEY` | YouTube broad search and video subscriber metrics | Uses public YouTube RSS feeds for monitored news channels |
| `META_ACCESS_TOKEN` | Instagram & Facebook audience metrics | Displays *"Not available: Meta Graph API access required"* |
| `IG_BUSINESS_ACCOUNT_ID`| Instagram discovery metrics | Displays *"Not available"* with requirement note |
| `MARKET_DATA_API_KEY` | Public market cap & stock valuation metrics | Displays *"Not available"* with requirement note |
| `FACTCHECK_API_KEY` | Google Fact Check Tools API claims search | Displays *"Not available: needs FACTCHECK_API_KEY"* |

---

## Automated Test Suite

BetWatch includes comprehensive unit, integration, pipeline, and browser tests:

```bash
cd backend
# Run all unit and integration tests (33 tests)
python -m pytest -k "not test_e2e and not test_live" -v

# Run PDF report tests
python -m pytest -k "test_pdf_reports" -v

# Run Playwright End-to-End browser test (Chromium headless)
$env:BETWATCH_E2E_URL="http://127.0.0.1:8000"
python -m pytest tests/test_e2e.py -v
```

All 34 automated test suites run cleanly with 100% pass rate.
