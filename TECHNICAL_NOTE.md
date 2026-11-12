# Technical note: Scholarship Intelligence Crawler

## 1. Architecture

```
seeds + web search + sitemaps + aggregator leads
                |
        priority frontier  ->  fetcher (robots.txt, rate limit, HTML/PDF)
                |
        source classifier  ->  aggregator? keep the NAME as a lead, store nothing else
                |
        page scorer (0-10) ->  LLM extraction with a quote per field
                |
        grounding checks   ->  unsupported values become "Not specified"
                |
        confidence engine  ->  9 evidence checks = 0-100, VERIFIED at 95+
                |
        SQLite: scholarships, evidence, score_checks, versions, changes
                |
        Streamlit dashboard
```

One command, `python main.py run`, performs a full cycle. Each cycle first re-verifies every stored scholarship against its official page, then discovers new ones. A scheduler (`watch`, or the included GitHub Actions workflow) repeats it daily.

## 2. Technology choices

| Need | Choice | Reason |
| --- | --- | --- |
| Crawling | `requests` + BeautifulSoup + pypdf | simple, free, enough for server-rendered government and institution pages |
| Search | DuckDuckGo via `ddgs` | free, no key |
| Extraction | Groq / Gemini free tiers, or local Ollama | free; the provider is swappable because the LLM is never trusted on its own |
| Storage | SQLite | free, a single inspectable file |
| Interface | Streamlit | free, deployable to Streamlit Community Cloud |
| History | Internet Archive | free access to earlier versions of official pages |

No paid API or scraping service is used.

## 3. Discovery

Discovery is not a list of URL-specific scrapers. There is one generic pipeline, fed from four sources:

1. **Web search** for configurable queries.
2. **Sitemaps** of seed sites, filtered for scholarship URLs.
3. **Link following** from every visited page. Links are scored by anchor text and address, and a priority queue visits the most scholarship-like first, up to two links deep.
4. **Aggregator leads.** On aggregator pages only scholarship names are collected. Each name is searched, and only a result on a government, academic, portal, or name-matching domain is crawled.

Every record stores how it was discovered (`discovered_via`).

## 4. Source classification

A page's site is classified by rules, each producing a written reason:

- configured aggregator/news domains, or addresses containing listing-site words: `aggregator`, never official;
- government suffixes (`.gov.in`, `.nic.in`): official;
- academic suffixes (`.ac.in`, `.edu.in`): official only if the provider's name or initials match the domain, so a college re-posting an AICTE notice is not treated as the primary source;
- other domains: official only if a distinctive word of the provider's name appears in the address (for example Reliance Foundation and `reliancefoundation.org`).

Only officially sourced pages become records. Everything else becomes a lead.

## 5. Extraction

A heuristic page score (scholarship words in the heading, eligibility, deadline words, rupee amounts, application words, length) decides which pages are worth an LLM call. The LLM receives the page text and the page's candidate application links, and must return for every field a value and an exact quote, omitting fields the page does not state. Dates must be returned in ISO format and only when the full date is written. The application URL must be chosen from the links actually on the page.

## 6. Anti-hallucination

The LLM output is treated as a proposal. For each field, code checks:

1. **The quote exists**: ignoring spacing and punctuation, its letters and digits must appear on the page in order. A quote shortened with "..." must have every piece on the page.
2. **The quote supports the value**: for dates, the date parsed from the quote must equal the value; for the income limit, the number parsed from the quote ("8 lakh" = 800000) must equal the value; for text, every number in the value must be in the evidence and most of its words must come from the quote or the lines around it; for the application URL, the link must be among the page's links.

If the LLM's quote fails for a text field, the verifier searches the page for one to three consecutive lines containing all of the value's numbers and more than 80% of its words; if found, that passage becomes the stored evidence. Dates and the income limit never use this fallback.

A value failing these checks is stored as `Not specified`, and the rejection is recorded and shown. The raw LLM answer per page is kept, so all records can be re-checked after a code change without new LLM calls. The result is the trace the assignment asks for: database value -> official source URL -> quote -> extracted value, stored per field in the `evidence` table.

**Records backed by more than one official page.** Official information about one scholarship is often split: a portal listing has the deadline and application link, a guidelines PDF has the eligibility. When two official pages describe the same scholarship (strict name match: identical distinctive words, or containment with at least three distinctive words and no extra audience word such as SC, girls or disabilities), they are combined into one record. The page with the deadline is the main source and the other fills only missing fields. Each field stores its own source URL and quote, each is re-checked on its own page in later runs, and a deadline disagreement between the pages is treated as a conflict.

## 7. Confidence score

The score is computed from recorded facts; no model is asked for a number.

| Check | Points |
| --- | --- |
| Source is official | 25 |
| Page loaded in this run and names the scholarship | 10 |
| Application link published on the official page and responds | 10 |
| Eligibility backed by quotes | 10 |
| Deadline backed by a quote | 10 |
| Information is current | 10 |
| No conflicting source | 10 |
| Extraction consistency (LLM vs pattern matching, no rejected values) | 5 |
| Key fields traceable to quotes | 10 |

Partial credit is defined per check (for example a quoted "how to apply" with no link earns 6 of 10). The points and the reason for each check are stored in `score_checks` and shown under "Why this score?".

`VERIFIED` requires a score of 95 or more **and** an official source **and** no conflict. A conflict is another source giving a different deadline for the same scholarship within the same cycle (within 200 days; a larger gap is the previous year's notice).

## 8. Change detection

Every extraction is saved as a row in `versions`; the current record is never the only copy. On each run:

- The official page is fetched and its text hashed. If the hash is unchanged, only status and score are refreshed.
- If the hash changed, every stored quote is re-checked against the new text. Re-extraction happens only when a quote has disappeared or a key field was missing, which avoids false "changes" caused by an LLM rephrasing the same text.
- A field change is logged only when the new value passed grounding and differs (dates, numbers and URLs exactly; text by similarity, with any differing number counting as a change), or when the old quote has been removed from the page.

Each row of `changes` holds old value, new value, detection time, source URL, new evidence and old evidence.

**History from the Internet Archive.** Real deadlines rarely change between two runs made minutes apart. `python main.py history` therefore fetches an older archived copy of each official page, runs the same extraction and grounding on it, and logs verified differences from the live page as changes, each labelled with the archive URL and snapshot date.

## 9. Stale and expired records

| Status | Rule |
| --- | --- |
| `ACTIVE` | quoted deadline more than 15 days away |
| `EXPIRING_SOON` | quoted deadline within 15 days |
| `EXPIRED` | quoted deadline has passed |
| `REVIEW_REQUIRED` | no full deadline on the page, or a conflicting source |
| `NO_LONGER_VERIFIABLE` | official page returns 404/410, fails twice, or no longer names the scholarship |

Status changes are logged in `changes`. A record that can no longer be verified keeps its last known data and loses its `VERIFIED` label.

## 10. Testing

Twenty-four automated tests run the whole pipeline against an in-memory fake website set and a fake LLM. They cover grounding (including an LLM that deliberately invents values), source classification, scoring, change detection across two runs, removal detection, conflict handling, multi-page records and withdrawal of a value when its supporting page disappears, the archive comparison, LLM failure handling, the no-LLM fallback and the exports.
