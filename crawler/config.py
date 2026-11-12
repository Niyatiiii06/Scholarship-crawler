"""Reads config.yaml (settings) and .env (API keys). Also defines the record fields."""
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_FOLDER = Path(__file__).resolve().parent.parent

NOT_SPECIFIED = "Not specified"

# Every scholarship record has these fields. Each one is stored with its evidence.
TEXT_FIELDS = ["provider", "amount", "eligibility", "academic_requirements", "education_level",
               "income_criteria", "age_criteria", "gender_criteria", "category_criteria",
               "domicile_requirements", "institution_requirements", "disability_criteria",
               "documents_required", "selection_process", "renewal_requirements", "application_mode"]
DATE_FIELDS = ["opening_date", "closing_date"]
NUMBER_FIELDS = ["income_limit_inr"]
URL_FIELDS = ["application_url"]
ALL_FIELDS = ["name"] + TEXT_FIELDS + DATE_FIELDS + NUMBER_FIELDS + URL_FIELDS

ELIGIBILITY_FIELDS = ["academic_requirements", "education_level", "income_criteria", "age_criteria",
                      "gender_criteria", "category_criteria", "domicile_requirements",
                      "institution_requirements", "disability_criteria"]
IMPORTANT_FIELDS = ["name", "provider", "amount", "eligibility", "closing_date", "application_url"]


def load_config(data_folder=None):
    load_dotenv(PROJECT_FOLDER / ".env")
    cfg = yaml.safe_load((PROJECT_FOLDER / "config.yaml").read_text(encoding="utf-8"))
    data = Path(data_folder or PROJECT_FOLDER / "data")
    data.mkdir(parents=True, exist_ok=True)
    cfg["paths"] = {"data": data, "db": data / "scholarships.db"}
    return cfg


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return date.today()
