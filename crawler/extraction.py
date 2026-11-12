"""Turns a page into structured scholarship data.

The LLM must give an exact evidence quote for every value. It is never trusted
on its own: grounding.py checks each quote against the page afterwards.
"""
import json
import logging
import re

from . import grounding, llm
from .config import ALL_FIELDS, DATE_FIELDS, NOT_SPECIFIED, NUMBER_FIELDS, URL_FIELDS

log = logging.getLogger("extraction")

DEADLINE_WORDS = r"(last date|deadline|closing date|apply by|apply before|applications? close|extended (?:up)?to|extended till|on or before|\btill\b|until)"
SCHOLARSHIP_WORDS = r"(scholarship|fellowship|stipend|bursary|freeship|financial assistance)"

SYSTEM_PROMPT = """You extract scholarship information for Indian students from ONE web page.
Use ONLY the page text you are given. Never use outside knowledge and never guess.

Return a JSON object:
{"page_type": "scholarship_detail" | "scholarship_listing" | "not_scholarship",
 "scholarships": [ {<field>: {"value": ..., "evidence": "..."}, ...} ]}

Rules:
- Include a scholarship only if this page itself describes it (at most 8, the best described first).
- On a listing page (a table of many schemes), return each scheme with the fields its row gives, such as closing date and ministry.
- For EVERY field give "value" and "evidence". "evidence" must be copied EXACTLY, character for character,
  from the page text (one short passage from one place, at most 200 characters). Do not paraphrase, do not join
  separate passages, and do not shorten with "...".
- If the page does not state a field, leave that field out. Missing is correct; inventing is a serious error.
- Keep each value close to the page's own wording.
- opening_date and closing_date: value in YYYY-MM-DD format, only if the full date (day, month, year) is written on the page.
- income_limit_inr: annual family income limit as a whole number of rupees (e.g. 800000), only if stated.
- application_url: choose ONLY from the "Links on the page" list, the link a student uses to apply. Evidence = the link text.
- application_mode: how to apply (online on which portal / offline / through the institute), if the page says so.
- name: the scholarship's proper name as written on the page, not a page heading such as "Scholarships" or "FAQ".
- If the page is a general portal page, a FAQ, a list of links, or not about a specific scholarship, return "scholarships": [].

Fields: name, provider (organisation that funds/runs it), amount (money or benefit), eligibility (who can apply, overall),
academic_requirements (marks/grades/exams), education_level (class/course/degree level), income_criteria, age_criteria,
gender_criteria, category_criteria (SC/ST/OBC/EWS/minority etc.), domicile_requirements (state/residence),
institution_requirements, disability_criteria, documents_required, selection_process, renewal_requirements,
application_mode (online/offline/through institute), opening_date, closing_date, income_limit_inr, application_url."""


def page_score(page):
    """How much does this page look like a scholarship description? (0-10)"""
    text = page["text"].lower()
    head = (page["title"] + " " + text[:300]).lower()
    score = 0
    if re.search(SCHOLARSHIP_WORDS, head):
        score += 3
    elif re.search(SCHOLARSHIP_WORDS, text):
        score += 1
    if "eligib" in text:
        score += 2
    if re.search(DEADLINE_WORDS, text):
        score += 2
    if re.search(r"(rs\.?|inr|₹)\s*\d", text):
        score += 1
    if "apply" in text or "application" in text:
        score += 1
    if len(text) >= 600:
        score += 1
    return score


def candidate_links(page):
    """Links a student might use to apply (shown to the LLM to choose from)."""
    if page.get("is_pdf") or len(page["links"]) <= 20:
        return page["links"][:20]
    chosen = []
    for url, label in page["links"]:
        if re.search(r"(apply|application|register|registration|login|portal|form|nsp|scholarship)", (label + " " + url).lower()):
            chosen.append((url, label))
    # Links that say "apply" first, then the rest
    chosen.sort(key=lambda link: 0 if re.search(r"(apply|application|register)", (link[1] + " " + link[0]).lower()) else 1)
    return chosen[:20]


def rule_deadlines(text):
    """Dates that appear right after deadline words. Used to cross-check the LLM."""
    dates = []
    lowered = grounding.normalize(text)
    for match in re.finditer(DEADLINE_WORDS, lowered):
        dates.extend(grounding.find_dates(lowered[match.start():match.start() + 140])[:1])
    return dates


def extract_with_llm(cfg, page):
    links = "\n".join("- " + label + " -> " + url for url, label in candidate_links(page)) or "(none)"
    size = cfg["crawl"]["max_text_chars"]
    # A long page listing many deadlines (e.g. a portal's table of schemes) is read in up to 3 parts
    parts = [page["text"][:size]]
    if len(page["text"]) > size and len(set(rule_deadlines(page["text"]))) >= 4:
        parts = [page["text"][start:start + size] for start in range(0, min(len(page["text"]), 3 * size), size)]

    scholarships, page_type, model = [], "", None
    for part in parts:
        user = ("Page URL: " + page["url"] + "\nPage title: " + page["title"] + "\n\nLinks on the page:\n" + links
                + "\n\nPage text:\n" + part)
        answer, model = llm.ask_llm(cfg, SYSTEM_PROMPT, user)
        found = answer.get("scholarships") or []
        if isinstance(found, list):
            scholarships.extend(found[:8])
        page_type = page_type or answer.get("page_type", "")
    return {"page_type": page_type, "scholarships": scholarships}, model


def extract_with_rules(page):
    """Backup when no LLM is available: a few fields found with plain pattern matching."""
    lines = page["text"].split("\n")
    record = {}

    for line in lines[:40]:
        if re.search(SCHOLARSHIP_WORDS, line.lower()) and 10 <= len(line) <= 140:
            record["name"] = {"value": line, "evidence": line}
            break
    if "name" not in record:
        return {"page_type": "not_scholarship", "scholarships": []}

    for index, line in enumerate(lines):
        low = line.lower()
        if "closing_date" not in record and re.search(DEADLINE_WORDS, low):
            window = " ".join(lines[index:index + 2])
            dates = grounding.find_dates(window)
            if dates:
                record["closing_date"] = {"value": dates[0].isoformat(), "evidence": window[:250]}
        if "amount" not in record and re.search(r"(rs\.?|inr|₹)\s*\d", low) and re.search(r"(amount|award|scholarship|per annum|per month|per year)", low):
            record["amount"] = {"value": line[:250], "evidence": line[:250]}
        if "eligibility" not in record and "eligib" in low:
            sentence = line if len(line) > 40 else " ".join(lines[index:index + 2])
            record["eligibility"] = {"value": sentence[:250], "evidence": sentence[:250]}

    for url, label in candidate_links(page):
        if "apply" in label.lower():
            record["application_url"] = {"value": url, "evidence": label}
            break
    return {"page_type": "scholarship_detail", "scholarships": [record]}


def extract(cfg, page):
    """Returns (raw result, name of the model that produced it).

    Returns (None, None) when an LLM is configured but failed for this page: the
    page is then skipped and tried again on the next run, instead of storing a
    poor rule-based record. Rules are used only when no LLM is configured at all.
    """
    if not llm.is_available(cfg):
        return extract_with_rules(page), "rules"
    try:
        return extract_with_llm(cfg, page)
    except llm.LLMError as error:
        if "413" in str(error) or "too large" in str(error).lower():
            try:
                shorter = dict(page, text=page["text"][:cfg["crawl"]["max_text_chars"] // 2])
                return extract_with_llm(cfg, shorter)
            except llm.LLMError as second_error:
                error = second_error
        log.warning("LLM failed for %s (%s); page skipped, will retry next run", page["url"], error)
        return None, None


APPLY_LINK = re.compile(r"\b(apply\s*(online|now|here)?|application\s*form|student\s*login|register\s*(now|online)?)\b", re.IGNORECASE)


def page_application_link(page):
    """The page's own application link, when it publishes exactly one.

    Used only when the extractor found no link for a scholarship. Listing pages on a
    portal give one application form for every scheme they list, so this is the link a
    student would use. The evidence records that it came from the page's apply button.
    """
    from . import sources
    found = []
    for url, label in page["links"]:
        if APPLY_LINK.search(label) and sources.registered_domain(url) == sources.registered_domain(page["url"]):
            if url not in [link for link, _ in found]:
                found.append((url, label))
    if len(found) > 1:
        # Prefer the link whose address itself says it is the application form
        named = [(url, label) for url, label in found if re.search(r"(applicationform|apply)", url.lower())]
        found = named or found
    return found[0] if len(found) == 1 else (None, None)


def ground(raw_scholarship, page):
    """Check every field against the page. Returns (fields, evidence, rejected list)."""
    page_links = [url for url, _label in page["links"]]
    fields, evidence, rejected = {}, {}, []
    for field in ALL_FIELDS:
        item = raw_scholarship.get(field) or {}
        if not isinstance(item, dict):
            item = {"value": item, "evidence": ""}
        value, quote = item.get("value"), str(item.get("evidence") or "")
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else "; ".join(str(v) for v in value)
        final, verified, note, quote = grounding.check_field(field, value, quote, page["text"], page_links)
        if field in DATE_FIELDS and verified:
            final = final[:10]
        if field in NUMBER_FIELDS and verified:
            final = str(int(float(final)))
        fields[field] = final
        evidence[field] = {"value": final, "quote": quote if verified else "", "verified": verified,
                           "note": note, "source_url": page["url"]}
        if note.startswith("rejected"):
            rejected.append(field + ": '" + str(value)[:80] + "' (" + note + ")")

    if fields["application_url"] == NOT_SPECIFIED:
        link, label = page_application_link(page)
        if link:
            fields["application_url"] = link
            evidence["application_url"] = {"value": link, "quote": label, "verified": True, "source_url": page["url"],
                                           "note": "the application link published on this page (\"" + label.strip()[:60] + "\")"}
    return fields, evidence, rejected
