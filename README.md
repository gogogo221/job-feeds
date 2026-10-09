# job-feeds

Nightly pull of public job-board APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Eightfold, Uber) for the Daily Job Digest.

- `fetch.py` — stdlib-only fetcher + filter. Run locally with `python fetch.py`.
- `companies.json` — companies and their boards. `{"slugs": [...]}` or `{}` = auto-discover; `"own_site"` = no API (covered by web search).
- `data/jobs.json` — output: filtered NYC / US-remote / US roles (title + location + experience filters), per-board status, and companies not covered.
- `data/seen.json` — first-seen dates, so the digest can pick out new postings.
- `data/discovered.json` — auto-discovered board tokens (re-probed weekly).

Workflow: `.github/workflows/fetch.yml`, daily 03:17 UTC and on manual dispatch.
