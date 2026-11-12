"""SQLite database (data/scholarships.db). Open it with any SQLite viewer to inspect records.

Tables:
  scholarships   one row per scholarship (current values, status, confidence)
  evidence       one row per field: value, exact quote, source URL, verified or not
  score_checks   one row per confidence check: points and the reason
  versions       every extraction ever made (nothing is overwritten)
  changes        old value -> new value, with date, source and evidence
  pages          every URL the crawler visited
  leads          scholarship names seen on aggregators, waiting for an official source
  runs           one row per crawler run with its summary
"""
import json
import sqlite3

from .config import ALL_FIELDS, now_text

FIELD_COLUMNS = ",\n    ".join(field + " TEXT" for field in ALL_FIELDS if field != "name")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scholarships (
    id TEXT PRIMARY KEY,
    name TEXT,
    """ + FIELD_COLUMNS + """,
    source_type TEXT,
    is_official INTEGER,
    source_reason TEXT,
    official_source_url TEXT,
    status TEXT,
    verification TEXT,
    confidence REAL,
    extracted_by TEXT,
    rejected TEXT,
    page_hash TEXT,
    discovered_via TEXT,
    first_seen TEXT,
    last_checked TEXT,
    last_verified TEXT,
    last_changed TEXT,
    fail_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS evidence (
    scholarship_id TEXT, field TEXT, value TEXT, quote TEXT, source_url TEXT, verified INTEGER, note TEXT,
    PRIMARY KEY (scholarship_id, field)
);
CREATE TABLE IF NOT EXISTS score_checks (
    scholarship_id TEXT, position INTEGER, name TEXT, points REAL, max_points REAL, reason TEXT,
    PRIMARY KEY (scholarship_id, name)
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id TEXT, captured_at TEXT, origin TEXT, source_url TEXT, page_hash TEXT, fields TEXT, evidence TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id TEXT, field TEXT, old_value TEXT, new_value TEXT, detected_at TEXT,
    source_url TEXT, evidence TEXT, old_source_url TEXT, old_evidence TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY, domain TEXT, status_code INTEGER, page_hash TEXT, fetched_at TEXT,
    page_score INTEGER, depth INTEGER, discovered_via TEXT, extracted_hash TEXT,
    raw_extraction TEXT, extracted_by TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    slug TEXT PRIMARY KEY, name TEXT, found_on_url TEXT, found_at TEXT, closing_date TEXT, resolved_url TEXT, state TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, summary TEXT
);
"""

SCHOLARSHIP_COLUMNS = ["id"] + ALL_FIELDS + ["source_type", "is_official", "source_reason", "official_source_url",
                                              "status", "verification", "confidence", "extracted_by", "rejected",
                                              "page_hash", "discovered_via", "first_seen", "last_checked",
                                              "last_verified", "last_changed", "fail_count"]


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for column in ("raw_extraction", "extracted_by"):      # databases made by an earlier version
        try:
            conn.execute("ALTER TABLE pages ADD COLUMN " + column + " TEXT")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def fetch(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def insert(conn, table, row, command="INSERT"):
    columns = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    conn.execute(command + " INTO " + table + " (" + columns + ") VALUES (" + marks + ")", list(row.values()))
    conn.commit()


def load_record(conn, scholarship_id):
    """A scholarship with its evidence, in the same shape the pipeline builds."""
    rows = fetch(conn, "SELECT * FROM scholarships WHERE id = ?", (scholarship_id,))
    if not rows:
        return None
    record = rows[0]
    record["is_official"] = bool(record["is_official"])
    record["rejected"] = json.loads(record["rejected"] or "[]")
    record["evidence"] = {}
    for item in fetch(conn, "SELECT * FROM evidence WHERE scholarship_id = ?", (scholarship_id,)):
        item["verified"] = bool(item["verified"])
        record["evidence"][item["field"]] = item
    return record


def save_record(conn, record, checks):
    """Write the current state of a scholarship, its evidence and its score breakdown."""
    row = {column: record.get(column) for column in SCHOLARSHIP_COLUMNS}
    row["is_official"] = 1 if record["is_official"] else 0
    row["rejected"] = json.dumps(record["rejected"], ensure_ascii=False)
    insert(conn, "scholarships", row, "INSERT OR REPLACE")

    conn.execute("DELETE FROM evidence WHERE scholarship_id = ?", (record["id"],))
    for field, item in record["evidence"].items():
        insert(conn, "evidence", {"scholarship_id": record["id"], "field": field, "value": item["value"],
                                  "quote": item["quote"], "source_url": item["source_url"],
                                  "verified": 1 if item["verified"] else 0, "note": item["note"]})

    conn.execute("DELETE FROM score_checks WHERE scholarship_id = ?", (record["id"],))
    for position, check in enumerate(checks):
        insert(conn, "score_checks", {"scholarship_id": record["id"], "position": position, "name": check["name"],
                                      "points": check["points"], "max_points": check["max_points"], "reason": check["reason"]})
    conn.commit()


def add_version(conn, record, origin, captured_at=None, source_url=None):
    insert(conn, "versions", {
        "scholarship_id": record["id"], "captured_at": captured_at or now_text(), "origin": origin,
        "source_url": source_url or record["official_source_url"], "page_hash": record.get("page_hash", ""),
        "fields": json.dumps({field: record[field] for field in ALL_FIELDS}, ensure_ascii=False),
        "evidence": json.dumps({field: item["quote"] for field, item in record["evidence"].items()}, ensure_ascii=False),
    })


def add_change(conn, scholarship_id, field, old_value, new_value, source_url, evidence,
               old_source_url="", old_evidence="", note=""):
    insert(conn, "changes", {"scholarship_id": scholarship_id, "field": field, "old_value": old_value,
                             "new_value": new_value, "detected_at": now_text(), "source_url": source_url,
                             "evidence": evidence, "old_source_url": old_source_url,
                             "old_evidence": old_evidence, "note": note})


def save_page(conn, page, depth, discovered_via, score, domain):
    old = fetch(conn, "SELECT extracted_hash, raw_extraction, extracted_by FROM pages WHERE url = ?", (page["url"],))
    insert(conn, "pages", {"url": page["url"], "domain": domain, "status_code": page["status"],
                           "page_hash": page["hash"], "fetched_at": now_text(), "page_score": score, "depth": depth,
                           "discovered_via": discovered_via,
                           "extracted_hash": old[0]["extracted_hash"] if old else None,
                           "raw_extraction": old[0]["raw_extraction"] if old else None,
                           "extracted_by": old[0]["extracted_by"] if old else None}, "INSERT OR REPLACE")
