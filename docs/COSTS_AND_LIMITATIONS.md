# Costs and Service Limitations

Status reviewed: 21 November 2026. Provider plans, model availability, and terms can
change; re-check the linked official pages before a demonstration or deployment.

## Cost statement

| Component | Role | Expected direct cost |
| --- | --- | --- |
| Python, Streamlit, SQLite, and project dependencies | Application runtime and storage | $0 license cost; open-source software |
| Google Gen AI Python SDK | Calls Gemini structured generation | $0 library cost |
| Gemini Developer API | Research profile, interview, source plan, analysis, brief, and reassessment | Free only while the account/model/request qualifies for Google's free tier; otherwise potentially billed |
| Public source requests | Company websites, news/RSS, and Hacker News | No API fee is configured by this project; internet/hosting costs may still apply |
| Hosting | Optional deployment | Not included; depends on the hosting provider and plan |

The application defaults to `gemini-3.5-flash-lite`, but the code cannot determine whether
a particular API key is on a free or paid billing tier and cannot enforce a $0 spending
limit. Use an account and model eligible for the free tier, do not enable paid billing if
the assignment requires strictly free usage, and check the current rate limits and pricing
before running a large workflow. This project does not use a paid scraping service or
search-grounding tool.

Google's pricing page describes the free tier as having limited model access and free
input/output tokens; it also states that free-tier content may be used to improve Google
products. Paid plans have different privacy and billing terms. Review:

- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API terms](https://ai.google.dev/gemini-api/terms)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)

## Data and privacy limits

- `.env` contains the Gemini key and is excluded from Git.
- SQLite and uploaded files remain under the local `data/` directory by default and are
  excluded from Git.
- Text selected from uploaded documents is sent to the Gemini API for analysis. The UI
  warns users not to upload confidential material to a free-tier account. Do not send
  personal or confidential data unless you have authorization and have reviewed the
  provider's applicable data terms.
- The application has no authentication or access-control layer. Do not expose the local
  Streamlit app publicly without adding appropriate security controls.
- The default SQLite setup is intended for a local, single-instance demonstration, not
  concurrent multi-user production hosting.

## Functional limits

- Source discovery and coverage depend on the model response, website structure, network,
  robots.txt, rate limiting, and whether sources are active. Minimum counts must be
  demonstrated using real collected records; the software must not invent data to meet them.
- Feed and search-query sources may return headlines/snippets rather than full article text.
- “Verified” means a quote was found in the text captured by this app. It does not mean the
  event or publisher claim was independently verified.
- AI-assigned relevance, impact, urgency, summaries, and recommendations may be wrong.
  Reports include source evidence so a reviewer can check the underlying material.
- Upload parsing is text-focused. PDFs are limited to 60 pages, uploads to 15 MB, and
  analysis samples at most 150 rows by default; scanned-image OCR is not provided.
- Scheduling only operates while `scripts/worker.py` is running. It is not a hosted
  background service.
- The app generates downloadable Markdown and email-ready HTML. It does not send email.
- This repository does not include a deployed instance or a recorded demonstration.
