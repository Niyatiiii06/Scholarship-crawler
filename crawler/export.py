"""Writes readable copies of the database (CSV + JSON) and prints summaries."""
import csv
import json

from . import storage
from .config import ALL_FIELDS


def stats(conn):
    """The numbers shown on the dashboard and by `python main.py status`."""
    def n(sql):
        return storage.count(conn, sql)

    average = conn.execute("SELECT AVG(confidence) FROM scholarships").fetchone()[0] or 0
    return {
        "Total discovered": n("SELECT COUNT(*) FROM scholarships"),
        "Verified (95%+)": n("SELECT COUNT(*) FROM scholarships WHERE verification = 'VERIFIED'"),
        "Review required": n("SELECT COUNT(*) FROM scholarships WHERE verification != 'VERIFIED'"),
        "On an official source": n("SELECT COUNT(*) FROM scholarships WHERE is_official = 1 AND status != 'NO_LONGER_VERIFIABLE'"),
        "Active": n("SELECT COUNT(*) FROM scholarships WHERE status IN ('ACTIVE', 'EXPIRING_SOON')"),
        "Expiring soon": n("SELECT COUNT(*) FROM scholarships WHERE status = 'EXPIRING_SOON'"),
        "Expired": n("SELECT COUNT(*) FROM scholarships WHERE status = 'EXPIRED'"),
        "No longer verifiable": n("SELECT COUNT(*) FROM scholarships WHERE status = 'NO_LONGER_VERIFIABLE'"),
        "Recently updated (7 days)": n("SELECT COUNT(DISTINCT scholarship_id) FROM changes WHERE detected_at >= datetime('now', '-7 days')"),
        "Changes recorded": n("SELECT COUNT(*) FROM changes"),
        "Source types": n("SELECT COUNT(DISTINCT source_type) FROM scholarships"),
        "Backed by 2+ official pages": n("SELECT COUNT(*) FROM (SELECT scholarship_id FROM evidence WHERE verified = 1 GROUP BY scholarship_id HAVING COUNT(DISTINCT source_url) > 1)"),
        "Unresolved leads": n("SELECT COUNT(*) FROM leads WHERE state IN ('new', 'unresolved')"),
        "Average confidence": round(average, 1),
    }


def export_all(cfg, conn):
    folder = cfg["paths"]["data"]
    columns = ["name", "provider", "amount", "eligibility", "closing_date", "official_source_url", "application_url",
               "status", "confidence", "verification", "last_verified", "source_type"]
    columns += [f for f in ALL_FIELDS if f not in columns] + ["discovered_via", "first_seen", "id"]
    rows = storage.fetch(conn, "SELECT * FROM scholarships ORDER BY confidence DESC, name")

    with open(folder / "scholarships.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[c] for c in columns])

    detailed = []
    for row in rows:
        row["evidence"] = storage.fetch(conn, "SELECT field, value, quote, source_url, verified, note FROM evidence WHERE scholarship_id = ? AND verified = 1", (row["id"],))
        row["why_this_score"] = storage.fetch(conn, "SELECT name, points, max_points, reason FROM score_checks WHERE scholarship_id = ? ORDER BY position", (row["id"],))
        row["change_history"] = storage.fetch(conn, "SELECT field, old_value, new_value, detected_at, source_url, evidence, old_source_url, note FROM changes WHERE scholarship_id = ? ORDER BY id", (row["id"],))
        detailed.append(row)
    (folder / "scholarships.json").write_text(json.dumps(detailed, indent=2, ensure_ascii=False), encoding="utf-8")

    changes = storage.fetch(conn, "SELECT c.*, s.name FROM changes c LEFT JOIN scholarships s ON s.id = c.scholarship_id ORDER BY c.id")
    with open(folder / "changes.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["scholarship", "field", "old_value", "new_value", "detected_at", "source_url", "evidence", "old_source_url", "old_evidence", "note"])
        for c in changes:
            writer.writerow([c["name"], c["field"], c["old_value"], c["new_value"], c["detected_at"], c["source_url"],
                             c["evidence"], c["old_source_url"], c["old_evidence"], c["note"]])
    return [folder / "scholarships.csv", folder / "scholarships.json", folder / "changes.csv"]
