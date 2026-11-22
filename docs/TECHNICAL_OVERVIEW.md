# Technical Overview

Signal-Stack is a Python application organized into UI, service, domain, and
infrastructure layers. It runs locally on Streamlit and SQLite. This document describes
the current implementation, not a production hosting or security certification.

## Architecture and data flow

```text
Streamlit UI / scheduler worker
                         |
            services and domain workflows
                         |
      infrastructure: SQLite, HTTP, parsers, Gemini
                         |
        local SQLite database and upload files
```

- **UI:** `app/streamlit_app.py` exposes setup, profile, interview, stack, source manager,
  feed, brief, settings, and context-reassessment screens.
- **Services:** `src/signalstack/services/` coordinates multi-step research, crawling,
  analysis, feed queries, audits, and jobs.
- **Domain:** `src/signalstack/domain/` contains business rules for interviews, source
  stacks, ranking, brief quality checks, schedules, reassessment, and uploads.
- **Infrastructure:** `src/signalstack/infrastructure/` contains configuration, SQLite,
  HTTP fetching, extraction, parsers, source validation, and the LangChain Gemini adapter.

The main pipeline is:

1. `services/research.py` fetches the company home page and selected key pages.
2. Gemini returns a schema-validated profile; `services/research.py` checks proposed
   evidence quotes against fetched page text and downgrades unsupported facts.
3. `domain/interview.py` asks one company-specific question at a time across ten core
   topics. A LangGraph turn graph prepares context, analyzes the saved answer, routes to
   a follow-up or next topic, and persists the result. Answers are saved before graph
   processing, enabling retry after an AI failure.
4. `domain/stack.py` asks Gemini for competitors and customer/market source ideas, adds
   sources derived from competitor sites, validates them, and stores them as proposed.
   The internal category creates upload slots; it does not crawl private files.
5. A user approves sources. `services/crawler.py` visits approved external sources,
   parses/diffs their content, removes duplicates, and records crawl outcomes.
6. `services/analyzer.py` filters and analyses collected records. `domain/ranker.py`
   scores analysed signals and stores a factor breakdown and explanation.
7. `domain/brief.py` selects evidence-linked signals, asks Gemini for a structured
   narrative, validates references and unsupported numbers, and saves Markdown and actions.
8. LangGraph routes scheduled monitor, weekly-brief, and monthly-audit jobs. Monitor and
   weekly jobs pass through crawl and analysis nodes, with brief generation conditional
   on job type; the audit route delegates to the deterministic source-audit service.
   The scheduler stores run status, errors, and a worker heartbeat.

`infrastructure/llm.py` uses LangChain's `ChatGoogleGenerativeAI` with Pydantic structured
outputs. The existing SQLite response cache, rate limiter, retry policy, and call logs
remain in the adapter layer. LangGraph orchestrates interview turns and scheduled jobs;
the SQLite database remains the source of truth for user-visible state between graph
invocations.

## Persistence

`src/signalstack/infrastructure/db.py` creates the SQLite schema. Important relationships:

- `companies` owns facts, interview turns, profile entries, competitors, sources, records,
  signals, briefs, settings, uploads, audits, and job runs.
- `sources` connect a company and optional competitor to crawls and records.
- `records` store collected content once per company; `record_sightings` tracks repeat
  sightings across sources.
- `signals` store AI analysis and rank components for records.
- `briefs` and `brief_signals` preserve report history and cited signal references;
  `brief_actions` stores the action tracker.
- `uploads`, `upload_rows`, and `upload_findings` hold parsed internal-data analysis.

The default database is `data/signalstack.db`; `DB_PATH` can override it. The entire
`data/` directory is local and Git-ignored. There is no schema migration runner yet;
schema changes currently use `CREATE TABLE IF NOT EXISTS` in `db.py`.

## Discovery, crawling, and evidence

Source plans are generated from the company website facts, founder profile, and interview
answers. Each external source stores its category, type, target, reason, source tier,
validation state, and optional competitor relationship. Proposed sources are not crawled
until approved. Invalid sources cannot be approved through the normal status API.

`PoliteFetcher` uses an application user agent, checks robots.txt, applies request delays,
limits response size and redirects, and does not implement login or protection bypass.
Feed, Hacker News, and page-change handlers parse public content. Records retain source
URLs, publisher, publication/discovery dates, text, and content hashes. URL and content
uniqueness constraints prevent duplicate storage.

Evidence verification is deliberately narrow: `domain/verify.py` checks whether a quoted
snippet occurs in the collected text. This supports traceability, but is not independent
fact checking or proof that a source is accurate.

## Analysis and ranking

`services/analyzer.py` asks Gemini for schema-constrained structured analysis and records
classification, relevance, impact, urgency, evidence snippets, and interpretation. The
quote check distinguishes a quote found in the stored text from an unsupported quote.

`domain/ranker.py` calculates a 0-100 priority score using configured weighted factors:

| Factor | Weight |
| --- | ---: |
| Strategic relevance | 0.25 |
| Business impact | 0.20 |
| Urgency | 0.15 |
| Source reliability | 0.10 |
| Evidence strength | 0.10 |
| Recency | 0.10 |
| Novelty | 0.10 |

Source reliability is based on source tier; evidence strength uses quote verification,
primary/official status, and sightings across sources. Relevance and impact remain AI
judgments, not truth scores. The calculation inputs and explanation are stored separately.

The brief includes a source appendix with direct links, dates, evidence snippets, and
separate reliability/evidence/relevance values. It explicitly reports empty categories
instead of generating filler. Quality checks reject unsupported signal IDs and numbers.

## Automation and failure handling

`scripts/worker.py` calls `services/scheduler.py` on a polling interval. The scheduler
calculates local-time recurrence using `domain/schedule.py`, records job runs, prevents
overlapping jobs, retries eligible failures, recovers stale runs, and updates a heartbeat.
The worker must remain running for scheduled work to execute.

The monthly audit rechecks approved and proposed external sources, evaluates activity,
redundancy, and relevance history, optionally proposes new sources, and stores a report.
Recommendations are not automatically applied.

Failures are persisted in crawl/job status and shown in **Settings & Health**. LLM calls
are rate-limited, retried for transient errors, cached by prompt/schema/model, and logged
in `llm_calls`. Tests inject fake fetchers and model functions and use temporary databases;
the test suite does not require credentials or internet access.

## Operational and privacy boundaries

- This project has no login, role-based access, or multi-tenant isolation layer.
- Uploaded data is stored locally, but selected text is sent to the configured Gemini API
  during analysis. Review provider terms and obtain consent before using personal or
  confidential information.
- Keep `.env`, the SQLite database, and uploaded files out of source control.
- Public source availability and crawling results are time-dependent. Review terms of use
  and local legal requirements for every source.
- The app exports Markdown and HTML but does not deliver email, deploy itself, or guarantee
  a fixed number of results.
