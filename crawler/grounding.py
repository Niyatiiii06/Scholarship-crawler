"""Anti-hallucination checks.

A value is only kept when (1) its evidence quote really appears on the page and
(2) the quote really supports the value. Everything else becomes "Not specified".
"""
import re
from datetime import date
from functools import lru_cache

from .config import DATE_FIELDS, NOT_SPECIFIED, NUMBER_FIELDS, URL_FIELDS

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
MONTH_PATTERN = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

EMPTY_VALUES = ("", "null", "none", "n/a", "na", "not specified", "not mentioned", "not available", "unknown")


def normalize(text):
    """Lower-case, straighten quotes/dashes and collapse spaces so texts can be compared."""
    text = str(text or "").lower()
    for old, new in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                     ("–", "-"), ("—", "-"), ("\u2011", "-"), ("\u2010", "-"), ("\xa0", " ")]:
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def is_empty(value):
    return value is None or normalize(value) in EMPTY_VALUES


def make_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def find_dates(text):
    """Every calendar date written in the text, in common Indian formats."""
    text = normalize(text)
    found = []
    for day, month, year in re.findall(r"\b(\d{1,2})\s?(?:st|nd|rd|th)?[\s\-/.,]+(?:day of\s+)?" + MONTH_PATTERN + r"[\s\-/.,]+(\d{4})\b", text):
        found.append(make_date(year, MONTHS[month[:4] if month.startswith("sept") else month[:3]], day))
    for month, day, year in re.findall(r"\b" + MONTH_PATTERN + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        found.append(make_date(year, MONTHS[month[:4] if month.startswith("sept") else month[:3]], day))
    for day, month, year in re.findall(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text):
        found.append(make_date(year, month, day))
    for year, month, day in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        found.append(make_date(year, month, day))
    return [d for d in found if d is not None]


def find_amounts(text):
    """Every rupee amount in the text as a whole number: "Rs. 2.5 lakh" -> 250000."""
    text = normalize(text).replace("lakhs", "lakh").replace("lacs", "lakh").replace("lac", "lakh").replace("crores", "crore")
    amounts = []
    pattern = r"(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(lakh|crore|thousand)?"
    for number, unit in re.findall(pattern, text):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        value *= {"lakh": 100000, "crore": 10000000, "thousand": 1000}.get(unit, 1)
        amounts.append(int(round(value)))
    return amounts


@lru_cache(maxsize=16)
def squash(text):
    """Letters and digits only. PDF text often has stray spaces ("i s a") and odd punctuation."""
    return re.sub(r"[\W_]", "", normalize(text))


def quote_on_page(quote, page_text):
    """Is the evidence quote really on the page?

    Spacing and punctuation may differ, but the letters and digits must match in
    order. A quote shortened with "..." must have every piece on the page.
    """
    pieces = [piece for piece in re.split(r"\.{3,}|\u2026", str(quote or "")) if len(squash(piece)) >= 8]
    if not pieces:
        return False
    page_squashed = squash(page_text)
    if all(squash(piece) in page_squashed for piece in pieces):
        return True
    quote, page_text = normalize(quote), normalize(page_text)
    # Fallback: at least 85% of the quote's 5-word runs must appear on the page
    words = quote.split()
    if len(words) < 6:
        return False
    runs = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
    present = sum(1 for run in runs if run in page_text)
    return present / len(runs) >= 0.85


def content_words(text):
    stop = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "is", "are", "be", "by", "on", "with", "as", "at",
            "from", "must", "should", "who", "that", "only", "per", "each", "every", "i", "e"}
    same = {"female": "girl", "females": "girl", "women": "girl", "woman": "girl", "girls": "girl", "male": "boy", "boys": "boy"}
    text = re.sub(r"(?<=\d),(?=\d)", "", normalize(text))
    words = []
    for word in re.findall(r"[a-z0-9]+", text):
        if word in stop:
            continue
        word = same.get(word, word)
        words.append(word[:-1] if len(word) > 4 and word.endswith("s") else word)   # courses -> course
    return words


def find_supporting_passage(value, page_text):
    """Search the page for 1-3 consecutive lines that contain the value's words and numbers.

    Used when the LLM's quote was not exact: the code finds the real passage itself.
    Returns the passage, or "" if the page does not support the value.
    """
    words = set(content_words(value))
    if len(words) < 2:
        return ""
    numbers = {word for word in words if word.isdigit()}
    lines = [line for line in page_text.split("\n") if line.strip()]
    line_words = [set(content_words(line)) for line in lines]
    best, best_score = "", 0.8
    for size in (1, 2, 3):
        for start in range(len(lines) - size + 1):
            window = set().union(*line_words[start:start + size])
            if not numbers <= window:
                continue
            score = len(words & window) / len(words)
            if score > best_score or (score == best_score and not best):
                best, best_score = " ".join(lines[start:start + size]), score
        if best:
            break
    return best[:400]


def same_link(first, second):
    def bare(url):
        url = re.sub(r"^https?://", "", str(url).strip().lower())
        return re.sub(r"^www\.", "", url).rstrip("/")
    return bare(first) == bare(second)


def text_around(quote, page_text, reach=400):
    """The part of the page surrounding the quote (values often summarise a few nearby lines)."""
    page_text = normalize(page_text)
    position = page_text.find(normalize(quote))
    if position < 0:
        return ""
    return page_text[max(0, position - reach):position + len(quote) + reach]


def value_supported(field, value, quote, page_links=(), nearby=""):
    """Does the quote actually say what the value claims?"""
    if field in DATE_FIELDS:
        try:
            wanted = date.fromisoformat(str(value)[:10])
        except ValueError:
            return False
        return wanted in find_dates(quote)

    if field in NUMBER_FIELDS:
        try:
            return int(float(value)) in find_amounts(quote)
        except (TypeError, ValueError):
            return False

    if field in URL_FIELDS:
        return any(same_link(link, value) for link in page_links)

    # Text fields: every number in the value must be in the evidence, and most of its words too
    words = content_words(value)
    if not words:
        return False
    evidence_numbers = set(re.findall(r"\d+", " ".join(content_words(quote + " " + nearby))))
    if any(number not in evidence_numbers for number in re.findall(r"\d+", " ".join(words))):
        return False
    quote_words = set(content_words(quote))
    if sum(1 for w in words if w in quote_words) / len(words) >= 0.6:
        return True
    # A provider written out in full is supported by its acronym in the quote, e.g. "(AICTE)"
    if field == "provider":
        acronyms = [token.lower() for token in re.findall(r"[A-Za-z]+", str(value)) if token.isupper() and len(token) >= 3]
        if any(token in quote_words for token in acronyms):
            return True
    nearby_words = set(content_words(nearby))
    return sum(1 for w in words if w in nearby_words) / len(words) >= 0.8


def check_field(field, value, quote, page_text, page_links=()):
    """Returns (final value, verified, note, evidence quote). Unsupported values are NOT kept."""
    if is_empty(value):
        return NOT_SPECIFIED, False, "not mentioned on the page", ""
    if field in URL_FIELDS:
        for link in page_links:
            if same_link(link, value):
                return link, True, "link found on the official page", str(quote or link)
        return NOT_SPECIFIED, False, "rejected: link is not on the page", ""

    if quote_on_page(quote, page_text) and value_supported(field, value, quote, nearby=text_around(quote, page_text)):
        return str(value).strip(), True, "evidence quote found on the page and supports the value", str(quote)

    # The LLM's quote was not usable. For text fields, look for the supporting passage ourselves.
    if field not in DATE_FIELDS + NUMBER_FIELDS:
        passage = find_supporting_passage(value, page_text)
        if passage:
            return str(value).strip(), True, "supporting passage located on the page by the verifier", passage
    if not quote_on_page(quote, page_text):
        return NOT_SPECIFIED, False, "rejected: evidence quote not found on the page", ""
    return NOT_SPECIFIED, False, "rejected: quote does not support the value", ""
