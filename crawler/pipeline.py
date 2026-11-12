"""The crawler run: re-verify what we know, discover what is new, record every change.

    Discover -> Crawl -> Extract -> Verify -> Score -> Store -> Update
"""
import json
import logging
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from . import discovery, extraction, fetcher, grounding, scoring, sources, storage
from .config import ALL_FIELDS, DATE_FIELDS, IMPORTANT_FIELDS, NOT_SPECIFIED, NUMBER_FIELDS, URL_FIELDS, now_text, today

log = logging.getLogger("pipeline")


seen_this_run = set()
page_cache = {}     # supporting pages fetched in this run


def supporting_page(cfg, url, primary_page):
    """The page a piece of evidence came from (a record can be backed by more than one official page)."""
    if url == primary_page["url"]:
        return primary_page
    if url not in page_cache:
        page_cache[url] = fetcher.fetch(cfg, url)
    return page_cache[url]


def still_supported(field, value, item, page):
    """Is this value's evidence still on the page it came from?"""
    if page["status"] in (404, 410):
        return False
    if page["status"] != 200:
        return True      # temporary failure: do not withdraw the value yet
    if field in URL_FIELDS:
        return value in [url for url, _ in page["links"]]
    return grounding.quote_on_page(item["quote"], page["text"])


def rank(record):
    """Which page should be the main source? One with a deadline, then the better-evidenced one."""
    return (record["closing_date"] != NOT_SPECIFIED, sum(1 for item in record["evidence"].values() if item["verified"]))


def fill_gaps(primary, other):
    """Copy fields the primary lacks from another official page of the same scholarship.

    Each copied field keeps its own source URL and quote. Returns the fields filled.
    """
    filled = []
    for field in ALL_FIELDS:
        item = other["evidence"].get(field, {})
        if field != "name" and primary[field] == NOT_SPECIFIED and item.get("verified"):
            primary[field] = other[field]
            primary["evidence"][field] = dict(item, note="from another official page of the same scholarship")
            filled.append(field)
    return filled


def new_counters():
    return {"pages_fetched": 0, "extractions": 0, "new": 0, "unchanged": 0, "changed": 0, "field_changes": 0,
            "expired": 0, "no_longer_verifiable": 0, "leads_found": 0, "llm_failures": 0, "no_deadline_extractions": 0, "merged": 0}


# ------------------------------------------------------------------ records
def build_record(cfg, page, raw_scholarship, model, discovered_via):
    """Ground the raw LLM output against the page. Returns None if even the name is unsupported."""
    fields, evidence, rejected = extraction.ground(raw_scholarship, page)
    if fields["name"] == NOT_SPECIFIED or not discovery.is_proper_name(fields["name"]):
        return None
    # Only scholarships for Indian students: an Indian site, or a page that talks about India
    if not sources.host_of(page["url"]).endswith(".in") and "india" not in page["text"].lower():
        return None
    provider = "" if fields["provider"] == NOT_SPECIFIED else fields["provider"]
    source = sources.classify(cfg, page["url"], provider)
    record = dict(fields)
    record.update({
        "id": discovery.slug(fields["name"]) + "@" + sources.registered_domain(page["url"]),
        "evidence": evidence, "rejected": rejected,
        "source_type": source["type"], "is_official": source["official"], "source_reason": source["reason"],
        "official_source_url": page["url"], "extracted_by": model, "page_hash": page["hash"],
        "discovered_via": discovered_via, "fail_count": 0,
    })
    return record


def values_same(field, old, new):
    if old == new:
        return True
    if field in DATE_FIELDS + NUMBER_FIELDS + URL_FIELDS:
        return False
    # Different numbers (amounts, percentages, ages) always count as a change
    if re.findall(r"\d+", old.replace(",", "")) != re.findall(r"\d+", new.replace(",", "")):
        return False
    return SequenceMatcher(None, grounding.normalize(old), grounding.normalize(new)).ratio() >= 0.85


def find_conflicts(conn, record):
    """Other sources that give a different deadline for the same scholarship in the same cycle."""
    if record["closing_date"] == NOT_SPECIFIED:
        return []
    mine = date.fromisoformat(record["closing_date"])
    key = record["id"].split("@")[0]
    others = storage.fetch(conn, "SELECT closing_date, found_on_url AS url FROM leads WHERE slug = ? AND closing_date IS NOT NULL", (key,))
    others += storage.fetch(conn, "SELECT closing_date, official_source_url AS url FROM scholarships WHERE id LIKE ? AND id != ?", (key + "@%", record["id"]))
    conflicts = []
    for other in others:
        try:
            theirs = date.fromisoformat(other["closing_date"])
        except (TypeError, ValueError):
            continue
        # A gap of more than 200 days is last year's cycle, not a contradiction
        if 0 < abs((theirs - mine).days) <= 200:
            conflicts.append(other["url"] + " gives the deadline as " + other["closing_date"])
    return conflicts


def merge_with_previous(old, record, page, lost=None):
    """Compare a fresh extraction with the stored record. Returns the list of real changes.

    If the LLM simply left a field out but the old quote is still on the page,
    the old value is kept: that is not a change.
    """
    changes = []
    for field in ALL_FIELDS:
        if field == "name":
            continue
        old_value, new_value = old[field], record[field]
        if values_same(field, old_value, new_value):
            continue
        old_item = old["evidence"].get(field, {})
        if new_value == NOT_SPECIFIED:
            if lost is not None:
                still_there = field not in lost
            elif old_item.get("source_url") != page["url"]:
                still_there = True          # backed by another page, which this extraction says nothing about
            else:
                still_there = still_supported(field, old_value, old_item, page)
            if still_there and old_item.get("verified"):
                record[field] = old_value
                record["evidence"][field] = dict(old_item)
                continue
        changes.append({"field": field, "old_value": old_value, "new_value": new_value,
                        "evidence": record["evidence"][field]["quote"] or record["evidence"][field]["note"],
                        "old_evidence": old_item.get("quote", "")})
    return changes


def assess_and_save(cfg, conn, record, page, counters, changes=(), old=None, log_status=True):
    """Score the record, set its status, save it, and log status/field changes."""
    facts = {
        "page_live": page["status"] == 200,
        "name_on_page": grounding.normalize(record["name"]) in grounding.normalize(page["text"])
                        or grounding.quote_on_page(record["evidence"]["name"]["quote"], page["text"]),
        "page_text": page["text"],
        "application_status": fetcher.url_status(cfg, record["application_url"]) if record["application_url"] != NOT_SPECIFIED else 0,
        "conflicts": find_conflicts(conn, record),
    }
    confidence, checks = scoring.score_record(cfg, record, facts, today())
    record["confidence"] = confidence
    record["verification"] = scoring.verification_label(cfg, record, confidence, facts)
    record["status"] = "REVIEW_REQUIRED" if facts["conflicts"] else scoring.lifecycle_status(cfg, record, today())

    now = now_text()
    record["last_checked"] = now
    record["last_verified"] = now
    record["first_seen"] = old["first_seen"] if old else now
    record["last_changed"] = now if changes else (old["last_changed"] if old else now)

    for change in changes:
        storage.add_change(conn, record["id"], change["field"], change["old_value"], change["new_value"],
                           page["url"], change["evidence"], old["official_source_url"], change["old_evidence"])
    if old and old["status"] != record["status"] and log_status:
        reason = "closing date " + record["closing_date"] + " compared with today " + today().isoformat()
        if facts["conflicts"]:
            reason = "; ".join(facts["conflicts"])
        storage.add_change(conn, record["id"], "status", old["status"], record["status"], page["url"], reason)
        if record["status"] == "EXPIRED":
            counters["expired"] += 1

    storage.save_record(conn, record, checks)
    if not old or changes:
        storage.add_version(conn, record, "live")

    if record["id"] in seen_this_run and not changes:
        return
    seen_this_run.add(record["id"])
    if not old:
        counters["new"] += 1
        log.info("NEW        %s  [%s, %.1f%%, %s]", record["name"], record["verification"], confidence, record["status"])
    elif changes:
        counters["changed"] += 1
        counters["field_changes"] += len(changes)
        for change in changes:
            log.info("CHANGED    %s: %s  '%s' -> '%s'", record["name"], change["field"], change["old_value"][:60], change["new_value"][:60])
    else:
        counters["unchanged"] += 1
        log.info("UNCHANGED  %s  [%.1f%%, %s]", record["name"], confidence, record["status"])


def mark_unverifiable(conn, record, reason, counters):
    """The official page is gone or no longer lists the scholarship. Old data is kept, not deleted."""
    if record["status"] != "NO_LONGER_VERIFIABLE":
        storage.add_change(conn, record["id"], "status", record["status"], "NO_LONGER_VERIFIABLE",
                           record["official_source_url"], reason)
        counters["no_longer_verifiable"] += 1
    conn.execute("UPDATE scholarships SET status = 'NO_LONGER_VERIFIABLE', verification = 'REVIEW REQUIRED', "
                 "last_checked = ?, fail_count = ? WHERE id = ?", (now_text(), record["fail_count"], record["id"]))
    conn.execute("UPDATE score_checks SET points = 0, reason = ? WHERE scholarship_id = ? AND name = 'live_on_source'",
                 (reason, record["id"]))
    total = storage.count(conn, "SELECT COALESCE(SUM(points), 0) FROM score_checks WHERE scholarship_id = ?", (record["id"],))
    conn.execute("UPDATE scholarships SET confidence = ? WHERE id = ?", (round(total, 1), record["id"]))
    conn.commit()
    log.info("NO LONGER VERIFIABLE  %s  (%s)", record["name"], reason)


def best_match(raw_scholarships, name):
    """The extracted scholarship whose name is closest to the one we are re-checking."""
    wanted = discovery.slug(name)
    best, best_ratio = None, 0.75
    for raw in raw_scholarships:
        item = raw.get("name") or {}
        found = discovery.slug(item.get("value") if isinstance(item, dict) else item)
        ratio = SequenceMatcher(None, wanted, found).ratio()
        if ratio >= best_ratio:
            best, best_ratio = raw, ratio
    return best


# ------------------------------------------------------------------ step 1: re-verify
def reverify_existing(cfg, conn, counters):
    """Visit the official page of every stored scholarship again."""
    ids = [row["id"] for row in storage.fetch(conn, "SELECT id FROM scholarships ORDER BY last_checked")]
    for scholarship_id in ids:
        old = storage.load_record(conn, scholarship_id)
        page = fetcher.fetch(cfg, old["official_source_url"])
        counters["pages_fetched"] += 1

        if page["status"] != 200:
            old["fail_count"] = (old["fail_count"] or 0) + 1
            gone = page["status"] in (404, 410) or old["fail_count"] >= cfg["status"]["failures_before_unverifiable"]
            if gone:
                mark_unverifiable(conn, old, "official page could not be loaded (status " + str(page["status"]) + " " + page["error"] + ")", counters)
            else:
                conn.execute("UPDATE scholarships SET fail_count = ?, last_checked = ? WHERE id = ?", (old["fail_count"], now_text(), scholarship_id))
                conn.commit()
            continue

        name_there = grounding.normalize(old["name"]) in grounding.normalize(page["text"]) \
            or grounding.quote_on_page(old["evidence"]["name"]["quote"], page["text"])
        if not name_there:
            mark_unverifiable(conn, old, "the scholarship no longer appears on its official page", counters)
            continue

        record = dict(old, evidence={f: dict(item) for f, item in old["evidence"].items()}, fail_count=0, page_hash=page["hash"])
        # Is every stored quote still on the page it came from?
        lost = [f for f, item in old["evidence"].items() if item["verified"]
                and not still_supported(f, old[f], item, supporting_page(cfg, item["source_url"], page))]
        unchanged = page["hash"] == old["page_hash"]
        missing = [f for f in IMPORTANT_FIELDS if old[f] == NOT_SPECIFIED]
        if not lost and (unchanged or not missing):
            assess_and_save(cfg, conn, record, page, counters, old=old)
            continue

        fresh = None
        if not unchanged and counters["extractions"] < cfg["crawl"]["max_extractions_per_run"]:
            counters["extractions"] += 1
            raw, model = extraction.extract(cfg, page)
            if raw:
                conn.execute("UPDATE pages SET extracted_hash = ?, raw_extraction = ?, extracted_by = ? WHERE url = ?",
                             (page["hash"], json.dumps(raw, ensure_ascii=False), model, page["url"]))
            match = best_match(raw["scholarships"], old["name"]) if raw else None
            if match:
                fresh = build_record(cfg, page, match, model, old["discovered_via"])
        if fresh:
            fresh["id"], fresh["name"] = old["id"], old["name"]
            fresh["evidence"]["name"] = dict(old["evidence"]["name"], source_url=page["url"])
            changes = merge_with_previous(old, fresh, page, lost)
            assess_and_save(cfg, conn, fresh, page, counters, changes, old)
        else:
            # Could not re-extract: withdraw only the values whose quotes disappeared
            changes = []
            for field in lost:
                changes.append({"field": field, "old_value": old[field], "new_value": NOT_SPECIFIED,
                                "evidence": "the supporting quote is no longer on " + old["evidence"][field]["source_url"],
                                "old_evidence": old["evidence"][field]["quote"]})
                record[field] = NOT_SPECIFIED
                record["evidence"][field] = {"value": NOT_SPECIFIED, "quote": "", "verified": False,
                                             "note": "withdrawn: quote no longer on the page", "source_url": page["url"]}
            assess_and_save(cfg, conn, record, page, counters, changes, old)


def share_page_apply_link(records, page):
    """On a listing page the extractor often names the apply link for some schemes only.

    When every link it found is the same one, that link belongs to the page and is
    given to the schemes it was left out of, with the evidence saying so.
    """
    links = {r["application_url"] for r in records if r["application_url"] != NOT_SPECIFIED}
    if len(links) > 1:
        # Several links were named: keep the one whose address says it is the application form
        links = {link for link in links if re.search(r"(applicationform|apply)", link.lower())}
    if len(links) != 1:
        return
    link = links.pop()
    for record in records:
        if record["application_url"] == NOT_SPECIFIED:
            record["application_url"] = link
            record["evidence"]["application_url"] = {"value": link, "quote": link, "verified": True, "source_url": page["url"],
                                                     "note": "the application link this page gives for the other schemes listed on it"}


def store_extracted(cfg, conn, page, raw, model, via, counters):
    """Turn one page's raw extraction into grounded, scored records (or leads)."""
    records = []
    for raw_scholarship in raw["scholarships"]:
        if isinstance(raw_scholarship, dict):
            record = build_record(cfg, page, raw_scholarship, model, via)
            if record is not None:
                records.append(record)
    share_page_apply_link(records, page)
    for record in records:
        if not record["is_official"]:
            # Not the primary source: remember the name (and its deadline, for conflict checks) and look for the official page later
            closing = None if record["closing_date"] == NOT_SPECIFIED else record["closing_date"]
            key = discovery.slug(record["name"])
            old_lead = storage.fetch(conn, "SELECT state, resolved_url FROM leads WHERE slug = ?", (key,))
            has_official = storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE id LIKE ?", (key + "@%",))
            storage.insert(conn, "leads", {
                "slug": key, "name": record["name"], "found_on_url": page["url"], "found_at": now_text(),
                "closing_date": closing, "resolved_url": old_lead[0]["resolved_url"] if old_lead else None,
                "state": old_lead[0]["state"] if old_lead else ("secondary" if has_official else "new")}, "INSERT OR REPLACE")
            counters["leads_found"] += 0 if old_lead else 1
            log.info("LEAD       %s  (%s)", record["name"], record["source_reason"])
            continue
        old = storage.load_record(conn, record["id"])
        if old and old["official_source_url"] != page["url"]:
            # The same scholarship on another official page. The page with the deadline becomes the main
            # source and the other one fills its gaps. This is not a change in the scholarship itself.
            if rank(record) > rank(old):
                fill_gaps(record, old)
                log.info("BETTER SOURCE  %s: %s", record["name"], page["url"])
                seen_this_run.discard(record["id"])
                assess_and_save(cfg, conn, record, page, counters, [], old, log_status=False)
                storage.add_version(conn, record, "live")
            else:
                merged = dict(old, evidence={f: dict(item) for f, item in old["evidence"].items()})
                filled = fill_gaps(merged, record)
                if filled:
                    log.info("ADDED DETAIL   %s: %s from %s", old["name"], ", ".join(filled), page["url"])
                    main_page = supporting_page(cfg, old["official_source_url"], page)
                    if main_page["status"] == 200:
                        assess_and_save(cfg, conn, merged, main_page, counters, [], old, log_status=False)
            continue
        changes = merge_with_previous(old, record, page) if old else []
        assess_and_save(cfg, conn, record, page, counters, changes, old)


def consolidate(cfg, conn, counters):
    """Combine records that describe the same scholarship on different official pages.

    Example: a portal's listing gives the deadline and the apply link, while the scheme's
    guidelines PDF gives the eligibility. The page with the deadline stays as the record;
    the other page's fields are added to it, each with its own source URL and quote.
    """
    rows = storage.fetch(conn, "SELECT id FROM scholarships WHERE status != 'NO_LONGER_VERIFIABLE' AND is_official = 1")
    records = [storage.load_record(conn, row["id"]) for row in rows]
    records.sort(key=rank, reverse=True)
    used = set()
    for primary in records:
        if primary["id"] in used:
            continue
        used.add(primary["id"])
        partners = [r for r in records if r["id"] not in used and discovery.same_scholarship(
            primary["name"], r["name"], same_site=r["id"].split("@")[1] == primary["id"].split("@")[1])]
        if not partners:
            continue
        before = dict(primary, evidence={f: dict(item) for f, item in primary["evidence"].items()})
        for partner in partners:
            used.add(partner["id"])
            filled = fill_gaps(primary, partner)
            log.info("MERGED     %s  <-  %s (%s)", primary["name"], partner["official_source_url"], ", ".join(filled) or "nothing new")
            # The other page is remembered as a secondary source; its deadline is used for conflict checks
            closing = None if partner["closing_date"] == NOT_SPECIFIED else partner["closing_date"]
            storage.insert(conn, "leads", {"slug": primary["id"].split("@")[0], "name": partner["name"],
                                           "found_on_url": partner["official_source_url"], "found_at": now_text(),
                                           "closing_date": closing, "resolved_url": primary["official_source_url"],
                                           "state": "secondary"}, "INSERT OR REPLACE")
            for table, column in (("scholarships", "id"), ("evidence", "scholarship_id"), ("score_checks", "scholarship_id")):
                conn.execute("DELETE FROM " + table + " WHERE " + column + " = ?", (partner["id"],))
            conn.commit()
            counters["merged"] += 1
        page = supporting_page(cfg, primary["official_source_url"], {"url": ""})
        if page["status"] == 200:
            assess_and_save(cfg, conn, primary, page, counters, [], before, log_status=False)


# ------------------------------------------------------------------ step 2: discover
def discover_new(cfg, conn, counters):
    """Crawl outward from seeds, search results, sitemaps and leads, and extract new scholarships."""
    limits = cfg["crawl"]
    frontier = []
    queued = set()
    # Pages that already gave us records: what they link to (guidelines, notifications) explains those schemes
    record_pages = {row["official_source_url"] for row in storage.fetch(conn, "SELECT official_source_url FROM scholarships")}

    def enqueue(url, depth, via, priority):
        if url not in queued:
            queued.add(url)
            frontier.append({"url": url, "depth": depth, "via": via, "priority": priority})

    for url, via in discovery.resolve_leads(cfg, conn):
        enqueue(url, 1, via, 9)
    # Official pages found for earlier leads (visited again; unchanged pages are not re-extracted)
    for lead in storage.fetch(conn, "SELECT name, resolved_url FROM leads WHERE state = 'resolved' AND resolved_url != ''"):
        enqueue(lead["resolved_url"], 1, "lead '" + lead["name"] + "' resolved earlier", 9)
    for url in cfg["seeds"]["official"]:
        enqueue(url, 0, "seed", 10)
        for found in discovery.sitemap_links(cfg, url):
            enqueue(found, 1, "sitemap of " + sources.host_of(url), 7)
    for url in cfg["seeds"]["discovery_only"]:
        enqueue(url, 0, "seed (discovery only)", 6)
    if cfg["search"]["enabled"]:
        for query in discovery.deadline_queries(today()) + cfg["search"]["queries"]:
            for url, _title in discovery.search_web(cfg, query):
                enqueue(url, 1, "web search: " + query, 5 if sources.is_aggregator(cfg, url) else 8)

    def crawl_frontier():
        while frontier and counters["pages_fetched"] < limits["max_pages_per_run"]:
            frontier.sort(key=lambda item: item["priority"])
            item = frontier.pop()
            page = fetcher.fetch(cfg, item["url"])
            counters["pages_fetched"] += 1
            if page["status"] != 200 or not page["text"]:
                continue

            if sources.is_aggregator(cfg, page["url"]):
                counters["leads_found"] += discovery.collect_leads(conn, page)
                storage.save_page(conn, page, item["depth"], item["via"], 0, sources.registered_domain(page["url"]))
                continue

            score = extraction.page_score(page)
            storage.save_page(conn, page, item["depth"], item["via"], score, sources.registered_domain(page["url"]))

            if item["depth"] < limits["max_depth"]:
                bonus = 3 if page["url"] in record_pages else 0
                for url, label in page["links"]:
                    priority = discovery.link_priority(url, label)
                    if priority and not sources.is_aggregator(cfg, url):
                        enqueue(url, item["depth"] + 1, "link from " + page["url"], priority - item["depth"] + bonus)

            already = storage.count(conn, "SELECT COUNT(*) FROM pages WHERE url = ? AND extracted_hash = ?", (page["url"], page["hash"]))
            if score < limits["min_page_score"] or already or counters["extractions"] >= limits["max_extractions_per_run"]:
                continue
            # Most of the LLM budget is kept for pages that state a deadline: only those can be fully verified
            explains_a_record = item["via"].startswith("link from ") and item["via"][10:] in record_pages
            if not extraction.rule_deadlines(page["text"]) and not item["via"].startswith("lead") and not explains_a_record:
                if counters["no_deadline_extractions"] >= limits["max_extractions_without_deadline"]:
                    continue
                counters["no_deadline_extractions"] += 1

            counters["extractions"] += 1
            raw, model = extraction.extract(cfg, page)
            if raw is None:
                counters["llm_failures"] += 1
                if counters["llm_failures"] >= 8 and counters["llm_failures"] > counters["extractions"] * 0.8:
                    log.error("The LLM is failing on almost every page. Stopping discovery; run `python main.py check`.")
                    break
                continue
            conn.execute("UPDATE pages SET extracted_hash = ?, raw_extraction = ?, extracted_by = ? WHERE url = ?",
                         (page["hash"], json.dumps(raw, ensure_ascii=False), model, page["url"]))
            conn.commit()
            store_extracted(cfg, conn, page, raw, model, item["via"], counters)

    crawl_frontier()
    # Names learned from aggregators in this run: find their official pages and crawl those too.
    # A share of the page budget is kept back for them.
    for url, via in discovery.resolve_leads(cfg, conn):
        enqueue(url, 1, via, 9)
    limits["max_pages_per_run"] += len(frontier)
    crawl_frontier()


# ------------------------------------------------------------------ the full run
def run(cfg, conn, discover=True):
    counters = new_counters()
    seen_this_run.clear()
    page_cache.clear()
    started = now_text()
    log.info("=== run started: re-verifying %d stored scholarships ===", storage.count(conn, "SELECT COUNT(*) FROM scholarships"))
    reverify_existing(cfg, conn, counters)
    if discover:
        log.info("=== discovering new scholarships ===")
        discover_new(cfg, conn, counters)
    consolidate(cfg, conn, counters)
    storage.insert(conn, "runs", {"started_at": started, "finished_at": now_text(), "summary": json.dumps(counters)})
    log.info("=== run finished: %s ===", counters)
    return counters


# ------------------------------------------------------------------ rebuild without the LLM
def reground(cfg, conn):
    """Rebuild every record from the saved LLM answers, without calling the LLM again.

    Each page is downloaded again and the saved answer is re-checked against it, so
    improved evidence checks or scoring can be applied without using any LLM quota.
    Records, versions and the change log are rebuilt from scratch.
    """
    counters = new_counters()
    seen_this_run.clear()
    page_cache.clear()
    for table in ("scholarships", "evidence", "score_checks", "versions", "changes"):
        conn.execute("DELETE FROM " + table)
    conn.commit()
    rows = storage.fetch(conn, "SELECT * FROM pages WHERE raw_extraction IS NOT NULL ORDER BY fetched_at")
    done_urls = set()
    for row in rows:
        if fetcher.tidy_url(row["url"]) in done_urls:      # the same page saved under two spellings
            conn.execute("DELETE FROM pages WHERE url = ?", (row["url"],))
            conn.commit()
            continue
        done_urls.add(fetcher.tidy_url(row["url"]))
        page = fetcher.fetch(cfg, row["url"])
        counters["pages_fetched"] += 1
        if page["status"] != 200 or page["hash"] != row["extracted_hash"]:
            # The page changed or is unreachable: read it again with the LLM on the next normal run
            conn.execute("UPDATE pages SET extracted_hash = NULL WHERE url = ?", (row["url"],))
            conn.commit()
            continue
        raw = json.loads(row["raw_extraction"])
        store_extracted(cfg, conn, page, raw, row["extracted_by"], row["discovered_via"], counters)
        if len(raw["scholarships"]) == 3:
            # Read with the older limit of 3 scholarships per page: let the next run read it again in full
            conn.execute("UPDATE pages SET extracted_hash = NULL WHERE url = ?", (row["url"],))
            conn.commit()
    consolidate(cfg, conn, counters)
    log.info("=== reground finished: %s ===", counters)
    return counters


# ------------------------------------------------------------------ history from the Internet Archive
def backfill_history(cfg, conn, limit=10):
    """Compare each scholarship with an OLDER copy of its official page from the Internet Archive.

    This shows real changes (for example last year's deadline vs this year's)
    without waiting months between runs. Archived values go through the same
    evidence checks, and every change is labelled with the archive URL and date.
    """
    settings = cfg["history"]
    target = (today() - timedelta(days=settings["snapshot_age_days"])).strftime("%Y%m%d")
    done = 0
    rows = storage.fetch(conn, "SELECT id FROM scholarships WHERE status != 'NO_LONGER_VERIFIABLE' AND id NOT IN "
                               "(SELECT scholarship_id FROM versions WHERE origin = 'archive') ORDER BY confidence DESC")
    for row in rows:
        if done >= limit:
            break
        current = storage.load_record(conn, row["id"])
        lookup = fetcher.http_get(cfg, "https://archive.org/wayback/available?url=" + current["official_source_url"] + "&timestamp=" + target)
        try:
            snapshot = json.loads(lookup["text"])["archived_snapshots"]["closest"]
            stamp = snapshot["timestamp"]
        except (ValueError, KeyError, TypeError):
            continue
        taken = datetime.strptime(stamp[:8], "%Y%m%d").date()
        if (today() - taken).days < settings["min_gap_days"]:
            continue

        archive_url = "https://web.archive.org/web/" + stamp + "id_/" + current["official_source_url"]
        answer = fetcher.http_get(cfg, archive_url)
        if answer["status"] != 200:
            continue
        title, text, links = fetcher.read_html(answer["text"], current["official_source_url"])
        old_page = {"url": archive_url, "status": 200, "title": title, "text": text, "links": links, "hash": "", "is_pdf": False}
        raw, model = extraction.extract(cfg, old_page)
        match = best_match(raw["scholarships"], current["name"]) if raw else None
        if not match:
            continue
        fields, evidence, _rejected = extraction.ground(match, old_page)
        done += 1

        archived = dict(current, **fields)
        archived["evidence"] = evidence
        storage.add_version(conn, archived, "archive", taken.isoformat() + "T00:00:00Z", archive_url)
        for field in ALL_FIELDS:
            if field == "name" or not (evidence[field]["verified"] and current["evidence"][field]["verified"]):
                continue
            if values_same(field, fields[field], current[field]):
                continue
            storage.add_change(conn, current["id"], field, fields[field], current[field], current["official_source_url"],
                               current["evidence"][field]["quote"], archive_url, evidence[field]["quote"],
                               "old value read from the Internet Archive copy of the official page dated " + taken.isoformat())
            log.info("CHANGE DETECTED  %s: %s  '%s' (%s) -> '%s' (now)", current["name"], field, fields[field][:50], taken.isoformat(), current[field][:50])
    return done
