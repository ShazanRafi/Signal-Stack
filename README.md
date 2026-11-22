# Signal-Stack

Signal-Stack is a company intelligence application. It researches a company website,
interviews its founder, proposes public monitoring sources, collects and analyses
developments, and produces an evidence-linked Monday Morning Brief.

## What it does

- Builds a company profile from public website pages and labels facts as verified or
  unverified assumptions.
- Runs an adaptive interview with 10 core topics, one question at a time, optional
  follow-ups, confidential skips, and editable profile notes.
- Proposes customer, competitor, market, and internal intelligence sources with reasons
  and validation status.
- Crawls approved public sources while respecting robots.txt and request delays.
- Deduplicates records, analyses developments, and keeps source reliability, evidence
  strength, recency, novelty, and AI relevance distinct.
- Generates a cited Markdown brief, tracks actions, and exports an email-ready HTML copy.
- Schedules monitoring, weekly briefs, and monthly source-health audits using a local
  worker process.

See [docs/TECHNICAL_OVERVIEW.md](docs/TECHNICAL_OVERVIEW.md) for the architecture and
[docs/COSTS_AND_LIMITATIONS.md](docs/COSTS_AND_LIMITATIONS.md) for costs, privacy, and
operational limits.

## Requirements

- Python 3.11 or newer
- A Gemini API key for AI-powered workflows
- Internet access for company research and source crawling

The application uses SQLite locally. The database, uploaded files, and API key are not
included in Git. A local database does not travel with a clone of this repository.

## Local setup

Run these commands from the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Add your key to `.env`:

```dotenv
GEMINI_API_KEY=your-key
```

`GOOGLE_API_KEY` is accepted as an alternative. Never commit `.env` or paste a real key
into an issue, demo recording, or report.

## Run the application

Start the web application:

```powershell
streamlit run app\streamlit_app.py
```

To run automatic jobs, open a second terminal at the repository root and start the worker:

```powershell
.\.venv\Scripts\python.exe scripts\worker.py
```

The worker must stay running for scheduled jobs to execute. The dashboard's **Settings &
Health** page shows its heartbeat and scheduled-run history. Jobs can also be started
manually from the dashboard.

## Deployment notes

The assignment prefers a deployed URL but accepts a documented local setup when deployment
is not feasible. This project is an assessment/demo application, not a production
multi-user service.

For a hosted demonstration:

- Configure `GEMINI_API_KEY` through the host's secret manager; do not upload `.env`.
- Give `DB_PATH` writable persistent storage if company data and reports must survive
  restarts. A fresh deployment starts with an empty database because `data/` is Git-ignored.
- Run `scripts/worker.py` as a separate long-running process if scheduled jobs must execute.
- Restrict access to the evaluation team. The app has no authentication or access-control
  layer and should not be exposed as an unrestricted public service.
- Verify the hosted database, worker heartbeat, source access, and generated reports before
  sharing the URL. Hosting storage and free-tier API limits vary by provider.

## Recommended company workflow

1. On **Setup**, enter the company name and website and run research.
2. Complete the 10-topic interview. Follow-ups may add questions; skipped confidential
   answers are not required.
3. Build the Signal Stack and review competitors and source reasons.
4. Approve validated sources in **Source Manager**. Only approved external sources are
   crawled.
5. Crawl, analyse records, review the **Intelligence Feed**, and generate a brief.
6. Configure the time zone, monitoring cadence, weekly brief, and monthly audit under
   **Settings & Health**.
7. When company context changes, use **Change business context** to reassess the stack.
   Review proposed additions/removals before applying the report.
8. Export the generated report and include it as a sample only after checking its source
   links, evidence, and company-specific content.

An empty or zero-signal brief is a truthful status report, not a completed intelligence
demonstration. Do not add fabricated records to meet sample-count targets.

## Command-line examples

After installation, commands are available as `signalstack-*` entry points:

```powershell
signalstack-research "MeetUniversity" "https://www.meetuniversity.com/"
signalstack-interview COMPANY_ID
signalstack-stack build COMPANY_ID
signalstack-stack approve-all COMPANY_ID
signalstack-crawl run COMPANY_ID
signalstack-analyze run COMPANY_ID
signalstack-brief generate COMPANY_ID 7
signalstack-brief list COMPANY_ID
signalstack-brief export BRIEF_ID "sample_reports\meetuniversity.md"
signalstack-scheduler overview
```

Replace uppercase placeholders with IDs from the application/database. Use each command
without arguments for its usage text. The command-line and Streamlit interfaces call the
same underlying services and domain logic.

## Configuration

The main settings are read from `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | empty | Gemini API authentication |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Structured-generation model |
| `DB_PATH` | `data/signalstack.db` | SQLite database file |
| `LLM_MAX_RPM` | `8` | Local request rate limit |
| `LLM_MAX_RETRIES` | `4` | Retry limit for transient model failures |
| `USER_AGENT` | Signal-Stack user agent | HTTP crawler identity |

Per-company schedules (time zone, brief time, monitoring interval, audit time) are managed
in the dashboard. See `.env.example` and the Settings page for current values.

## Tests

Install development dependencies as part of setup, then run:

```powershell
python -m pytest -q
```

The suite uses isolated temporary SQLite databases and mocked external services; it does
not require a Gemini key or make live network requests.

## Check demonstration readiness

After preparing the demonstration companies, run the database checklist:

```powershell
python scripts\demo_readiness.py
```

Optionally pass only the company IDs you plan to demonstrate:

```powershell
python scripts\demo_readiness.py 1 2 3
```

It checks the assignment's measurable counts: MeetUniversity's interview, competitor and
approved-source targets; three distinct industries; completed interviews and cited briefs;
30 collected records; and stored audit evidence. It does not verify that records are real or
that source evidence is accurate; check those manually. Exit code `1` means one or more
checks failed.

## Submission and demonstration checklist

The application code is not itself proof that the required live-data targets have been
met. Before submitting, use the live application and include reproducible evidence for:

- MeetUniversity plus two companies from different industries.
- Ten completed core interview topics per demonstration company.
- Five to ten relevant competitors and at least 20 approved MeetUniversity sources.
- At least 30 real collected records across the demonstrations.
- One evidence-linked brief per company, plus historical crawl and scheduler/audit evidence.
- Three reviewed sample reports: MeetUniversity and two companies from different industries.
  Check all required sections, citations, evidence, and limitations; do not pad thin evidence.
- The technical and cost documentation linked above.
- A 5–10-minute screen recording showing the end-to-end workflow from company setup to the
  final brief. A deployed URL is preferred but optional when local setup is documented.

Create reports from the database after collecting real data. For example:

```powershell
signalstack-brief list COMPANY_ID
signalstack-brief export BRIEF_ID "sample_reports\company-name.md"
```

Review exported reports for evidence links and sensitive founder-entered information before
committing them. The default `data/` directory and its database are ignored by Git, so a
fresh clone starts without your local demonstration data.

## Repository layout

```text
app/                         Streamlit user interface
src/signalstack/cli/          Installed command-line entry points
src/signalstack/domain/        Business rules and structured workflows
src/signalstack/services/      Research, crawling, analysis, jobs, and scheduling
src/signalstack/infrastructure/ SQLite, HTTP, parsing, source adapters, and Gemini client
scripts/                      Scheduler worker and diagnostics
tests/unit/                   Unit tests
migrations/                   Database schema-change guidance
sample_reports/               User-reviewed Markdown report exports
data/                         Local SQLite database and uploaded files (ignored)
docs/                         Technical overview and cost/limitations statement
```

## Important limitations

- Gemini access and free-tier eligibility depend on the account, model, region, and current
  provider terms. Review [Google's API pricing](https://ai.google.dev/gemini-api/docs/pricing)
  before use; the application cannot prevent an account from incurring provider charges.
- Free-tier Gemini terms may allow submitted content to be used to improve Google's
  products. Do not upload confidential or personal data unless you have approval and have
  checked the applicable provider terms.
- This is a local application, not an authenticated multi-user service. SQLite and the
  local worker are intended for a single-instance demonstration.
- Crawling availability, source quality, and the number of records vary by source and time.
  Robots rules, rate limits, changing websites, and network failures can leave a source
  incomplete.
- “Verified” means an evidence quote was found in the stored source text; it does not prove
  that a publisher's claim is true. AI relevance, impact, and recommendations are judgments.
- The application creates downloadable reports; it does not send email or host/deploy itself.
