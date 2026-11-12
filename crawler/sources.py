"""Decides what kind of website a page is on, and whether it is the OFFICIAL source."""
import re
from urllib.parse import urlparse

TWO_PART_SUFFIXES = (".gov.in", ".nic.in", ".ac.in", ".edu.in", ".res.in", ".co.in", ".org.in", ".net.in",
                     ".ernet.in", ".gov.uk", ".ac.uk", ".co.uk", ".org.uk", ".com.au", ".edu.au")

# Words that say nothing about WHO the provider is
GENERIC_WORDS = {"the", "of", "and", "for", "in", "india", "indian", "national", "scholarship", "scholarships",
                 "scheme", "schemes", "foundation", "trust", "limited", "ltd", "pvt", "private", "company",
                 "government", "govt", "ministry", "department", "state", "programme", "program", "education",
                 "students", "student", "fellowship", "award", "merit", "means", "university", "institute",
                 "college", "technology", "council", "board", "society", "charitable", "group", "bank", "to", "by",
                 "new", "all", "sri", "shri", "smt", "post", "pre", "matric", "top", "girl", "girls", "boys", "women",
                 "child", "children", "higher", "school", "class", "free", "open", "day", "one", "two"}

INTERNATIONAL_WORDS = ["commonwealth", "fulbright", "chevening", "daad", "erasmus", "british council", "embassy",
                       "high commission", "rhodes", "gates cambridge", "inlaks", "unesco", "world bank"]
FOUNDATION_WORDS = ["foundation", "trust", "charitable", "society", "ngo", "sabha", "samiti", "mission"]


def registered_domain(url):
    """scholarships.gov.in stays whole (it ends in a two-part suffix); www.tatatrusts.org -> tatatrusts.org"""
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    keep = 3 if host.endswith(TWO_PART_SUFFIXES) else 2
    return ".".join(parts[-keep:])


def host_of(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_aggregator(cfg, url):
    host = host_of(url)
    settings = cfg["sources"]
    if any(host == site or host.endswith("." + site) for site in settings["aggregators"]):
        return True
    # Sites whose address looks like a listing/news/exam-prep site, unless they are government or academic
    if host.endswith(tuple(settings["government_suffixes"] + settings["university_suffixes"])):
        return False
    return any(word in host for word in settings["aggregator_words"])


def name_matches_domain(name, url):
    """True when the organisation's name can be seen in the web address.

    "Reliance Foundation" matches reliancefoundation.org (distinctive word "reliance").
    "National Institute of Technology Kurukshetra" matches nitkkr.ac.in (initials "nitk").
    """
    host = re.sub(r"[^a-z0-9]", "", host_of(url))
    words = re.findall(r"[a-z]+", (name or "").lower())
    for word in words:
        if word not in GENERIC_WORDS and len(word) >= 3 and word in host:
            return True
    initials = "".join(word[0] for word in words if word not in ("of", "and", "for", "the", "in"))
    for size in range(len(initials), 2, -1):
        if initials[:size] in host:
            return True
    return False


def classify(cfg, url, provider="", scholarship_name=""):
    """Returns {"type", "official", "reason"} for a page that describes a scholarship."""
    settings = cfg["sources"]
    host = host_of(url)
    provider_text = (provider or "").lower()
    matches = name_matches_domain(provider, url) or name_matches_domain(scholarship_name, url)

    if is_aggregator(cfg, url):
        return {"type": "aggregator", "official": False,
                "reason": host + " is an aggregator/news site: used for discovery only"}

    if any(host == portal or host.endswith("." + portal) for portal in settings["official_portals"]):
        return {"type": "scholarship_portal", "official": True, "reason": host + " is a recognised official scholarship portal"}

    if host.endswith(tuple(settings["government_suffixes"])):
        kind = "international" if not host.endswith(".in") else "government"
        return {"type": kind, "official": True, "reason": host + " is a government domain"}

    if host.endswith(tuple(settings["university_suffixes"])):
        if matches:
            return {"type": "university", "official": True, "reason": host + " is the institution's own academic domain"}
        if not provider:
            return {"type": "university", "official": False,
                    "reason": host + " is an institution's notice board and the page does not say who runs the scholarship"}
        return {"type": "university", "official": False,
                "reason": host + " is an institution re-posting a scholarship from another provider (" + provider + ")"}

    if matches:
        if any(word in provider_text for word in INTERNATIONAL_WORDS) or not (host.endswith(".in") or host.endswith(".org") or host.endswith(".com")):
            kind = "international"
        elif any(word in provider_text for word in FOUNDATION_WORDS):
            kind = "foundation_trust"
        else:
            kind = "corporate_csr"
        return {"type": kind, "official": True, "reason": "provider name '" + (provider or scholarship_name) + "' matches the domain " + host}

    return {"type": "unknown", "official": False, "reason": "could not confirm that " + host + " belongs to the provider"}
