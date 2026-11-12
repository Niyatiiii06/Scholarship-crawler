"""Dashboard. Run with:  streamlit run app.py"""
import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from crawler import export
from crawler.config import ALL_FIELDS, NOT_SPECIFIED

DB_PATH = Path(__file__).parent / "data" / "scholarships.db"

FIELD_LABELS = {"provider": "Provider", "amount": "Amount / benefit", "eligibility": "Eligibility",
                "academic_requirements": "Academic requirements", "education_level": "Course / education level",
                "income_criteria": "Income criteria", "income_limit_inr": "Income limit (INR, machine-readable)",
                "age_criteria": "Age criteria", "gender_criteria": "Gender criteria", "category_criteria": "Category criteria",
                "domicile_requirements": "Domicile / state", "institution_requirements": "Institution requirements",
                "disability_criteria": "Disability criteria", "opening_date": "Opening date", "closing_date": "Closing date",
                "documents_required": "Documents required", "selection_process": "Selection process",
                "renewal_requirements": "Renewal requirements", "application_mode": "How to apply",
                "application_url": "Application link"}

st.set_page_config(page_title="Scholarship Intelligence", layout="wide")


def query(sql, params=()):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


st.title("Scholarship Intelligence")
st.caption("Every record below was read from an official source page. Each value links back to the exact quote it came from.")

if not DB_PATH.exists():
    st.warning("No database yet. Run `python main.py run` first.")
    st.stop()

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
numbers = export.stats(conn)
last_run = conn.execute("SELECT finished_at, summary FROM runs ORDER BY id DESC LIMIT 1").fetchone()
conn.close()

top = st.columns(7)
for column, label in zip(top, ["Total discovered", "Verified (95%+)", "Review required", "Active", "Expired",
                               "Recently updated (7 days)", "Average confidence"]):
    value = numbers[label]
    column.metric(label, f"{value}%" if label == "Average confidence" else value)

more = st.columns(5)
for column, label in zip(more, ["On an official source", "Expiring soon", "No longer verifiable", "Changes recorded", "Source types"]):
    column.metric(label, numbers[label])

if last_run:
    summary = json.loads(last_run["summary"])
    st.caption(f"Last crawler run finished {last_run['finished_at']} - new: {summary['new']}, unchanged: {summary['unchanged']}, "
               f"changed: {summary['changed']}, expired: {summary['expired']}, no longer verifiable: {summary['no_longer_verifiable']}")

st.divider()
all_rows = query("SELECT * FROM scholarships ORDER BY confidence DESC, name")
if all_rows.empty:
    st.info("The database has no scholarships yet.")
    st.stop()

filters = st.columns([3, 2, 2, 2])
search = filters[0].text_input("Search", placeholder="name, provider, eligibility...")
verification = filters[1].multiselect("Verification", sorted(all_rows["verification"].unique()))
status = filters[2].multiselect("Status", sorted(all_rows["status"].unique()))
source_type = filters[3].multiselect("Source type", sorted(all_rows["source_type"].unique()))

rows = all_rows
if search:
    text = rows[["name", "provider", "eligibility", "amount", "education_level"]].astype(str).agg(" ".join, axis=1).str.lower()
    rows = rows[text.str.contains(search.lower(), regex=False)]
if verification:
    rows = rows[rows["verification"].isin(verification)]
if status:
    rows = rows[rows["status"].isin(status)]
if source_type:
    rows = rows[rows["source_type"].isin(source_type)]

st.subheader(f"Scholarships ({len(rows)})")
table = rows[["name", "provider", "amount", "closing_date", "status", "confidence", "verification", "source_type", "last_verified"]]
st.dataframe(table, width="stretch", hide_index=True,
             column_config={"confidence": st.column_config.ProgressColumn("confidence", min_value=0, max_value=100, format="%.1f%%")})

if rows.empty:
    st.stop()

st.divider()
st.subheader("Scholarship details")
chosen_name = st.selectbox("Open a scholarship", rows["name"].tolist())
item = rows[rows["name"] == chosen_name].iloc[0]

head = st.columns(4)
head[0].metric("Confidence", f"{item['confidence']}%")
head[1].metric("Verification", item["verification"])
head[2].metric("Status", item["status"])
head[3].metric("Last verified", str(item["last_verified"])[:10])

st.markdown(f"### {item['name']}")
st.markdown(f"**Provider:** {item['provider']}  \n**Amount:** {item['amount']}  \n**Deadline:** {item['closing_date']}")
st.markdown(f"**Official source:** {item['official_source_url']}  \n**Source type:** {item['source_type']} - {item['source_reason']}")
if item["application_url"] != NOT_SPECIFIED:
    st.markdown(f"**Application link:** {item['application_url']}")
st.caption(f"Discovered via: {item['discovered_via']} | first seen {str(item['first_seen'])[:10]} | extracted by {item['extracted_by']}")

tab_fields, tab_score, tab_changes, tab_evidence = st.tabs(["Eligibility and details", "Why this score?", "Change history", "Source evidence"])

with tab_fields:
    for field in ALL_FIELDS:
        if field in ("name", "provider", "amount", "closing_date", "application_url"):
            continue
        st.markdown(f"**{FIELD_LABELS[field]}:** {item[field]}")

with tab_score:
    checks = query("SELECT name, points, max_points, reason FROM score_checks WHERE scholarship_id = ? ORDER BY position", (item["id"],))
    st.markdown(f"The confidence is the sum of nine evidence checks: **{checks['points'].sum():.1f} / {checks['max_points'].sum():.0f}**. "
                "VERIFIED needs 95 or more, an official source and no conflicting source.")
    st.dataframe(checks.rename(columns={"name": "check", "max_points": "out of"}), width="stretch", hide_index=True)
    rejected = json.loads(item["rejected"] or "[]")
    if rejected:
        st.markdown("**Values the extractor proposed but the system rejected (no supporting evidence on the page):**")
        for line in rejected:
            st.markdown("- " + line)

with tab_changes:
    changes = query("SELECT detected_at, field, old_value, new_value, evidence, old_evidence, source_url, old_source_url, note "
                    "FROM changes WHERE scholarship_id = ? ORDER BY id DESC", (item["id"],))
    if changes.empty:
        st.info("No changes recorded for this scholarship yet.")
    else:
        st.dataframe(changes, width="stretch", hide_index=True)

with tab_evidence:
    st.markdown("Database value → official source → exact quote on that page.")
    evidence = query("SELECT field, value, quote, source_url, note FROM evidence WHERE scholarship_id = ? AND verified = 1", (item["id"],))
    st.dataframe(evidence, width="stretch", hide_index=True)

st.divider()
with st.expander("All recorded changes"):
    st.dataframe(query("SELECT c.detected_at, s.name AS scholarship, c.field, c.old_value, c.new_value, c.evidence, c.note "
                       "FROM changes c LEFT JOIN scholarships s ON s.id = c.scholarship_id ORDER BY c.id DESC"),
                 width="stretch", hide_index=True)
with st.expander("Leads waiting for an official source (names seen on aggregators; no data stored)"):
    st.dataframe(query("SELECT name, found_on_url, state, resolved_url FROM leads ORDER BY found_at DESC"),
                 width="stretch", hide_index=True)
