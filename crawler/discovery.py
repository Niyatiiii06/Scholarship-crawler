"""Finding pages worth reading: web search, sitemaps, link scoring and aggregator leads."""
import logging
import re
from datetime import date
from urllib.parse import urlparse

from . import fetcher, sources, storage
from .config import now_text
from .extraction import SCHOLARSHIP_WORDS

log = logging.getLogger("discovery")

SKIP_LINK_WORDS = r"(tender|recruit|career|gallery|photo|video|contact|privacy|terms|sitemap|login|signin|logout|rti|feedback|javascript:)"
GENERIC_LEAD_NAMES = r"^(all |view |more |latest |top |best |list of |scholarships? (for|in|by) |scholarships?$|apply)"


def slug(name):
    """A stable key for a scholarship name: lower-case words without years, e.g.
    "Pragati Scholarship 2026-27" -> "pragati-scholarship"."""
    skip = {"for", "a", "y", "ay", "fy", "the", "of", "in", "to", "and", "academic", "year", "session"}
    words = re.findall(r"[a-z]+|\d+", (name or "").lower())
    return "-".join(word for word in words if not word.isdigit() and word not in skip)


GENERIC_NAME_WORDS = {"scholarship", "scholarships", "fellowship", "fellowships", "scheme", "schemes", "national",
                      "portal", "nsp", "student", "students", "faq", "faqs", "guidelines", "division", "rules",
                      "list", "all", "home", "otr", "registration", "notice", "notification", "details", "for", "of", "the",
                      "stipend", "stipends", "award", "awards", "grant", "grants", "programme", "program", "yojana", "based"}


DEPARTMENT_WORDS = {"department", "ministry", "directorate", "welfare", "empowerment", "classes", "backward",
                    "minority", "social", "justice", "tribal", "affairs", "literacy", "development", "board",
                    "commission", "council", "government", "state", "centre", "center", "cell", "office"}


def is_proper_name(name):
    """False for page headings ("Scholarships") and department names ("Minority Welfare Department Scheme")."""
    words = re.findall(r"[a-z]+", name.lower())
    distinctive = [word for word in words if word not in GENERIC_NAME_WORDS and len(word) > 1]
    if not distinctive or not name[0].isalnum() or name[0].islower() or len(name) > 150:
        return False
    # A name made only of department words is the department, not a scholarship
    if not [word for word in distinctive if word not in DEPARTMENT_WORDS]:
        return False
    # "Department of Technical Education Scheme": a department with no scholarship word at all
    has_scholarship_word = any(word in words for word in ("scholarship", "scholarships", "fellowship", "fellowships", "stipend",
                                                          "stipends", "award", "awards", "grant", "grants", "bursary", "yojana",
                                                          "freeship", "concession", "assistance", "programme", "program"))
    if any(word in DEPARTMENT_WORDS for word in words) and not has_scholarship_word:
        return False
    # "Uttar Pradesh", "Post Matric (Bihar)", "SSO (Rajasthan)": a place or a portal, not a scholarship name
    return has_scholarship_word or "scheme" in words or len(distinctive) >= 4


def search_web(cfg, query):
    """Free web search (DuckDuckGo through the ddgs package). Returns [(url, title)]."""
    try:
        from ddgs import DDGS
        results = DDGS().text(query, region="in-en", max_results=cfg["search"]["results_per_query"])
        return [(item.get("href", ""), item.get("title", "")) for item in results if item.get("href")]
    except Exception as error:
        log.warning("search failed for '%s': %s", query, str(error)[:120])
        return []


def sitemap_links(cfg, site_url):
    """Scholarship pages listed in a site's sitemap.xml (if it has one)."""
    parts = urlparse(site_url)
    answer = fetcher.http_get(cfg, parts.scheme + "://" + parts.netloc + "/sitemap.xml")
    if answer["status"] != 200:
        return []
    found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", answer["text"])
    return [url for url in found if re.search(SCHOLARSHIP_WORDS, url.lower())][:15]


def link_priority(url, label):
    """0 = do not follow. Higher = more likely to lead to a scholarship."""
    text = (label + " " + url).lower()
    if re.search(SKIP_LINK_WORDS, text):
        return 0
    score = 0
    if re.search(r"(scholarship|fellowship)", text):
        score += 5
    if re.search(r"(stipend|bursary|freeship|financial assistance|financial aid)", text):
        score += 4
    if re.search(r"(scheme|student)", text):
        score += 2
    if re.search(r"(notification|circular|apply|guideline)", text):
        score += 1
    return score


def looks_official_for(cfg, url, scholarship_name):
    """Could this search result be the official page of a scholarship we only know by name?"""
    if sources.is_aggregator(cfg, url):
        return False
    host = sources.host_of(url)
    settings = cfg["sources"]
    if host.endswith(tuple(settings["government_suffixes"])) or host.endswith(tuple(settings["university_suffixes"])):
        return True
    if any(host.endswith(portal) for portal in settings["official_portals"]):
        return True
    return sources.name_matches_domain(scholarship_name, url)


def collect_leads(conn, page):
    """On an aggregator page we only note scholarship NAMES. No data is copied."""
    added = 0
    for _url, label in page["links"]:
        label = label.strip()
        if not (12 <= len(label) <= 120) or not re.search(SCHOLARSHIP_WORDS, label.lower()):
            continue
        if re.search(GENERIC_LEAD_NAMES, label.lower()):
            continue
        added += add_lead(conn, label, page["url"])
        if added >= 20:
            break
    return added


def add_lead(conn, name, found_on_url, closing_date=None):
    key = slug(name)
    if not key or storage.count(conn, "SELECT COUNT(*) FROM leads WHERE slug = ?", (key,)):
        return 0
    if storage.count(conn, "SELECT COUNT(*) FROM scholarships WHERE id LIKE ?", (key + "@%",)):
        return 0
    storage.insert(conn, "leads", {"slug": key, "name": name, "found_on_url": found_on_url, "found_at": now_text(),
                                   "closing_date": closing_date, "state": "new"})
    return 1


def resolve_leads(cfg, conn):
    """Search the web for the official page of each new lead. Returns [(url, via)]."""
    found = []
    leads = storage.fetch(conn, "SELECT * FROM leads WHERE state = 'new' ORDER BY found_at LIMIT ?", (cfg["crawl"]["max_leads_per_run"],))
    for lead in leads:
        official = ""
        for url, _title in search_web(cfg, lead["name"] + " official website apply"):
            if looks_official_for(cfg, url, lead["name"]):
                official = url
                break
        conn.execute("UPDATE leads SET state = ?, resolved_url = ? WHERE slug = ?",
                     ("resolved" if official else "unresolved", official, lead["slug"]))
        conn.commit()
        if official:
            found.append((official, "lead '" + lead["name"] + "' first seen on " + lead["found_on_url"]))
            log.info("lead resolved: %s -> %s", lead["name"], official)
    return found


def deadline_queries(today):
    """Searches aimed at scholarships that are open now: they name this month and the next two."""
    queries = []
    year, month = today.year, today.month
    for _ in range(3):
        name = date(year, month, 1).strftime("%B %Y")
        queries.append("scholarship for Indian students last date " + name + " apply online eligibility")
        queries.append("scholarship applications invited last date " + name + " official notification India")
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return queries


AUDIENCE_WORDS = {"disability", "disabilities", "disabled", "abled", "divyang", "handicapped", "girl", "girls", "boy", "boys",
                  "women", "woman", "sc", "st", "obc", "ebc", "ews", "dnt", "minority", "minorities", "muslim", "christian",
                  "sikh", "buddhist", "parsi", "jain", "pre", "post", "matric", "orphan", "orphans", "widow", "ward", "wards",
                  "bpl", "tribal", "scheduled", "caste", "tribe", "tribes", "overseas", "abroad", "foreign"}


def name_tokens(name):
    """The distinctive words of a scholarship name (no years, no generic words)."""
    return {word for word in slug(name).split("-") if word and word not in GENERIC_NAME_WORDS}


def same_scholarship(first, second, same_site=False):
    """Do two names refer to the same scholarship? Deliberately strict: a wrong merge would create a false record.

    same_site=True when both pages are on the same official site, where a one-word
    name ("Swanath Scholarship Scheme" in the guidelines) can safely be matched.
    """
    a, b = name_tokens(first), name_tokens(second)
    if not a or not b:
        return False
    if a == b:
        return len(a) >= 2 or (same_site and len(a) == 1)
    small, large = sorted([a, b], key=len)
    # One name inside the other ("AICTE Pragati ... Girl" inside "AICTE Pragati ... Girl (Technical Degree)").
    # Three distinctive words must match, so "... Technical Degree" and "... Technical Diploma" stay apart,
    # and the longer name must not add an audience word: "Post Matric for SC" is not
    # "Post Matric for SC Students with Disabilities".
    if not small <= large or (large - small) & AUDIENCE_WORDS:
        return False
    if same_site and len(small) == 1 and len(next(iter(small))) >= 6 and not small & AUDIENCE_WORDS:
        return True
    # Two words are enough when neither is an audience word ("AICTE Swanath" inside
    # "AICTE Swanath (Technical Degree)"); otherwise three are needed.
    return len(small) >= 3 or (len(small) == 2 and not small & AUDIENCE_WORDS)
