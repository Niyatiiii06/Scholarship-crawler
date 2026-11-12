"""The verification engine: a confidence score built from evidence checks.

No LLM is involved here. Each check looks at facts the crawler recorded and
awards points; the total (0-100) is the confidence. VERIFIED needs 95+.
"""
import re
from datetime import date

from . import extraction, grounding, sources
from .config import ELIGIBILITY_FIELDS, IMPORTANT_FIELDS, NOT_SPECIFIED


def has(record, field):
    return record["evidence"].get(field, {}).get("verified", False)


def score_record(cfg, record, facts, today):
    """facts = what we observed: page_live, name_on_page, page_text, application_status, conflicts.

    Returns (confidence, list of checks). Each check has name, points, max_points and a reason.
    """
    weights = cfg["scoring"]["weights"]
    checks = []

    def add(name, fraction, reason):
        checks.append({"name": name, "points": round(weights[name] * fraction, 1), "max_points": weights[name], "reason": reason})

    # 1. Is the source official?
    if record["is_official"]:
        add("official_source", 1, record["source_reason"])
    elif record["source_type"] == "aggregator":
        add("official_source", 0, record["source_reason"])
    else:
        add("official_source", 0.3, record["source_reason"])

    # 2. Is the scholarship on the official source right now?
    if facts["page_live"] and facts["name_on_page"]:
        add("live_on_source", 1, "page loaded successfully in this run and the scholarship name is on it")
    elif facts["page_live"]:
        add("live_on_source", 0.5, "page loaded but the scholarship name was not found on it")
    else:
        add("live_on_source", 0, "page could not be loaded in this run")

    # 3. Is there a working official application link?
    link = record["application_url"]
    if link != NOT_SPECIFIED:
        same_site = sources.registered_domain(link) == sources.registered_domain(record["official_source_url"])
        trusted = same_site or sources.classify(cfg, link, record["provider"])["official"]
        # 401/403/405/429 mean the server is there but refuses robots: the link is not dead
        working = 200 <= facts["application_status"] < 400 or facts["application_status"] in (401, 403, 405, 429)
        if working:
            add("application_url", 1, "application link is published on the official page and responds" + ("" if trusted else " (it is on another domain: " + sources.host_of(link) + ")"))
        elif trusted:
            add("application_url", 0.7, "application link is on the official page and a trusted domain, but did not respond (status " + str(facts["application_status"]) + ")")
        else:
            add("application_url", 0.4, "application link is on the official page but is on another domain and did not respond")
    elif has(record, "application_mode"):
        add("application_url", 0.6, "no application link, but the page states how to apply: " + record["application_mode"][:80])
    else:
        add("application_url", 0, "no application link or application method found on the page")

    # 4. Are the eligibility criteria supported by quotes from the page?
    specific = [f for f in ELIGIBILITY_FIELDS if has(record, f)]
    if has(record, "eligibility") and specific:
        add("eligibility_evidence", 1, "eligibility and " + str(len(specific)) + " specific criteria are backed by page quotes")
    elif len(specific) >= 2:
        add("eligibility_evidence", 1, str(len(specific)) + " specific criteria are backed by page quotes (" + ", ".join(specific) + ")")
    elif has(record, "eligibility") or specific:
        add("eligibility_evidence", 0.6, "only part of the eligibility is backed by page quotes")
    else:
        add("eligibility_evidence", 0, "no eligibility text could be traced to the page")

    # 5. Is the deadline supported by a quote from the page?
    if has(record, "closing_date"):
        add("deadline_evidence", 1, "deadline " + record["closing_date"] + " is backed by the quote: \"" + record["evidence"]["closing_date"]["quote"][:100] + "\"")
    else:
        add("deadline_evidence", 0, "the page does not state a full closing date")

    # 6. Is the information current?
    year = today.year
    if has(record, "closing_date"):
        age = (today - date.fromisoformat(record["closing_date"])).days
        if age <= 365:
            add("currency", 1, "deadline is in the current cycle (" + record["closing_date"] + ")")
        else:
            add("currency", 0.2, "deadline is more than a year old (" + record["closing_date"] + ")")
    elif re.search(r"\b(" + str(year) + "|" + str(year + 1) + r")\b", facts["page_text"]):
        add("currency", 0.7, "page mentions the current year but gives no deadline")
    else:
        add("currency", 0.3, "page gives no deadline and does not mention the current year")

    # 7. Do other sources contradict it?
    if facts["conflicts"]:
        add("no_conflict", 0, "conflict: " + "; ".join(facts["conflicts"])[:300])
    else:
        add("no_conflict", 1, "no conflicting source found")

    # 8. Was the extraction consistent? (LLM result compared with plain pattern matching)
    problems = []
    rule_dates = [d.isoformat() for d in extraction.rule_deadlines(facts["page_text"])]
    if has(record, "closing_date") and rule_dates and record["closing_date"] not in rule_dates:
        problems.append("pattern matching found a different deadline (" + ", ".join(sorted(set(rule_dates))[:3]) + ")")
    fraction = 0.5 if problems else 1
    if record["rejected"]:
        problems.append(str(len(record["rejected"])) + " extracted value(s) were rejected for lacking evidence (rejected values are not stored)")
        fraction = max(0, fraction - 0.1 * len(record["rejected"]))
    add("extraction_consistency", fraction, "; ".join(problems) if problems else "LLM values agree with pattern matching and none were rejected")

    # 9. Can the important fields be traced to evidence?
    traced = [f for f in IMPORTANT_FIELDS if has(record, f)]
    if "application_url" not in traced and has(record, "application_mode"):
        traced.append("application_mode")
    add("traceability", len(traced) / len(IMPORTANT_FIELDS),
        str(len(traced)) + " of " + str(len(IMPORTANT_FIELDS)) + " key fields have a source quote (" + ", ".join(traced) + ")")

    confidence = round(sum(check["points"] for check in checks), 1)
    return confidence, checks


def verification_label(cfg, record, confidence, facts):
    """VERIFIED only with 95+, an official source, and no conflict."""
    if confidence >= cfg["scoring"]["verified_threshold"] and record["is_official"] and not facts["conflicts"] and facts["page_live"]:
        return "VERIFIED"
    return "REVIEW REQUIRED"


def lifecycle_status(cfg, record, today):
    """ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED from the deadline."""
    if record["closing_date"] == NOT_SPECIFIED:
        return "REVIEW_REQUIRED"
    days_left = (date.fromisoformat(record["closing_date"]) - today).days
    if days_left < 0:
        return "EXPIRED"
    if days_left <= cfg["status"]["expiring_soon_days"]:
        return "EXPIRING_SOON"
    return "ACTIVE"
