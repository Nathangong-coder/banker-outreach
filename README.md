# Outreach

Finds people at your target banks, scores them on shared background, writes a
cold email for each one, and leaves it in your Gmail drafts folder. It never
sends anything.

Two ways to run it: a GitHub Actions cron for the unattended daily job, and a
local web app for reviewing what it wrote.

## How the work is split

The pipeline has four stages, and they run on different cadences on purpose.

| Stage | What it does | How often |
|---|---|---|
| `refresh` | Searches Apollo for people at every active bank | Monthly |
| `enrich` | Reveals work emails for a small batch, scores them on fit | Daily |
| `draft` | Writes an email for the top scorers | Daily |
| `push` | Moves the drafts you approved into Gmail | Daily |

Search is the expensive stage and bank rosters barely move week to week, so it
is not part of the daily job. The daily job works through the pool that search
already built. That keeps a steady flow of drafts coming without paying for the
same search results over and over.

## Setup

```bash
git clone <your repo> && cd outreach
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
python -m src.cli init
```

**Gmail.** Create an OAuth client in Google Cloud Console (application type:
Desktop app), enable the Gmail API, put the client ID and secret in `.env`, then:

```bash
python -m src.cli gmail-auth
```

Paste the resulting refresh token into `.env` and into your repo secrets. The
only scope requested is `gmail.compose`, which can create drafts but cannot read
your mail or send.

**Banks.** Export your sheet as CSV with these columns:

```csv
name,domain,category,priority
Goldman Sachs,gs.com,BB,1
```

`priority` decides search and draft order — 1 goes first. `category` is your own
tiering and shows up in the UI. `domain` makes Apollo search far more accurate
than name matching; fill it in where you can.

Then `python -m src.cli load-banks`, or upload the CSV from the Banks page.

**First real run:**

```bash
python -m src.cli refresh    # builds the pool, uses the most credits
python -m src.cli enrich     # emails + fit scores for 10 people
python -m src.cli draft      # writes emails for the top scorers
```

## The web app

```bash
uvicorn web.app:app --reload --port 8000
```

Then open `http://localhost:8000`. You get the pipeline counts, buttons to run
any stage, a review queue where you can edit a draft before approving it, the
full contact pool, bank management, and key status.

It runs locally and has no authentication. That is deliberate: your Apollo and
model keys live in `.env` on this machine, and a public page that manages API
keys is a page that leaks API keys. If you ever want it hosted, put real auth in
front of it first.

## Multiple keys

Both Apollo and the model provider accept several keys:

```
APOLLO_API_KEYS=key_one,key_two,key_three
ANTHROPIC_API_KEYS=sk-ant-aaa,sk-ant-bbb
```

The pool round-robins. A key that returns 429 rests for the duration of its
`Retry-After` while the others keep working; a key that returns 401 or 403 drops
out for the rest of the run. The Keys page shows call counts and status per key
using a short hash, never the key itself.

This is for smoothing over rate limits across accounts you legitimately hold.
Check Apollo's terms before pointing several accounts at the same workflow.

## Switching model providers

```
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEYS=sk-...
```

Supported: `anthropic`, `openai`, `google`, `openrouter`. Set `LLM_BASE_URL` if
you route through a gateway. Model names change often, so check your provider's
current list rather than trusting the default in `.env.example`.

## Scheduling

`.github/workflows/daily.yml` runs weekday mornings at 6:30 Pacific and commits
the updated database back to the repo. Add these repo **secrets**:

`APOLLO_API_KEYS`, your provider's key variable, `GMAIL_CLIENT_ID`,
`GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`

and these repo **variables**: `SENDER_NAME`, `SENDER_EMAIL`, `SENDER_BLURB`,
`LLM_PROVIDER`, `LLM_MODEL`.

Because the DB is committed by CI, pull before you work locally, or you will hit
a merge conflict on a binary file — which SQLite cannot merge. The workflow
rebases before pushing to reduce the chance of this, but the habit matters more.
If it becomes a nuisance, move the DB to Turso or Postgres and drop the commit
step.

## Fit scoring

`config/affinity.yml` holds the rules. Each one matches a school name or a
keyword and carries a weight; a contact's score is the sum of what matched, and
the labels that matched get passed into the drafting prompt as the reason you
are writing.

Everything scored here comes from what someone published about themselves —
their school, degree, employer, stated group. Nothing is inferred from a name.
Name-based inference is unreliable enough to poison the ranking, and a stored
list of people tagged by guessed personal characteristics is not something you
want sitting in a repo.

Edit the rules freely. Adding a school or a group takes one line.

## Two things that will bite you

**Apollo plan gating.** API access to email reveal has historically been limited
to certain plans, with per-minute, per-hour and per-day caps that vary. Endpoint
paths move around too. If `refresh` returns 403, check your plan before
debugging the code. `APOLLO_BASE_URL` is overridable for this reason.

**Generic drafts.** Apollo gives you title, company, and school. That produces a
competent email that reads like every other cold email. The `hook` column on
`contacts` exists for the thing that actually makes someone reply — a deal their
group ran, a recent move, something specific. Nothing populates it yet. Filling
it, by hand or from a news source, is the single highest-leverage change you can
make to this system.

## Volume

The default is 12 drafts a day. Near-identical cold emails at volume from a
personal address hurt deliverability and get flagged. Twelve is also roughly the
number a person can actually follow up on. Raise it if you must, but the
constraint is doing you a favor.
