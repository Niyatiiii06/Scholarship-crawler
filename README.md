# Scholarship Intelligence Crawler

EDXSO AI Engineer Intern - Assignment 2

An automated crawler that keeps a database of scholarships for Indian students up to date. It discovers scholarship pages, reads them, keeps only what the official source actually says, scores every record from evidence, and records every change between runs.

```
Discover -> Crawl -> Extract -> Verify -> Score -> Store -> Update
```

Everything is free: Python, SQLite, Streamlit, DuckDuckGo search, the Internet Archive, and the free tiers of Groq / Gemini (or a local Ollama model).

## Results (database built 11-12 November 2026)

| Measure | Result |
| --- | --- |
| Scholarships discovered | 59 |
| On an official source | 59 (100%) |
| VERIFIED at 95%+ confidence | 10 |
| Backed by two or more official pages | 12 |
| Source types | 5 (government, scholarship portal, university, foundation/trust, corporate CSR) |
| Active / expiring soon / expired | 20 / 1 / 7 |
| Changes recorded | 33, across 7 scholarships, from comparing each official page with its Internet Archive copy of 1-10 months earlier |
| Average confidence | 82.4 |
| Pages crawled in the latest run | 815 |

Built over four crawler runs on two days, plus one `history` run. The recorded changes include genuine ones (the HDFC Parivartan deadline moved from 31 December 2025 to 15 December 2026; the Bharti Airtel income limit changed from Rs 8.5 lakh to Rs 8 lakh; several application links changed) and some where a text field was reworded on the page without changing its meaning. Every value in the database carries the URL of the official page it came from and the exact quote that supports it. The 49 records below 95% are mostly portal listings whose scheme guidelines have not yet been read (they give a deadline but no quoted eligibility), and pages that state no full closing date. One record (SDEF India Scholarship) is held at REVIEW REQUIRED because the provider's web page and its own applicant handbook give different deadlines; the system flags the conflict instead of choosing one.

## What makes the output trustworthy

- **Official sources only.** A record is stored only when its page is on a government domain, an institution's own academic domain, a recognised scholarship portal, or a site whose address matches the provider's name. Aggregators and news sites are used to learn scholarship *names*; none of their data is stored.
- **Every value has a quote.** The LLM must return an exact quote for each value. Code then checks that the quote is on the page and that it supports the value (the date in the quote equals the date extracted, the numbers match, the link is on the page). Anything that fails becomes `Not specified`.
- **The confidence score is calculated, not generated.** Nine evidence checks award points that add up to 100. The dashboard shows the points and the reason for each check.
- **One scholarship, several official pages.** A portal listing often gives the deadline and apply link while the scheme's guidelines give the eligibility. When two official pages clearly describe the same scholarship, they are combined into one record, and every field keeps the URL and quote of the page it came from.
- **Nothing is overwritten.** Every extraction is kept as a version, and every change is logged with old value, new value, date, source and evidence.

## Setup

```bash
git clone https://github.com/Niyatiiii06/scholarship-crawler && cd scholarship-crawler
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Use Python 3.10 to 3.13. Put a free Groq key (and optionally a Gemini key) in `.env`.

## Run

```bash
python -m pytest -q                # 24 tests on a fake mini-internet (no keys, no network)
python main.py check               # confirm the LLM key and models work
python main.py run                 # full cycle: re-verify stored records, then discover new ones
python main.py history             # compare with older Internet Archive copies of the official pages
python main.py run --no-discover   # run again: re-verify only, shows change / expiry detection
python main.py reground            # after a code change: rebuild records from saved LLM answers, no LLM calls
python main.py status              # counts
streamlit run app.py               # dashboard
python main.py watch --hours 24    # keep running, one cycle every 24 hours
```

The first run takes roughly 20-40 minutes because the crawler waits between requests and stays inside free LLM limits. Run it more than once (or on later days) to grow the database: pages already read are not sent to the LLM again.

`.github/workflows/crawl.yml` runs the crawler every day on GitHub's free runners and commits the updated database.

## What you get

| File | Contents |
| --- | --- |
| `data/scholarships.db` | The SQLite database (open with DB Browser for SQLite or any SQLite viewer) |
| `data/scholarships.csv` | One row per scholarship |
| `data/scholarships.json` | Each scholarship with its evidence quotes, score breakdown and change history |
| `data/changes.csv` | Every detected change |

Database tables: `scholarships`, `evidence`, `score_checks`, `versions`, `changes`, `pages`, `leads`, `runs`.

## How it works

### 1. Discovery (`crawler/discovery.py`, `crawler/pipeline.py`)

Seeds in `config.yaml` are starting points only. From there the crawler:

- runs web searches (DuckDuckGo) for the queries in `config.yaml`, plus generated queries naming the current and next two months to find scholarships that are open now;
- reads each seed site's `sitemap.xml` for scholarship URLs;
- follows links, scoring each by its text and address (`scholarship`, `fellowship`, `stipend`...) and visiting the most promising first, up to 2 links deep;
- on aggregator pages, collects scholarship **names** as leads, then searches the web for each name and accepts only a result that looks official.

It respects `robots.txt`, identifies itself honestly, and pauses between requests to the same site.

### 2. Source classification (`crawler/sources.py`)

Each page's site is classified as `government`, `university`, `scholarship_portal`, `corporate_csr`, `foundation_trust`, `international`, `aggregator` or `unknown`, with a written reason. A college re-posting another body's scholarship is not treated as official; it becomes a lead and its deadline is used for conflict checks.

### 3. Extraction (`crawler/extraction.py`)

Pages that look like scholarship descriptions (a 0-10 heuristic score) are sent to the LLM with a strict prompt: use only the page text, give an exact quote per field, leave out anything the page does not state. HTML and PDF are both supported. If the LLM fails on a page, the page is skipped and retried on the next run. A weak pattern-matching extractor exists for running with no LLM at all (`--no-llm`).

Fields: name, provider, amount, eligibility, academic requirements, education level, income criteria, income limit (number), age, gender, category, domicile, institution, disability, opening date, closing date, documents, selection process, renewal, application mode, application URL.

### 4. Grounding (`crawler/grounding.py`)

For each field: the quote must be on the page, and the quote must support the value. Matching ignores spacing and punctuation (PDF text is messy) but the letters and digits must match in order. If the LLM's quote is not exact, the verifier searches the page itself for a passage containing the value's words and numbers, and stores that passage as the evidence. Dates and numbers never use this fallback. Rejected values are listed on the dashboard and reduce the score.

The raw LLM answer for every page is saved, so `python main.py reground` can re-check all records after a code change without spending LLM quota.

### 4b. Combining official pages (`crawler/pipeline.py`: `consolidate`, `fill_gaps`)

Records are combined only when their names match strictly: the same distinctive words, or one name contained in the other with at least three distinctive words and no extra audience word (so "Post Matric for SC" is never merged with "Post Matric for SC Students with Disabilities", and "Technical Degree" never with "Technical Diploma"). The page that states the deadline stays the main source; the other page fills only the fields the main page lacks. If the two pages give different deadlines in the same cycle, the record is marked `REVIEW_REQUIRED`. On every later run each value is re-checked on the page it came from, and withdrawn if that page no longer supports it.

Long listing pages with many deadlines are read in up to three parts so the whole table is covered.

### 5. Confidence score (`crawler/scoring.py`)

| Check | Points | Full marks when |
| --- | --- | --- |
| Official source | 25 | the page is on an official domain |
| Live on source | 10 | the page loaded in this run and names the scholarship |
| Application link | 10 | the link is published on the official page and responds (6 if only "how to apply" is quoted). On a listing page, the apply link the page gives for its other schemes, or its single apply button, is used and the evidence says so |
| Eligibility evidence | 10 | eligibility plus a specific criterion, or at least two specific criteria, are quoted |
| Deadline evidence | 10 | a full closing date is quoted |
| Currency | 10 | the deadline is in the current cycle |
| No conflict | 10 | no other source gives a different deadline for the same cycle |
| Extraction consistency | 5 | the LLM's deadline agrees with pattern matching (half the points); 0.5 off per rejected value |
| Traceability | 10 | name, provider, amount, eligibility, deadline and link (or how to apply) all have quotes |

`VERIFIED` needs 95 or more, an official source, and no conflict. Everything else is `REVIEW REQUIRED`.

### 6. Updates, changes and stale records (`crawler/pipeline.py`)

Every run first revisits the official page of each stored scholarship:

- page unchanged: status and score are refreshed;
- page changed: stored quotes are re-checked, and the page is re-extracted only if a quote disappeared or a key field was missing; real differences are logged in `changes`;
- page gone (404/410, or two failed attempts) or the scholarship's name no longer on it: `NO_LONGER_VERIFIABLE`, with the old data kept;
- deadline passed: `EXPIRED`; within 15 days: `EXPIRING_SOON`; conflicting deadline: `REVIEW_REQUIRED`.

`python main.py history` fetches an older copy of each official page from the Internet Archive, extracts it with the same checks, and logs differences from today's page as changes, labelled with the archive URL and date. This shows real past changes without waiting months between runs.

### 7. Dashboard (`app.py`)

Totals, a searchable and filterable list, and a details view with "Why this score?", change history and source evidence.

## Limitations

- Sites that build their content with JavaScript (for example parts of the National Scholarship Portal) return little text to a plain HTTP request and are mostly skipped.
- Provider-to-domain matching is a heuristic. It is deliberately strict, so some genuine corporate or trust pages end up as leads instead of records.
- Pages that give only a month or an academic year, not a full closing date, cannot reach 95.
- Free LLM limits cap a run at about 60 extractions; a larger database needs several runs. Most of that budget is reserved for pages that state a deadline, since only those can be fully verified.
- Scanned-image PDFs have no extractable text.
- Search results vary from day to day, so two machines will not discover exactly the same pages.

## Project layout

```
main.py                 command line
app.py                  Streamlit dashboard
config.yaml             seeds, search queries, limits, source lists, score weights
crawler/
  pipeline.py           the run: re-verify, discover, change detection, history
  discovery.py          search, sitemaps, link scoring, aggregator leads
  fetcher.py            polite downloads, HTML/PDF to text, links
  sources.py            official / aggregator classification
  extraction.py         LLM prompt, rule-based fallback, page scoring
  grounding.py          evidence checks, date and amount parsing
  scoring.py            confidence score, VERIFIED label, lifecycle status
  storage.py            SQLite tables
  llm.py                Groq / Gemini / Ollama with automatic fallback
  export.py             CSV / JSON exports and statistics
tests/test_crawler.py   24 tests
TECHNICAL_NOTE.md       short technical note
```
