"""LLM extraction for Telegram scholarship posts (+ the official page they link to).

One LLM call per post decides is_scholarship AND extracts every field, so the
non-scholarship posts cost a single cheap call. The official page, when it could
be read, wins over the post text on conflicts."""
import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, field_validator

from scholarships.llm_enrich import (ARABIC, SYSTEM_PROMPT, Extraction, LLMClient,
                                     semantic_issue, verified)


class Country(BaseModel):
    code: Optional[str] = None
    name_ar: Optional[str] = None
    name_en: Optional[str] = None


class Benefit(BaseModel):
    text_ar: Optional[str] = None
    text_en: Optional[str] = None


class TgExtraction(Extraction):
    is_scholarship: bool
    not_scholarship_reason: Optional[str] = None
    title_ar: Optional[str] = None
    title_en: Optional[str] = None
    provider_name_en: Optional[str] = None
    host_countries: list[Country] = []
    deadline: Optional[str] = None
    palestinians_eligible: Optional[bool] = None
    benefits: list[Benefit] = []
    description_ar: Optional[str] = None
    description_en: Optional[str] = None

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, v):
        v = (v or "").strip() if isinstance(v, str) else None
        if not v or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return None
        try:
            date.fromisoformat(v)  # rejects 2026-13-45
        except ValueError:
            return None
        return v

    @field_validator("host_countries", mode="before")
    @classmethod
    def _countries(cls, v):
        out = []
        for c in v or []:
            if isinstance(c, dict):
                code = (c.get("code") or "").strip().upper()
                c = {**c, "code": code if re.fullmatch(r"[A-Z]{2}", code) else None}
            out.append(c)
        return out


_BASE_KEYS = SYSTEM_PROMPT[SYSTEM_PROMPT.index("study_levels:"):]

TG_SYSTEM_PROMPT = """You read a post from a Telegram channel that shares scholarships, and (when available) the text of the OFFICIAL PAGE the post links to.
Return ONE JSON object only (no markdown) with exactly these keys:

is_scholarship: decide from the POST text. true ONLY if the post announces a specific scholarship, fellowship, grant or funded study programme that people can apply to. false for news, advice, reminders, events, ads, courses without funding, job offers, greetings, results announcements, or anything else
not_scholarship_reason: short reason when is_scholarship is false, else null
title_ar: Arabic title of the scholarship: short and clean (no emoji, no "|" separators, no taglines)
title_en: English title, short and clean in the same way
provider_name_en: English name of the organisation that AWARDS the scholarship (a university, government, foundation), never a website name or domain
host_countries: array of {"code": ISO 3166-1 alpha-2 uppercase, "name_ar", "name_en"} - the country/countries where the student will STUDY (not the applicants' nationality)
deadline: application deadline as YYYY-MM-DD, only if the text gives day, month AND year; else null
palestinians_eligible: true if the text says Palestinians (or all international students) may apply, false if it excludes them, null if unclear
benefits: array (max 10) of {"text_ar", "text_en"}: the benefits explicitly listed, translated into both languages
description_ar / description_en: 1-3 short plain-text paragraphs (separated by a blank line, no emoji, no markdown) summarising the scholarship faithfully from the text
""" + _BASE_KEYS.replace(
    "evidence: object mapping study_levels, funding_type, fields_of_study_scope, funding_duration",
    "evidence: object mapping study_levels, funding_type, fields_of_study_scope, funding_duration, deadline",
) + """
The OFFICIAL PAGE only adds detail: if it is irrelevant (link shortener, ad, login, error, cookie notice) ignore it and rely on the POST. If the POST and a relevant OFFICIAL PAGE disagree, the OFFICIAL PAGE is right. When is_scholarship is false you may leave every other key null or empty."""


def build_tg_user_text(rec):
    raw = rec["raw"]
    parts = [f"POST DATE: {rec.get('updated_at') or 'unknown'}",
             "POST:\n" + (raw.get("post_text") or "")]
    page = raw.get("page_text")
    if page:
        parts.append(f"OFFICIAL PAGE ({raw.get('page_url')}):\n" + page[:8000])
    else:
        parts.append(f"OFFICIAL PAGE: not available ({raw.get('page_status')})")
    return "\n\n".join(parts)


def tg_semantic(ex):
    if not ex.is_scholarship:
        return None
    if not ex.title_ar and not ex.title_en:
        return "is_scholarship is true but there is no title_ar/title_en."
    return semantic_issue(ex)


class TelegramLLMClient(LLMClient):
    system_prompt = TG_SYSTEM_PROMPT
    schema = TgExtraction

    def build_user(self, rec):
        return build_tg_user_text(rec)

    def semantic(self, ex):
        return tg_semantic(ex)


def apply_tg_extraction(rec, ex, meta):
    """Fill the contract-shaped draft record from the extraction. Returns flags."""
    flags = []
    blob = build_tg_user_text(rec)
    rec["is_scholarship"] = ex.is_scholarship
    ev = {k: v for k, v in (ex.evidence or {}).items() if v}
    rec["llm"] = {**meta, "evidence": ev}

    if not ex.is_scholarship:
        rec["not_scholarship_reason"] = ex.not_scholarship_reason
        flags.append("not_scholarship")
        rec["review_flags"].extend(f for f in flags if f not in rec["review_flags"])
        return flags

    bad = [k for k, v in ev.items() if not verified(v, blob)]
    flags += [f"llm_evidence_unverified:{k}" for k in bad]

    rec["title_ar"] = ex.title_ar or ex.title_en
    rec["title_en"] = ex.title_en
    if not ex.title_en:
        flags.append("title_en_missing")
    rec["provider_name_en"] = ex.provider_name_en
    rec["provider_name_ar"] = ex.provider_name_ar if ex.provider_name_ar and ARABIC.search(ex.provider_name_ar) else None
    if not rec["provider_name_ar"]:
        flags.append("provider_name_ar_missing")
    rec["host_countries"] = [c.model_dump() for c in ex.host_countries]
    if not rec["host_countries"]:
        flags.append("host_country_missing")

    rec["study_levels"] = ex.study_levels
    if not ex.study_levels:
        flags.append("levels_missing")
    rec["funding_type"] = ex.funding_type
    if not ex.funding_type:
        flags.append("funding_missing")

    scope = ex.fields_of_study_scope
    rec["fields_of_study_scope"] = scope or "ALL"
    rec["fields_of_study"] = [] if scope in (None, "ALL") else [f.model_dump() for f in ex.fields_of_study]
    if scope is None:
        flags.append("fields_scope_assumed_all")
    elif scope == "SPECIFIC" and not rec["fields_of_study"]:
        flags.append("fields_missing")

    rec["financial_coverage_ar"] = ex.financial_coverage_ar
    rec["financial_coverage_en"] = ex.financial_coverage_en
    rec["funding_duration"] = ex.funding_duration.model_dump() if ex.funding_duration else None
    rec["required_documents"] = [{"order": i + 1, **d.model_dump()}
                                 for i, d in enumerate(ex.required_documents)]
    rec["benefits"] = [{"order": i + 1, **b.model_dump()} for i, b in enumerate(ex.benefits)]
    rec["description_ar"] = ex.description_ar
    rec["description_en"] = ex.description_en
    flags.append("description_llm_written")

    rec["deadline"] = ex.deadline
    if not ex.deadline:
        flags.append("deadline_missing")
    rec["palestinians_eligible"] = ex.palestinians_eligible
    if ex.palestinians_eligible is False:
        flags.append("palestinians_not_eligible")

    status = rec["raw"].get("page_status")
    if status == "closed":
        flags.append("application_closed")
    elif status != "ok":
        flags.append("official_page_" + str(status))
    if ex.deadline and ex.deadline < date.today().isoformat():
        flags.append("deadline_passed")
    rec["review_flags"].extend(f for f in flags if f not in rec["review_flags"])
    return flags