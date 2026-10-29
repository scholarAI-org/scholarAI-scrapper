"""LLM enrichment for for9a records (no scrapy dependency, easy to test).

Fills the fields for9a does not provide in structured form:
fields_of_study(+scope), required_documents, financial_coverage,
funding_duration (+ study_levels / funding_type / Arabic provider name when
they are missing).

Providers: Groq first, Gemini as fallback (override the order with LLM_PROVIDERS=gemini,groq). Each provider may have several API
keys (GEMINI_API_KEY, GEMINI_API_KEY_2, ... or GEMINI_API_KEYS=a,b); calls
alternate between keys, and a key that is rate limited / rejected is skipped.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

LEVELS = ("BACHELOR", "MASTER", "PHD", "DIPLOMA", "POSTDOC")
DOC_TYPES = ("TRANSCRIPT", "RECOMMENDATION_LETTER", "MOTIVATION_LETTER",
             "PASSPORT", "LANGUAGE_CERTIFICATE", "CV", "OTHER")
ARABIC = re.compile(r"[؀-ۿ]")


# ------------------------------------------------------------------ schema
def _fill_names(obj):
    """Models often leave one language empty: reuse the other instead of failing."""
    if not obj.name_ar and not obj.name_en:
        raise ValueError("both name_ar and name_en are empty")
    obj.name_ar = obj.name_ar or obj.name_en
    obj.name_en = obj.name_en or obj.name_ar
    return obj


class FieldOfStudy(BaseModel):
    code: str
    name_ar: Optional[str] = None
    name_en: Optional[str] = None

    @model_validator(mode="after")
    def _names(self):
        return _fill_names(self)


class Duration(BaseModel):
    min_months: int = Field(ge=1, le=120)
    max_months: int = Field(ge=1, le=120)

    @model_validator(mode="after")
    def _order(self):
        if self.min_months > self.max_months:
            raise ValueError("min_months > max_months")
        return self


class RequiredDoc(BaseModel):
    name_ar: Optional[str] = None
    name_en: Optional[str] = None
    document_type: str = "OTHER"
    is_mandatory: bool = True

    @model_validator(mode="after")
    def _names(self):
        return _fill_names(self)

    @field_validator("document_type", mode="before")
    @classmethod
    def _type(cls, v):
        v = str(v or "OTHER").upper().strip()
        return v if v in DOC_TYPES else "OTHER"


class Extraction(BaseModel):
    study_levels: list[str] = []
    fields_of_study_scope: Optional[Literal["ALL", "SPECIFIC"]] = None
    fields_of_study: list[FieldOfStudy] = []
    funding_type: Optional[Literal["FULL", "PARTIAL", "NONE"]] = None
    financial_coverage_ar: Optional[str] = None
    financial_coverage_en: Optional[str] = None
    funding_duration: Optional[Duration] = None
    required_documents: list[RequiredDoc] = []
    provider_name_ar: Optional[str] = None
    evidence: dict[str, Optional[str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data):
        """Models often send null instead of []/{} for fields that do not apply."""
        if isinstance(data, dict):
            data = dict(data)
            for k in ("study_levels", "fields_of_study", "required_documents",
                      "benefits", "host_countries"):
                if k in data and data[k] is None:
                    data[k] = []
            ev = data.get("evidence")
            data["evidence"] = ({k: (v if isinstance(v, str) else None) for k, v in ev.items()}
                                if isinstance(ev, dict) else {})
        return data

    @field_validator("study_levels", mode="before")
    @classmethod
    def _levels(cls, v):
        out = []
        for x in v or []:
            x = str(x).upper().strip()
            if x in LEVELS and x not in out:
                out.append(x)
        return out

    @field_validator("funding_duration", mode="before")
    @classmethod
    def _dur(cls, v):
        # {"min_months": null, "max_months": null} -> None
        if isinstance(v, dict) and not (v.get("min_months") and v.get("max_months")):
            return None
        return v


# ------------------------------------------------------------------ prompt
SYSTEM_PROMPT = """You extract structured data about a scholarship from the text provided.
Return ONE JSON object only (no markdown) with exactly these keys:

study_levels: array of BACHELOR | MASTER | PHD | DIPLOMA | POSTDOC (levels the scholarship is open to)
fields_of_study means what the student will STUDY or research in this programme/award, NOT the academic backgrounds required of applicants (a master's in humanitarian action is SPECIFIC with field "International Humanitarian Action", even if applicants may come from any background).
fields_of_study_scope: "ALL" ONLY if the text explicitly says any/all disciplines or fields; "SPECIFIC" if it names one field or a list of disciplines; null if unknown
A list introduced by "such as", "including", "e.g.", "for example", "and other disciplines/fields" is only EXAMPLES, not a restriction: do not make the scope SPECIFIC from it (use ALL if the text implies open to the listed area and others, else null). SPECIFIC only when the award is restricted to the named field(s).
fields_of_study: array of {"code": UPPER_SNAKE_CASE, "name_ar": Arabic, "name_en": English}; when scope is SPECIFIC it MUST contain every named field (never empty); [] only when scope is ALL or null
funding_type: FULL | PARTIAL | NONE | null  (FULL = tuition + living costs covered; PARTIAL = part of the costs or a fixed amount; NONE = no money)
financial_coverage_ar / financial_coverage_en: ONE short sentence (max 200 chars) summarising what money is given (stipend, insurance, travel...), or null
funding_duration: {"min_months": int, "max_months": int} if a duration is stated (e.g. "two years" -> 24/24), else null. If the award covers several study levels with different durations, or the quote only supports one level, use null unless the quote itself covers all levels; never estimate
required_documents: array of {"name_ar", "name_en", "document_type": TRANSCRIPT | RECOMMENDATION_LETTER | MOTIVATION_LETTER | PASSPORT | LANGUAGE_CERTIFICATE | CV | OTHER, "is_mandatory": bool}, only documents explicitly mentioned; is_mandatory is false when the text says optional / if applicable / if needed, or when the document applies only to some applicants (e.g. only one study level, so when the required documents differ by study level, such as high school documents for bachelor vs university documents for master, ALL those level-specific documents are false); otherwise true. document_type: use TRANSCRIPT only for grade records/transcripts; diplomas, degree certificates and school-leaving certificates are OTHER
provider_name_ar: Arabic name of the awarding organisation/university (translate or transliterate it, e.g. "جامعة كانساس"); null only if there is no clear organisation
evidence: object mapping study_levels, funding_type, fields_of_study_scope, funding_duration to a SHORT VERBATIM quote (max 160 chars) copied from the provided text that supports your answer, or null

Rules: use ONLY the provided text; never guess. If something is not stated, use null or []. Quotes must be copied exactly from the text."""


def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " ..."


def build_user_text(rec):
    raw = rec.get("raw", {})
    benefits = "\n".join(f"- {b['text_en'] or b['text_ar']}" for b in rec.get("benefits", []))
    parts = [
        f"TITLE (EN): {rec.get('title_en') or ''}",
        f"TITLE (AR): {rec.get('title_ar') or ''}",
        f"PROVIDER: {rec.get('provider_name_en') or ''}",
        f"COUNTRIES: {', '.join(c['name_en'] or c['name_ar'] for c in rec.get('host_countries', []))}",
        f"SOURCE FUNDING LABEL: {raw.get('funding_type_en') or raw.get('funding_type_ar') or 'unknown'}",
        f"SOURCE FIELD CATEGORIES (unreliable): {', '.join(raw.get('fields_en') or [])}",
        "DESCRIPTION:\n" + _clip(rec.get("description_en") or rec.get("description_ar"), 3000),
        "BENEFITS:\n" + _clip(benefits, 1500),
        "ELIGIBILITY:\n" + _clip(raw.get("eligibility_en") or raw.get("eligibility_ar"), 2000),
        "HOW TO APPLY (Arabic):\n" + _clip(raw.get("how_to_apply"), 1500),
    ]
    return "\n\n".join(parts)


# --------------------------------------------------------------------- http
class LLMHTTPError(Exception):
    def __init__(self, code, body="", retry_after=None):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code, self.retry_after = code, retry_after


class LLMError(Exception):
    pass


DEFAULT_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))


def http_post(url, headers, body, timeout=None):
    timeout = timeout or DEFAULT_TIMEOUT
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Groq sits behind Cloudflare, which rejects urllib's default
        # "Python-urllib/x.y" signature with error 1010.
        "User-Agent": "Mozilla/5.0 (compatible; ScholarAI-Scraper/1.0)",
        **headers,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        ra = e.headers.get("retry-after") if e.headers else None
        raise LLMHTTPError(e.code, e.read().decode("utf-8", "ignore"),
                           float(ra) if ra and ra.replace(".", "").isdigit() else None)
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMHTTPError(0, repr(e))


def _gemini_generation_config():
    cfg = {"temperature": 0, "responseMimeType": "application/json"}
    level = os.getenv("GEMINI_THINKING_LEVEL", "").strip()  # opt-in, e.g. "low"
    if level:
        cfg["thinkingConfig"] = {"thinkingLevel": level}
    return cfg


def call_gemini(key, model, system, user, post=http_post):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    data = post(url, {"x-goog-api-key": key}, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": _gemini_generation_config(),
    })
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_groq(key, model, system, user, post=http_post):
    data = post("https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": f"Bearer {key}"}, {
        "model": model, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    })
    return data["choices"][0]["message"]["content"]


def load_keys(env, prefix):
    """GEMINI_API_KEY, GEMINI_API_KEY_2.._9 and GEMINI_API_KEYS=a,b,c"""
    keys = [env.get(prefix, "")]
    keys += [env.get(f"{prefix}_{i}", "") for i in range(2, 10)]
    keys += (env.get(prefix + "S", "") or "").split(",")
    out = []
    for k in (k.strip() for k in keys):
        if k and k not in out:
            out.append(k)
    return out


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    return json.loads(t)


def semantic_issue(ex):
    """A problem the model can fix if told about it, else None."""
    if ex.fields_of_study_scope == "SPECIFIC" and not ex.fields_of_study:
        return ("fields_of_study_scope is SPECIFIC but fields_of_study is empty. "
                "List the disciplines named in the text (both name_ar and name_en), "
                "or set fields_of_study_scope to null if they are not stated.")
    not_ar = [f.name_en for f in ex.fields_of_study if not ARABIC.search(f.name_ar or "")]
    not_ar += [d.name_en for d in ex.required_documents if not ARABIC.search(d.name_ar or "")]
    if not_ar:
        return ("name_ar must be written in Arabic letters (translate it), but these have no "
                "Arabic: " + "; ".join(not_ar[:12]) + ". Keep name_en unchanged.")
    return None


# ------------------------------------------------------------------- client
class LLMClient:
    """Gemini then Groq; several keys per provider, alternated per call."""

    # hooks, overridden by other sources (e.g. Telegram posts)
    system_prompt = SYSTEM_PROMPT
    schema = Extraction

    def build_user(self, rec):
        return build_user_text(rec)

    def semantic(self, ex):
        return semantic_issue(ex)

    MAX_WAIT = 30  # never sleep longer than this for one key; try another
    COOLDOWN = float(os.getenv("LLM_COOLDOWN", "300"))  # pause a failing provider

    def __init__(self, env=None, post=http_post, sleep=time.sleep, clock=time.monotonic):
        env = os.environ if env is None else env
        self.post, self.sleep, self.clock = post, sleep, clock
        self.providers = []
        order = [x.strip().lower() for x in env.get("LLM_PROVIDERS", "").split(",") if x.strip()]
        only = set(order)
        known = (
            ("groq", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b", 20, call_groq),
            ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-3.5-flash", 4, call_gemini),
        )
        if order:  # explicit order wins
            known = sorted(known, key=lambda p: order.index(p[0]) if p[0] in order else 99)
        for name, prefix, model_env, model, interval, fn in known:
            keys = load_keys(env, prefix)
            if keys and (not only or name in only):
                self.providers.append({
                    "name": name, "model": env.get(model_env, model), "fn": fn,
                    "interval": float(env.get(f"{name.upper()}_MIN_INTERVAL", interval)),
                    "keys": [{"key": k, "next": 0.0, "dead": False} for k in keys],
                    "rr": 0, "disabled": None, "streak": 0, "cooldown_until": 0.0,
                })

    @property
    def enabled(self):
        return bool(self.providers)

    def _ask(self, prov, st, user):
        text = prov["fn"](st["key"], prov["model"], self.system_prompt, user, self.post)
        st["next"] = self.clock() + prov["interval"]
        return self.schema.model_validate(parse_json(text))

    def _attempt(self, prov, st, user):
        """One call; if the answer is invalid or inconsistent, ask ONCE more on
        the same key, telling the model what was wrong."""
        ex, problem = None, None
        try:
            ex = self._ask(prov, st, user)
            problem = self.semantic(ex)
        except LLMHTTPError:
            raise
        except Exception as e:  # bad JSON / schema
            problem = f"your JSON was invalid: {re.sub(chr(10), ' ', str(e))[:300]}"
        if problem is None:
            return ex
        wait = st["next"] - self.clock()
        if wait > 0:
            self.sleep(wait)
        try:
            return self._ask(prov, st, f"{user}\n\nYour previous answer was rejected: {problem}\nReturn the corrected JSON.")
        except LLMHTTPError:
            raise
        except Exception:
            if ex is not None:
                return ex  # keep the first (consistent-enough) answer; flags will show it
            raise

    def extract(self, rec):
        """Return (Extraction, meta). Raises LLMError if every key/provider fails."""
        user = self.build_user(rec)
        errors = []
        started = self.clock()
        for prov in self.providers:
            if prov["disabled"]:
                errors.append(f"{prov['name']}: disabled ({prov['disabled']})")
                continue
            if prov["cooldown_until"] > self.clock():
                errors.append(f"{prov['name']}: paused after repeated timeouts/5xx")
                continue
            n = len(prov["keys"])
            for step in range(n):
                idx = (prov["rr"] + step) % n
                st = prov["keys"][idx]
                label = f"{prov['name']}#{idx + 1}"
                if st["dead"]:
                    continue
                wait = st["next"] - self.clock()
                if wait > self.MAX_WAIT:
                    errors.append(f"{label}: cooling down {wait:.0f}s")
                    continue
                if wait > 0:
                    self.sleep(wait)
                try:
                    result = self._attempt(prov, st, user)
                except LLMHTTPError as e:
                    errors.append(f"{label}: {e}")
                    if e.code == 404:
                        # unknown / retired model: every key fails the same way,
                        # so stop using this provider for the rest of the run
                        prov["disabled"] = f"model {prov['model']} not found, set {prov['name'].upper()}_MODEL"
                        break
                    if e.code == 0 or e.code >= 500:
                        # timeout / server trouble is the service's, not the key's:
                        # other keys would only wait the same timeout again
                        prov["streak"] += 1
                        if prov["streak"] >= 2:
                            prov["cooldown_until"] = self.clock() + self.COOLDOWN
                        break
                    if e.code in (401, 403):
                        st["dead"] = True
                    elif e.code == 429:
                        st["next"] = self.clock() + (e.retry_after or 60)
                    else:
                        st["next"] = self.clock() + prov["interval"]
                    continue
                except Exception as e:  # bad JSON / schema / unexpected payload
                    st["next"] = self.clock() + prov["interval"]
                    errors.append(f"{label}: invalid output ({type(e).__name__}: {str(e)[:120]})")
                    continue
                prov["streak"] = 0
                prov["rr"] = (idx + 1) % n  # alternate keys on the next call
                return result, {"provider": prov["name"], "model": prov["model"],
                                "key": idx + 1, "seconds": round(self.clock() - started, 1),
                                "failed_attempts": errors}
        raise LLMError("; ".join(errors) or "no LLM keys configured")


# ------------------------------------------------------------------- merge
def _alnum(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def verified(quote, source_blob):
    """True if the quote really appears in the source (punctuation-insensitive)."""
    quote = (quote or "").replace("\\n", " ").replace("\\t", " ")  # model sometimes returns a literal \n
    blob = _alnum(source_blob)
    parts = [_alnum(p) for p in re.split(r"\.\.\.|\u2026", quote or "")]
    parts = [p for p in parts if len(p) >= 6]  # ignore tiny fragments around "..."
    return bool(parts) and all(p in blob for p in parts)


def apply_extraction(rec, ex, meta):
    """Merge an Extraction into a record. Returns the list of flags added."""
    flags = []
    blob = build_user_text(rec)

    # evidence check: every quote must exist in the source text
    ev = {k: v.replace("\\n", " ").replace("\\t", " ") for k, v in (ex.evidence or {}).items() if v}
    bad = [k for k, v in ev.items() if not verified(v, blob)]
    for k in bad:
        flags.append(f"llm_evidence_unverified:{k}")
    ok = lambda field: field not in bad  # noqa: E731

    # study levels: keep the deterministic result, LLM fills the gap / flags conflict
    if ex.study_levels:
        if not rec["study_levels"]:
            rec["study_levels"] = ex.study_levels
            flags.append("levels_from_llm")
            if "levels_missing" in rec["review_flags"]:
                rec["review_flags"].remove("levels_missing")
        elif set(rec["study_levels"]) != set(ex.study_levels):
            flags.append("llm_level_disagrees")

    # funding type
    if ex.funding_type:
        if not rec["funding_type"]:
            rec["funding_type"] = ex.funding_type
            flags.append("funding_from_llm")
            if "funding_missing" in rec["review_flags"]:
                rec["review_flags"].remove("funding_missing")
        elif rec["funding_type"] != ex.funding_type:
            flags.append("llm_funding_disagrees")

    # fields of study
    scope = ex.fields_of_study_scope
    rec["fields_of_study_scope"] = scope
    rec["fields_of_study"] = [] if scope == "ALL" else [f.model_dump() for f in ex.fields_of_study]
    if scope is None:
        # the frontend contract requires ALL or SPECIFIC: no restriction found -> ALL,
        # but tell the Admin it was an assumption
        rec["fields_of_study_scope"] = "ALL"
        flags.append("fields_scope_assumed_all")
    elif scope == "SPECIFIC" and not rec["fields_of_study"]:
        flags.append("fields_missing")

    if any(not ARABIC.search(f.name_ar or "") for f in ex.fields_of_study):
        flags.append("fields_names_not_arabic")
    rec["financial_coverage_ar"] = ex.financial_coverage_ar
    rec["financial_coverage_en"] = ex.financial_coverage_en
    rec["funding_duration"] = ex.funding_duration.model_dump() if ex.funding_duration else None
    rec["required_documents"] = [
        {"order": i + 1, **d.model_dump()} for i, d in enumerate(ex.required_documents)
    ]

    # Arabic provider name only when the scraped one has no Arabic letters
    if ex.provider_name_ar and ARABIC.search(ex.provider_name_ar) \
            and not ARABIC.search(rec.get("provider_name_ar") or ""):
        rec["provider_name_ar"] = ex.provider_name_ar

    rec["llm"] = {**meta, "evidence": ev}
    rec["review_flags"].extend(f for f in flags if f not in rec["review_flags"])
    return flags