# job-feeds

Nightly pull of public job-board APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Eightfold, Uber) for the Daily Job Digest.

- `fetch.py` — stdlib-only fetcher + filter. Run locally with `python fetch.py`.
- `companies.json` — companies and their boards. `{"slugs": [...]}` or `{}` = auto-discover; `"own_site"` = no API (covered by web search).
- `data/jobs.json` — output: filtered NYC / US-remote / US roles (title + location + experience filters), per-board status, and companies not covered.
- `data/seen.json` — first-seen dates, so the digest can pick out new postings.
- `data/discovered.json` — auto-discovered board tokens (re-probed weekly).
- `broad.py` + `data/broad.json` — broad coverage beyond the curated list. Each night it harvests ATS links from
  Hacker News "Who is hiring?" posts (NYC / US-remote) and probes ~150 hiring YC companies (careers-page scan, then
  slug guesses) for Greenhouse / Ashby / Lever / Workday boards. Those boards are fetched like the curated ones, but a
  job is only kept if it **posts a salary whose top end is ≥ $170k** ($230k outside NYC/remote) and the title looks
  like software. HN posts with a qualifying salary are kept as jobs too. Boards that fail or go empty 3 nights in a
  row are retired. Every job in `jobs.json` carries `source`: `curated`, `broad` or `hn`.
- Big companies without a shared ATS use custom fetchers: `google`, `microsoft`, `apple`, `janestreet`.

Workflow: `.github/workflows/fetch.yml`, daily 03:17 UTC and on manual dispatch.
