# job-finder

Every morning a GitHub Action pulls public job feeds (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday) for the companies in `companies.json`, keeps roles that
could fit, and commits the results to `data/`. A Claude scheduled task reads
`data/latest.json` at 5:55 AM Arizona time, filters further, and emails the
digest with application packets.

- `companies.json` holds the companies to check. Add a company by giving its job-board
  type and slug (the part of the careers URL after `boards.greenhouse.io/`,
  `jobs.lever.co/`, `jobs.ashbyhq.com/`, etc.).
- `data/latest.json` lists every open matching job, with `first_seen` (the day the finder first saw it).
- `data/status.json` shows which feeds worked and which failed.
- `data/seen.json` records when each job was first seen.

Run it by hand: Actions tab → "Find jobs" → "Run workflow".
