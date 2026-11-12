"""Tests. Run with: python -m pytest -q

Everything here uses a FAKE mini-internet (made-up pages served from memory) and
a fake LLM, so the tests need no keys and no network. None of it touches ./data.
"""
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler import discovery, export, extraction, fetcher, grounding, llm, pipeline, scoring, sources, storage
from crawler.config import NOT_SPECIFIED, load_config, today

GOV = "https://scholar.example.gov.in"
FILLER = "<p>" + "Students are advised to read the guidelines carefully before applying. " * 12 + "</p>"


def long_date(days_from_today):
    return (today() + timedelta(days=days_from_today)).strftime("%d %B %Y")


def scholarship_page(name, provider, amount="Rs. 50,000", deadline=None, apply_link=GOV + "/apply", income="Rs. 8 lakh"):
    deadline_line = f"<p>Last date to apply: {deadline}.</p>" if deadline else ""
    return f"""<html><head><title>{name}</title></head><body><main>
    <h1>{name}</h1>
    <p>Offered by {provider}.</p>
    <p>Scholarship amount: {amount} per annum.</p>
    <p>Eligibility: Indian students enrolled in the first year of an undergraduate degree.</p>
    <p>Annual family income must not exceed {income}.</p>
    {deadline_line}
    <a href="{apply_link}">Apply online</a>
    {FILLER}</main></body></html>"""


def build_web():
    """A tiny fake internet: url -> (status, html)."""
    hub = f"""<html><head><title>Student schemes</title></head><body>
    <a href="{GOV}/schemes/merit-scholarship">National Merit Scholarship</a>
    <a href="{GOV}/schemes/girls-fellowship">Fellowship for Girls</a>
    <a href="{GOV}/schemes/old-scholarship">Heritage Scholarship</a>
    <a href="{GOV}/tenders">Tenders</a></body></html>"""
    aggregator = """<html><body><a href="https://www.buddy4study.com/page/acme">Acme Foundation Scholarship 2026</a></body></html>"""
    return {
        GOV + "/": (200, hub),
        GOV + "/apply": (200, "<html><body>Application portal</body></html>"),
        GOV + "/schemes/merit-scholarship": (200, scholarship_page("National Merit Scholarship", "Ministry of Education", deadline=long_date(60))),
        GOV + "/schemes/girls-fellowship": (200, scholarship_page("Fellowship for Girls", "Ministry of Education")),
        GOV + "/schemes/old-scholarship": (200, scholarship_page("Heritage Scholarship", "Ministry of Education", deadline=long_date(-60))),
        "https://www.buddy4study.com/scholarships": (200, aggregator),
        "https://www.acmefoundation.org/scholarship": (200, scholarship_page(
            "Acme Foundation Scholarship", "Acme Foundation", deadline=long_date(10), apply_link="https://www.acmefoundation.org/apply")),
        "https://www.acmefoundation.org/apply": (200, "<html><body>Apply</body></html>"),
        "https://college.example.ac.in/notice": (200, scholarship_page(
            "State Topper Scholarship", "Department of Higher Education", deadline=long_date(30))),
    }


def fake_llm_answer(user_prompt, invent_income=False):
    """Pretends to be the LLM: reads the fixture page honestly (or invents a value on request)."""
    text = user_prompt.split("Page text:\n", 1)[1]

    def line_with(word):
        for line in text.split("\n"):
            if word.lower() in line.lower():
                return line
        return ""

    name = text.split("\n")[0]
    record = {"name": {"value": name, "evidence": name}}
    provider = line_with("Offered by")
    if provider:
        record["provider"] = {"value": provider.replace("Offered by ", "").rstrip("."), "evidence": provider}
    amount = line_with("Scholarship amount")
    if amount:
        record["amount"] = {"value": amount.replace("Scholarship amount: ", ""), "evidence": amount}
    eligibility = line_with("Eligibility:")
    if eligibility:
        record["eligibility"] = {"value": eligibility.replace("Eligibility: ", ""), "evidence": eligibility}
        record["education_level"] = {"value": "first year of an undergraduate degree", "evidence": eligibility}
    income = line_with("Annual family income")
    if income:
        record["income_criteria"] = {"value": income, "evidence": income}
        record["income_limit_inr"] = {"value": grounding.find_amounts(income)[-1], "evidence": income}
    deadline = line_with("Last date")
    if deadline:
        record["closing_date"] = {"value": grounding.find_dates(deadline)[0].isoformat(), "evidence": deadline}
    link = re.search(r"- Apply online -> (\S+)", user_prompt)
    if link:
        record["application_url"] = {"value": link.group(1), "evidence": "Apply online"}
    if invent_income:
        record["age_criteria"] = {"value": "Below 25 years", "evidence": "Applicants must be below 25 years of age"}
        record["opening_date"] = {"value": "2020-01-01", "evidence": deadline}
    return {"page_type": "scholarship_detail", "scholarships": [record]}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    web = build_web()
    searches = {"acme": [("https://www.buddy4study.com/page/acme", "Acme"), ("https://www.acmefoundation.org/scholarship", "Acme Foundation")]}

    def fake_http_get(cfg, url):
        status, html = web.get(url, (404, ""))
        return {"status": status, "content_type": "text/html", "content": html.encode(), "text": html, "final_url": url, "error": ""}

    def fake_search(cfg, query):
        for word, results in searches.items():
            if word in query.lower():
                return results
        return []

    monkeypatch.setattr(fetcher, "http_get", fake_http_get)
    monkeypatch.setattr(discovery, "search_web", fake_search)
    monkeypatch.setattr(llm, "is_available", lambda cfg: True)
    monkeypatch.setattr(llm, "ask_llm", lambda cfg, system, user: (fake_llm_answer(user), "fake/model"))
    fetcher.robots_cache.clear()
    fetcher.status_cache.clear()

    cfg = load_config(tmp_path)
    cfg["seeds"] = {"official": [GOV + "/", "https://college.example.ac.in/notice"],
                    "discovery_only": ["https://www.buddy4study.com/scholarships"]}
    cfg["search"]["queries"] = []
    cfg["crawl"]["delay_seconds"] = 0
    cfg["llm"]["seconds_between_calls"] = 0
    conn = storage.connect(cfg["paths"]["db"])
    yield cfg, conn, web
    conn.close()


def get(conn, name):
    return storage.fetch(conn, "SELECT * FROM scholarships WHERE name = ?", (name,))[0]


def test_dates_and_amounts_are_parsed():
    found = grounding.find_dates("Last date: 31st August, 2026. Extended to 15-09-2026 or Sept 20, 2026 (2026-10-01)")
    assert [d.isoformat() for d in found] == ["2026-08-31", "2026-09-20", "2026-09-15", "2026-10-01"]
    assert 250000 in grounding.find_amounts("income below Rs. 2.5 lakh")
    assert 800000 in grounding.find_amounts("up to ₹8,00,000 per annum")


def test_invented_values_are_rejected():
    page_text = "Pragati Scholarship\nLast date to apply: 31 August 2026.\nIncome must not exceed Rs. 8 lakh."
    ok = grounding.check_field("closing_date", "2026-08-31", "Last date to apply: 31 August 2026.", page_text)
    assert ok[0] == "2026-08-31" and ok[1] is True
    # quote is not on the page
    assert grounding.check_field("income_criteria", "Up to Rs 5 lakh", "Family income up to Rs 5 lakh", page_text)[0] == NOT_SPECIFIED
    # quote is on the page but does not support the value
    assert grounding.check_field("closing_date", "2026-09-30", "Last date to apply: 31 August 2026.", page_text)[0] == NOT_SPECIFIED
    assert grounding.check_field("income_limit_inr", 500000, "Income must not exceed Rs. 8 lakh.", page_text)[0] == NOT_SPECIFIED
    assert grounding.check_field("income_limit_inr", 800000, "Income must not exceed Rs. 8 lakh.", page_text)[0] == "800000"
    assert grounding.check_field("age_criteria", None, "", page_text)[0] == NOT_SPECIFIED


def test_source_classification(world):
    cfg, _conn, _web = world
    assert sources.classify(cfg, "https://aicte.gov.in/x", "AICTE")["type"] == "government"
    assert sources.classify(cfg, "https://www.buddy4study.com/x", "Anyone")["official"] is False
    assert sources.classify(cfg, "https://scholarships.reliancefoundation.org/", "Reliance Foundation")["type"] == "foundation_trust"
    assert sources.classify(cfg, "https://nitkkr.ac.in/x", "National Institute of Technology Kurukshetra")["official"] is True
    repost = sources.classify(cfg, "https://www.thapar.edu/notice.pdf", "All India Council for Technical Education")
    assert repost["official"] is False
    assert sources.classify(cfg, "https://nitkkr.ac.in/scholarship-notifications", "")["official"] is False
    assert sources.classify(cfg, "https://nitkkr.ac.in/x", "NIT Kurukshetra")["official"] is True
    assert sources.classify(cfg, "https://randomblog.com/x", "Tata Trusts")["official"] is False


def test_first_run_stores_only_officially_sourced_records(world):
    cfg, conn, _web = world
    pipeline.run(cfg, conn)

    merit = get(conn, "National Merit Scholarship")
    assert merit["verification"] == "VERIFIED" and merit["confidence"] >= 95 and merit["status"] == "ACTIVE"
    assert merit["income_limit_inr"] == "800000" and merit["source_type"] == "government"

    girls = get(conn, "Fellowship for Girls")
    assert girls["closing_date"] == NOT_SPECIFIED
    assert girls["verification"] == "REVIEW REQUIRED" and girls["status"] == "REVIEW_REQUIRED"

    assert get(conn, "Heritage Scholarship")["status"] == "EXPIRED"

    acme = get(conn, "Acme Foundation Scholarship")
    assert acme["official_source_url"] == "https://www.acmefoundation.org/scholarship"
    assert acme["source_type"] == "foundation_trust" and acme["status"] == "EXPIRING_SOON"
    assert "buddy4study" in acme["discovered_via"]

    # the aggregator page and the college re-post are never stored as records
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE official_source_url LIKE '%buddy4study%'") == 0
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE name = 'State Topper Scholarship'") == 0
    assert storage.count(conn, "SELECT COUNT(*) FROM leads WHERE name = 'State Topper Scholarship'") == 1
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships") == 4


def test_every_stored_value_traces_to_a_quote_on_the_page(world):
    cfg, conn, web = world
    pipeline.run(cfg, conn)
    rows = storage.fetch(conn, "SELECT * FROM evidence WHERE verified = 1 AND field != 'application_url'")
    assert len(rows) > 20
    for row in rows:
        html = web[row["source_url"]][1]
        _title, text, _links = fetcher.read_html(html, row["source_url"])
        assert grounding.quote_on_page(row["quote"], text), row


def test_hallucinated_fields_never_reach_the_database(world, monkeypatch):
    cfg, conn, _web = world
    monkeypatch.setattr(llm, "ask_llm", lambda cfg, system, user: (fake_llm_answer(user, invent_income=True), "fake/model"))
    pipeline.run(cfg, conn)
    merit = get(conn, "National Merit Scholarship")
    assert merit["age_criteria"] == NOT_SPECIFIED and merit["opening_date"] == NOT_SPECIFIED
    assert len(json.loads(merit["rejected"])) == 2
    # being caught inventing values costs points, so it is no longer auto-verified at 100
    assert merit["confidence"] < 100


def test_confidence_is_the_sum_of_its_checks(world):
    cfg, conn, _web = world
    pipeline.run(cfg, conn)
    for row in storage.fetch(conn, "SELECT id, confidence FROM scholarships"):
        total = storage.count(conn, "SELECT SUM(points) FROM score_checks WHERE scholarship_id = ?", (row["id"],))
        assert abs(total - row["confidence"]) < 0.11
        assert storage.count(conn, "SELECT COUNT(*) FROM score_checks WHERE scholarship_id = ?", (row["id"],)) == 9
    assert sum(cfg["scoring"]["weights"].values()) == 100


def test_second_run_detects_changes_removals_and_new(world):
    cfg, conn, web = world
    pipeline.run(cfg, conn)

    web[GOV + "/schemes/merit-scholarship"] = (200, scholarship_page("National Merit Scholarship", "Ministry of Education", deadline=long_date(75)))
    web[GOV + "/schemes/old-scholarship"] = (404, "")
    web[GOV + "/schemes/new-scholarship"] = (200, scholarship_page("Young Scientist Scholarship", "Ministry of Education", deadline=long_date(40)))
    web[GOV + "/"] = (200, web[GOV + "/"][1].replace("</body>", f'<a href="{GOV}/schemes/new-scholarship">Young Scientist Scholarship</a></body>'))
    fetcher.status_cache.clear()

    counters = pipeline.run(cfg, conn)
    assert counters["new"] == 1 and counters["changed"] == 1 and counters["no_longer_verifiable"] == 1
    assert counters["unchanged"] == 2

    change = storage.fetch(conn, "SELECT * FROM changes WHERE field = 'closing_date'")[0]
    assert change["old_value"] == (today() + timedelta(days=60)).isoformat()
    assert change["new_value"] == (today() + timedelta(days=75)).isoformat()
    assert "Last date to apply" in change["evidence"] and "Last date to apply" in change["old_evidence"]
    assert change["source_url"] == GOV + "/schemes/merit-scholarship" and change["detected_at"]

    gone = get(conn, "Heritage Scholarship")
    assert gone["status"] == "NO_LONGER_VERIFIABLE" and gone["verification"] == "REVIEW REQUIRED"
    assert gone["closing_date"] != NOT_SPECIFIED          # old data is kept, not wiped
    assert storage.count(conn, "SELECT COUNT(*) FROM changes WHERE field = 'status' AND new_value = 'NO_LONGER_VERIFIABLE'") == 1
    assert storage.count(conn, "SELECT COUNT(*) FROM versions WHERE scholarship_id = ?", (get(conn, "National Merit Scholarship")["id"],)) == 2


def test_scholarship_removed_from_a_live_page_is_flagged(world):
    cfg, conn, web = world
    pipeline.run(cfg, conn)
    web[GOV + "/schemes/girls-fellowship"] = (200, "<html><body><main>This scheme has been discontinued." + FILLER + "</main></body></html>")
    pipeline.run(cfg, conn, discover=False)
    assert get(conn, "Fellowship for Girls")["status"] == "NO_LONGER_VERIFIABLE"


def test_conflicting_deadline_blocks_verification(world):
    cfg, conn, _web = world
    pipeline.run(cfg, conn)
    other = (today() + timedelta(days=90)).isoformat()
    storage.insert(conn, "leads", {"slug": "national-merit-scholarship", "name": "National Merit Scholarship",
                                   "found_on_url": "https://other.example.ac.in/notice", "closing_date": other, "state": "secondary"})
    pipeline.run(cfg, conn, discover=False)
    merit = get(conn, "National Merit Scholarship")
    assert merit["verification"] == "REVIEW REQUIRED" and merit["status"] == "REVIEW_REQUIRED" and merit["confidence"] < 95


def test_history_backfill_finds_a_real_past_change(world):
    cfg, conn, web = world
    pipeline.run(cfg, conn)
    url = GOV + "/schemes/merit-scholarship"
    old_stamp = (today() - timedelta(days=300)).strftime("%Y%m%d") + "120000"
    web["https://archive.org/wayback/available?url=" + url + "&timestamp=" + old_stamp[:8]] = (
        200, json.dumps({"archived_snapshots": {"closest": {"available": True, "timestamp": old_stamp}}}))
    web["https://web.archive.org/web/" + old_stamp + "id_/" + url] = (
        200, scholarship_page("National Merit Scholarship", "Ministry of Education", amount="Rs. 30,000", deadline=long_date(-300)))

    assert pipeline.backfill_history(cfg, conn, limit=5) == 1
    changes = {c["field"]: c for c in storage.fetch(conn, "SELECT * FROM changes WHERE note LIKE '%Internet Archive%'")}
    assert set(changes) == {"amount", "closing_date"}
    assert "30,000" in changes["amount"]["old_value"] and "50,000" in changes["amount"]["new_value"]
    assert changes["closing_date"]["old_source_url"].startswith("https://web.archive.org/web/")


def test_rule_based_extraction_works_without_an_llm(world, monkeypatch):
    cfg, conn, _web = world
    monkeypatch.setattr(llm, "is_available", lambda cfg: False)
    pipeline.run(cfg, conn)
    merit = get(conn, "National Merit Scholarship")
    assert merit["closing_date"] == (today() + timedelta(days=60)).isoformat()
    assert merit["extracted_by"] == "rules" and merit["application_url"] == GOV + "/apply"


def test_stats_and_exports(world):
    cfg, conn, _web = world
    pipeline.run(cfg, conn)
    numbers = export.stats(conn)
    assert numbers["Total discovered"] == 4 and numbers["Expired"] == 1 and numbers["Source types"] == 2
    for path in export.export_all(cfg, conn):
        assert path.exists() and path.stat().st_size > 0
    detailed = json.loads((cfg["paths"]["data"] / "scholarships.json").read_text(encoding="utf-8"))
    assert detailed[0]["why_this_score"] and detailed[0]["evidence"]


def test_link_scoring_and_slugs():
    assert discovery.link_priority("https://x.gov.in/pragati-scholarship", "Pragati Scholarship") >= 5
    assert discovery.link_priority("https://x.gov.in/tenders", "Tenders") == 0
    assert discovery.slug("Pragati Scholarship 2026-27") == discovery.slug("Pragati Scholarship 2025-26") == "pragati-scholarship"
    assert sources.registered_domain("https://www.scholarships.gov.in/a") == "scholarships.gov.in"


def test_page_is_skipped_not_guessed_when_the_llm_fails(world, monkeypatch):
    cfg, conn, _web = world

    def broken(cfg, system, user):
        raise llm.LLMError("groq/model: HTTP 400 bad request")

    monkeypatch.setattr(llm, "ask_llm", broken)
    counters = pipeline.run(cfg, conn)
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships") == 0 and counters["llm_failures"] > 0
    assert storage.count(conn, "SELECT COUNT(*) FROM pages WHERE extracted_hash IS NOT NULL") == 0   # retried next run

    monkeypatch.setattr(llm, "ask_llm", lambda cfg, system, user: (fake_llm_answer(user), "fake/model"))
    pipeline.run(cfg, conn)
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships") == 4


def test_generic_headings_are_not_scholarship_names():
    for bad in ["Scholarships", "Fellowship", "(Scholarship Division)", "NSP : National Scholarship Portal", "Scholarship Schemes"]:
        assert not discovery.is_proper_name(bad), bad
    for good in ["Pragati Scholarship", "AICTE SARSWATI SCHOLARSHIP SCHEME FOR A/Y 2026-27", "PM YASASVI Scheme"]:
        assert discovery.is_proper_name(good), good
    links = dict(fetcher.urls_in_text("Apply online at https://scholarships.gov.in. or visit www.aicte.gov.in/schemes, mail a@www.x.in"))
    assert "https://scholarships.gov.in" in links and "https://www.aicte.gov.in/schemes" in links and len(links) == 2


def test_evidence_matching_handles_real_world_text():
    page = ("NEC Merit Scholarship\nThe scholarship is open only to a student who i s a permanent resident of any North Eastern State.\n"
            "Applicants must have secured minimum 60% marks in aggregate.\nApply online through the National Scholarship Portal (NSP).\n"
            "Implemented by AICTE for meritorious girl students.\nLast date: 28 th February, 2027")
    # stray PDF spaces, fancy hyphens and "..." in the quote are tolerated
    assert grounding.quote_on_page("who is a permanent resident of any North‑Eastern State", page)
    assert grounding.quote_on_page("open only to a student ... minimum 60% marks in aggregate", page)
    assert not grounding.quote_on_page("open only to a student ... minimum 75% marks in aggregate", page)
    # an inexact quote: the verifier finds the real passage itself and stores that as evidence
    value, ok, note, quote = grounding.check_field("application_mode", "Online through National Scholarship Portal (NSP)", "apply via NSP website", page)
    assert ok and "located on the page" in note and quote == "Apply online through the National Scholarship Portal (NSP)."
    # but it cannot rescue a value the page does not support
    assert grounding.check_field("academic_requirements", "Minimum 75% marks in aggregate", "made up", page)[1] is False
    assert grounding.check_field("provider", "All India Council for Technical Education (AICTE)", "Implemented by AICTE for meritorious girl students.", page)[1]
    assert grounding.check_field("gender_criteria", "Female students", "Implemented by AICTE for meritorious girl students.", page)[1]
    assert grounding.check_field("closing_date", "2027-02-28", "Last date: 28 th February, 2027", page)[0] == "2027-02-28"
    link = grounding.check_field("application_url", "www.scholarships.gov.in", "", page, ["https://scholarships.gov.in/"])
    assert link[0] == "https://scholarships.gov.in/" and link[1]


def test_reground_rebuilds_records_without_calling_the_llm(world, monkeypatch):
    cfg, conn, _web = world
    pipeline.run(cfg, conn)
    before = storage.fetch(conn, "SELECT id, confidence, closing_date FROM scholarships ORDER BY id")

    def no_llm(cfg, system, user):
        raise AssertionError("the LLM must not be called during reground")

    monkeypatch.setattr(llm, "ask_llm", no_llm)
    pipeline.reground(cfg, conn)
    assert storage.fetch(conn, "SELECT id, confidence, closing_date FROM scholarships ORDER BY id") == before


def test_a_better_page_for_the_same_scholarship_is_not_logged_as_a_change(world):
    cfg, conn, web = world
    thin = "<html><head><title>National Merit Scholarship</title></head><body><main><h1>National Merit Scholarship</h1><p>Offered by Ministry of Education.</p><p>Eligibility: Indian students.</p><p>Last date to apply: " + long_date(60) + ".</p>" + FILLER + "</main></body></html>"
    web[GOV + "/schemes/merit-summary"] = (200, thin)
    web[GOV + "/"] = (200, web[GOV + "/"][1].replace('<a href="' + GOV + '/schemes/merit-scholarship">', '<a href="' + GOV + '/schemes/merit-summary">Merit scholarship summary</a><a href="' + GOV + '/schemes/merit-scholarship">'))
    pipeline.run(cfg, conn)
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE name = 'National Merit Scholarship'") == 1
    assert get(conn, "National Merit Scholarship")["official_source_url"] == GOV + "/schemes/merit-scholarship"
    assert storage.count(conn, "SELECT COUNT(*) FROM changes") == 0


def listing_page(name):
    return f"""<html><head><title>{name}</title></head><body><main><h1>{name}</h1>
    <p>Offered by Ministry of Education.</p><p>Scholarship amount: Rs. 20,000 per annum.</p>
    <p>Last date to apply: {long_date(45)}.</p><a href="{GOV}/apply">Apply online</a>{FILLER}</main></body></html>"""


def guidelines_page(name):
    return f"""<html><head><title>{name}</title></head><body><main><h1>{name}</h1>
    <p>Offered by Ministry of Education.</p>
    <p>Eligibility: Indian students enrolled in the first year of an undergraduate degree.</p>
    <p>Annual family income must not exceed Rs. 8 lakh.</p><p>Application guidelines are given below.</p>{FILLER}</main></body></html>"""


def test_one_record_can_be_backed_by_two_official_pages(world):
    cfg, conn, web = world
    name = "Rural Talent Scholarship"
    state_site = "https://education.otherstate.gov.in/rural-talent-guidelines"
    web[GOV + "/schemes/rural-talent"] = (200, listing_page(name))
    web[state_site] = (200, guidelines_page(name))
    cfg["seeds"]["official"] += [GOV + "/schemes/rural-talent", state_site]

    counters = pipeline.run(cfg, conn)
    assert counters["merged"] == 1
    assert storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE name = ?", (name,)) == 1
    record = get(conn, name)
    assert record["official_source_url"] == GOV + "/schemes/rural-talent"       # the page with the deadline
    assert record["eligibility"] != NOT_SPECIFIED and record["verification"] == "VERIFIED"
    sources_used = {row["field"]: row["source_url"] for row in storage.fetch(conn, "SELECT field, source_url FROM evidence WHERE scholarship_id = ? AND verified = 1", (record["id"],))}
    assert sources_used["closing_date"] == GOV + "/schemes/rural-talent" and sources_used["eligibility"] == state_site
    assert storage.count(conn, "SELECT COUNT(*) FROM changes") == 0

    # nothing changed: the next run keeps it verified and logs nothing
    counters = pipeline.run(cfg, conn)
    assert get(conn, name)["verification"] == "VERIFIED" and storage.count(conn, "SELECT COUNT(*) FROM changes") == 0

    # the guidelines page disappears: only the values it supported are withdrawn, with a change logged
    web[state_site] = (404, "")
    pipeline.run(cfg, conn, discover=False)
    record = get(conn, name)
    assert record["eligibility"] == NOT_SPECIFIED and record["closing_date"] != NOT_SPECIFIED
    assert record["verification"] == "REVIEW REQUIRED"
    change = storage.fetch(conn, "SELECT * FROM changes WHERE field = 'eligibility'")[0]
    assert change["new_value"] == NOT_SPECIFIED and state_site in change["evidence"]


def test_two_pages_of_the_same_site_fill_each_others_gaps(world):
    cfg, conn, web = world
    name = "Coastal Merit Scholarship"
    web[GOV + "/schemes/coastal-listing"] = (200, listing_page(name))
    web[GOV + "/schemes/coastal-guidelines"] = (200, guidelines_page(name))
    cfg["seeds"]["official"] += [GOV + "/schemes/coastal-guidelines", GOV + "/schemes/coastal-listing"]
    pipeline.run(cfg, conn)
    record = get(conn, name)
    assert record["official_source_url"] == GOV + "/schemes/coastal-listing"
    assert record["eligibility"] != NOT_SPECIFIED and record["closing_date"] != NOT_SPECIFIED
    assert record["verification"] == "VERIFIED" and storage.count(conn, "SELECT COUNT(*) FROM changes") == 0


def test_only_clearly_identical_names_are_merged():
    same = discovery.same_scholarship
    assert same("AICTE YASHASVI SCHOLARSHIP SCHEME", "AICTE Yashasvi Scholarship Scheme 2024")
    assert same("PM-USP Central Sector Scheme of Scholarship for College and University Students",
                "Pradhan Mantri PM-USP Central Sector Scheme Of Scholarship For College And University Students")
    assert not same("Post Matric Scholarship for SC", "Post Matric Scholarship for SC Students with Disabilities")
    assert not same("Prime Minister's Scholarship Scheme for Central Armed Police Forces", "Prime Minister's Scholarship Scheme for Wards of State Police")
    assert not same("Pragati Scholarship", "Saksham Scholarship")
    assert same("AICTE - Pragati Scholarship Scheme for Girl Students",
                "AICTE - Pragati Scholarship Scheme For Girl Students ( Technical Degree) (Merit Based Scheme)")
    assert not same("AICTE - Pragati Scholarship Scheme For Girl Students ( Technical Degree)",
                    "AICTE - Pragati Scholarship Scheme For Girl Students ( Technical Diploma)")
    assert not same("Pre Matric Scholarship For Students With Disabilities", "Post Matric Scholarship For Students With Disabilities")
    for bad in ["Backward Classes Welfare Department Scheme", "Department of Minority welfare Scheme", "Social Welfare Department"]:
        assert not discovery.is_proper_name(bad), bad
    assert discovery.is_proper_name("Department of School Education & Literacy National Means Cum Merit Scholarship")
    assert fetcher.tidy_url("https://scholarships.gov.in:443/All-Scholarships/") == fetcher.tidy_url("https://scholarships.gov.in/All-Scholarships")


def test_listing_page_apply_link_is_used_when_extractor_gives_none(world):
    cfg, conn, web = world
    listing = f"""<html><head><title>All schemes</title></head><body><main><h1>Schemes</h1>
    <p>Harbour Merit Scholarship. Ministry of Ports. Last date to apply: {long_date(50)}.</p>
    <a href="{GOV}/ApplicationForm">Apply Now</a><a href="{GOV}/faq">FAQ</a>{FILLER}</main></body></html>"""
    web[GOV + "/schemes/listing"] = (200, listing)
    web[GOV + "/ApplicationForm"] = (200, "<html><body>form</body></html>")
    page = fetcher.fetch(cfg, GOV + "/schemes/listing")
    raw = {"name": {"value": "Harbour Merit Scholarship", "evidence": "Harbour Merit Scholarship"},
           "closing_date": {"value": (today() + timedelta(days=50)).isoformat(), "evidence": "Last date to apply: " + long_date(50)}}
    fields, evidence, _rejected = extraction.ground(raw, page)
    assert fields["application_url"] == GOV + "/ApplicationForm"
    assert "published on this page" in evidence["application_url"]["note"]
    # two apply links on one page: the one whose address names the application form wins
    web[GOV + "/schemes/listing"] = (200, listing.replace("<a href", f'<a href="{GOV}/StudentLogin">Apply here</a><a href', 1))
    page = fetcher.fetch(cfg, GOV + "/schemes/listing")
    assert extraction.ground(raw, page)[0]["application_url"] == GOV + "/ApplicationForm"
    # two equally plausible apply links: ambiguous, so none is used
    web[GOV + "/schemes/listing"] = (200, listing.replace("<a href", f'<a href="{GOV}/apply-renewal">Apply for renewal</a><a href', 1))
    page = fetcher.fetch(cfg, GOV + "/schemes/listing")
    assert extraction.ground(raw, page)[0]["application_url"] == NOT_SPECIFIED


def test_apply_link_is_shared_across_a_listing_page(world):
    cfg, conn, web = world
    deadline = long_date(40)
    listing = f"""<html><head><title>Schemes</title></head><body><main><h1>Scheme list</h1>
    <p>Alpha Merit Scholarship. Ministry of Education. Last date to apply: {deadline}.</p>
    <p>Beta Merit Scholarship. Ministry of Education. Last date to apply: {deadline}.</p>
    <a href="{GOV}/ApplicationForm">Apply</a><a href="{GOV}/login">Student Login</a>{FILLER}</main></body></html>"""
    web[GOV + "/schemes/listing"] = (200, listing)
    web[GOV + "/ApplicationForm"] = (200, "<html><body>form</body></html>")
    page = fetcher.fetch(cfg, GOV + "/schemes/listing")
    iso = (today() + timedelta(days=40)).isoformat()
    raw = {"scholarships": [
        {"name": {"value": "Alpha Merit Scholarship", "evidence": "Alpha Merit Scholarship"}, "closing_date": {"value": iso, "evidence": "Last date to apply: " + deadline},
         "application_url": {"value": GOV + "/ApplicationForm", "evidence": "Apply"}},
        {"name": {"value": "Beta Merit Scholarship", "evidence": "Beta Merit Scholarship"}, "closing_date": {"value": iso, "evidence": "Last date to apply: " + deadline}},
    ]}
    pipeline.store_extracted(cfg, conn, page, raw, "fake/model", "seed", pipeline.new_counters())
    beta = get(conn, "Beta Merit Scholarship")
    assert beta["application_url"] == GOV + "/ApplicationForm"
    assert not discovery.is_proper_name("Department of Technical Education Scheme")
    assert discovery.same_scholarship("AICTE Swanath Scholarship Scheme", "AICTE - Swanath Scholarship Scheme ( Technical Degree) (Welfare Based Scheme)")
    assert not discovery.same_scholarship("Post Matric Scholarship", "Post Matric Scholarship for SC Students")
    assert discovery.same_scholarship("SWANATH SCHOLARSHIP SCHEME", "AICTE - Swanath Scholarship Scheme ( Technical Degree)", same_site=True)
    assert not discovery.same_scholarship("SWANATH SCHOLARSHIP SCHEME", "AICTE - Swanath Scholarship Scheme ( Technical Degree)")
    assert discovery.same_scholarship("National Means Cum Merit Scholarship (Merit Based Scheme)",
                                      "National Means-cum-Merit Scholarship Scheme of the Department of School Education and Literacy")
    for bad in ["Uttar Pradesh", "Post Matric (Bihar)", "SSO (Rajasthan)", "Har-Chhatravrati (Haryana)"]:
        assert not discovery.is_proper_name(bad), bad
    for good in ["Free Coaching For Scs OBCs And Beneficiaries Of PM Cares Children Scheme", "Fee Concession for AY 2024-25",
                 "Parivartan ECSS Programme for School Students"]:
        assert discovery.is_proper_name(good), good
