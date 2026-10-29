"""Pure parsing helpers for for9a data (no scrapy dependency, easy to test).

build_record() turns the raw for9a payloads (Arabic page, English page,
application info) into a record shaped like the frontend contract.
Fields that need an LLM (fields_of_study, required_documents, ...) are left
empty here and the raw material for them is kept under "raw".
"""
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

# ----------------------------------------------------------------- mappings
FUNDING_MAP = {
    "ممولة بالكامل": "FULL",
    "fully funded": "FULL",
    "ممولة جزئيا": "PARTIAL",
    "ممولة جزئياً": "PARTIAL",
    "partial funding": "PARTIAL",
    "partially funded": "PARTIAL",
}

# order matters: POSTDOC must be tested before PHD
# Latin words are matched as whole words so "Mastercard" is not "master" and
# "Doctors Without Borders" is not "doctor". Arabic words are plain substrings.
LEVEL_KEYWORDS = [
    ("POSTDOC", r"post-?doc\w*|ما بعد الدكتوراه"),
    ("PHD", r"\bph\.?d\b|\bdoctoral\b|\bdoctorate\b|دكتوراه|دكتوراة"),
    ("MASTER", r"\bmasters?\b|\bmaster['’]s\b|ماجستير|ماستر"),
    ("BACHELOR", r"\bbachelors?\b|\bbachelor['’]s\b|\bundergraduate\b|بكالوريوس|بكالوريس"),
    ("DIPLOMA", r"\bdiploma\b|دبلوم"),
]

SECTION_CLASSES = ("description", "benefits", "qualifications")

AUDIENCE = "PALESTINIAN"  # for9a is read with the "Palestinians" nationality filter

# for9a "Nationality" criterion: open to everyone, or a list of nationalities
NATIONALITY_LABELS = ("الجنسية", "Nationality")
OPEN_TO_ALL = re.compile(r"لا يشترط جنسية|no specific nationality", re.I)
PALESTINIAN = re.compile(r"فلسطين|palestin", re.I)


# ------------------------------------------------------------------ helpers
def _norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def detect_levels(text):
    """Return ordered list of study levels mentioned in text."""
    t = (text or "").lower()
    found = []
    for level, pattern in LEVEL_KEYWORDS:
        # "postdoctoral" must not also count as PHD
        haystack = re.sub(LEVEL_KEYWORDS[0][1], " ", t) if level == "PHD" else t
        if re.search(pattern, haystack):
            found.append(level)
    return found


def map_funding(*names):
    for n in names:
        if n:
            v = FUNDING_MAP.get(_norm(n).lower())
            if v:
                return v
    return None


def _own_text(el):
    """Text of an element without its nested lists."""
    clone = BeautifulSoup(str(el), "html.parser")
    root = clone.find(el.name)
    for sub in root.find_all(["ul", "ol"]):
        sub.decompose()
    for sep in root.find_all(["br", "p", "div"]):
        sep.append(" ")  # keep block/line breaks as a space
    # "" (not " ") so <strong>x</strong>, stays "x," instead of "x ,"
    return _norm(root.get_text(""))


def _is_link_only(el):
    """True for promo lines: a short paragraph linking to another for9a page
    (e.g. "Explore Fully Funded Scholarships") or made only of link text."""
    anchors = el.find_all("a")
    if not anchors:
        return False
    text = _norm(el.get_text(""))
    if len(text) <= 150 and any("for9a.com" in (a.get("href") or "")
                                for a in anchors):
        return True
    clone = BeautifulSoup(str(el), "html.parser")
    for a in clone.find_all("a"):
        a.decompose()
    return not _norm(clone.get_text(""))


def html_to_paragraphs(html):
    """HTML -> list of plain-text paragraphs (no HTML)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        if el.name == "p" and el.find_parent("li"):
            continue  # already included through its <li>
        if _is_link_only(el):
            continue
        text = _own_text(el)
        if text:
            out.append(text)
    return out


def split_sections(descriptions):
    """Return {"description": html, "benefits": html, "qualifications": html}."""
    sections = {}
    for idx, sec in enumerate(descriptions or []):
        body = sec.get("body") or ""
        soup = BeautifulSoup(body, "html.parser")
        name = None
        for cls in SECTION_CLASSES:
            # class may be "benefits" or "benefits_ar" / "benefits_en"
            if soup.find(class_=re.compile(rf"^{cls}(_ar|_en)?$")):
                name = cls
                break
        if name is None and idx < len(SECTION_CLASSES):
            name = SECTION_CLASSES[idx]  # fallback: position
        if name and name not in sections:
            sections[name] = body
    return sections


def extract_benefits(html):
    """Benefit items as list of strings (leaf bullets, intro lines dropped)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.find_all("li"):
        if _is_link_only(li):
            continue
        text = _own_text(li)
        if not text:
            continue
        has_children = bool(li.find(["ul", "ol"]))
        if has_children and text.rstrip().endswith((":", "：")):
            continue  # intro line such as "The fellowship includes:"
        items.append(text)
    if not items:  # section written as plain paragraphs
        items = html_to_paragraphs(html)
    return items


def criteria_values(opp, *labels):
    """Values of the first criteria block whose label is in labels."""
    for key in ("opportunity_criteria", "applicant_criteria"):
        for c in (opp or {}).get(key) or []:
            if _norm(c.get("label")) in labels:
                return [_norm(v) for v in (c.get("value") or []) if _norm(v)]
    return []


def palestinians_eligible(values):
    """True / False / None from the for9a nationality criterion values.
    open to all -> True; a list that names Palestinians -> True; a list that
    does not -> False; no criterion at all -> None (unknown)."""
    text = " | ".join(values or [])
    if not text.strip():
        return None
    if OPEN_TO_ALL.search(text) or PALESTINIAN.search(text):
        return True
    return False


def ts_to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def cover_url(opp):
    img = (opp or {}).get("image")
    if not img or not img.get("name"):
        return None
    return ("https://images.for9a.com/thumb/max-2000-auto-100-jpeg/"
            f"opportunity/{img['name']}")


def host_countries(ar, en):
    en_names = {(l.get("code") or "").lower(): l.get("name")
                for l in (en or {}).get("locations") or []}
    out = []
    for l in (ar or {}).get("locations") or []:
        code = (l.get("code") or "").lower()
        if not code:
            continue
        out.append({
            "code": code.upper(),
            "name_ar": _norm(l.get("name")),
            "name_en": _norm(en_names.get(code)) or None,
        })
    return out


def pair_benefits(ar_items, en_items):
    """Pair AR/EN benefits by index ONLY when both lists have the same length.

    When the counts differ the two lists are not translations of each other
    (for9a sometimes puts eligibility items in the EN benefits), so pairing by
    index would attach the wrong English text to an Arabic benefit. In that
    case the Arabic list is the source of truth and text_en stays None.
    """
    if ar_items and en_items and len(ar_items) != len(en_items):
        en_items = []
    base = ar_items or en_items
    return [
        {
            "order": i + 1,
            "text_ar": ar_items[i] if i < len(ar_items) else None,
            "text_en": en_items[i] if i < len(en_items) else None,
        }
        for i in range(len(base))
    ]


PROMO_LINE = re.compile(
    r"اضغط (على )?الرابط (بالأسفل|أدناه)|click (on )?the link below", re.I
)


def clean_how_to_apply(html):
    """how_to_apply HTML -> plain paragraphs, without for9a's generic
    'click the link below' footer."""
    paras = [p for p in html_to_paragraphs(html) if not PROMO_LINE.search(p)]
    return "\n\n".join(paras) or None


# ------------------------------------------------------------------- record
def build_record(summary, ar, en, app):
    """summary: list-query item, ar/en: opportunity dicts (en may be None),
    app: applicationInfo dict (may be None)."""
    en = en or {}
    app = app or {}
    flags = []

    # --- titles
    title_ar = _norm(ar.get("title") or summary.get("title"))
    title_en = _norm(en.get("title")) or None
    if not title_en:
        flags.append("title_en_missing")

    # --- funding
    raw_ar = (ar.get("fundingType") or {}).get("name")
    raw_en = (en.get("fundingType") or {}).get("name")
    funding = map_funding(raw_ar, raw_en)
    if not funding:
        flags.append("funding_missing")
    else:
        a, b = map_funding(raw_ar), map_funding(raw_en)
        if a and b and a != b:
            flags.append("funding_ar_en_mismatch")
        title_all = f"{title_ar} {title_en or ''}".lower()
        says_full = "ممولة بالكامل" in title_all or "fully funded" in title_all
        if says_full and funding == "PARTIAL":
            flags.append("funding_title_mismatch")

    # --- study levels
    from_title = detect_levels(f"{title_ar} {title_en or ''}")
    degree_vals = criteria_values(ar, "الدرجة") + criteria_values(en, "Degree")
    from_criteria = detect_levels(" ".join(degree_vals))
    if from_title:
        levels = from_title
    else:
        levels = from_criteria
    if from_title and from_criteria and set(from_title) != set(from_criteria):
        flags.append("level_mismatch")
    if not levels:
        flags.append("levels_missing")

    # --- sections
    sec_ar = split_sections(ar.get("descriptions"))
    sec_en = split_sections(en.get("descriptions"))

    def text(sec, name):
        paras = html_to_paragraphs(sec.get(name))
        return "\n\n".join(paras) if paras else None

    ben_ar = extract_benefits(sec_ar.get("benefits"))
    ben_en = extract_benefits(sec_en.get("benefits"))
    if ben_en and len(ben_ar) != len(ben_en):
        flags.append("benefits_count_mismatch")
    if not ben_ar and not ben_en:
        flags.append("benefits_missing")

    # --- deadline
    no_deadline = bool(ar.get("no_deadline"))
    deadline = None if no_deadline else ar.get("deadline")

    org_ar = (ar.get("organization") or {}).get("name")
    org_en = (en.get("organization") or {}).get("name")

    countries = host_countries(ar, en)
    if not countries:
        flags.append("host_country_missing")

    nationality = criteria_values(ar, *NATIONALITY_LABELS) or criteria_values(en, *NATIONALITY_LABELS)
    eligible = palestinians_eligible(nationality)
    if eligible is False:
        flags.append("palestinians_not_eligible")
    elif eligible is None:
        flags.append("nationality_unknown")

    return {
        "source": "for9a",
        "source_id": str(summary.get("id") or ar.get("id")),
        "source_url": ar.get("url") or summary.get("url"),
        "title_ar": title_ar,
        "title_en": title_en,
        "cover_image_url": cover_url(ar),
        "provider_name_ar": _norm(org_ar) or None,
        "provider_name_en": _norm(org_en) or None,
        "host_countries": countries,
        "study_levels": levels,
        "fields_of_study_scope": None,       # needs LLM
        "fields_of_study": [],               # needs LLM
        "funding_type": funding,
        "financial_coverage_ar": None,       # needs LLM
        "financial_coverage_en": None,       # needs LLM
        "funding_duration": None,            # needs LLM
        "deadline": deadline,
        "updated_at": ts_to_iso(ar.get("updated_at")),
        "description_ar": text(sec_ar, "description"),
        "description_en": text(sec_en, "description"),
        "benefits": pair_benefits(ben_ar, ben_en),
        "required_documents": [],            # needs LLM
        "application_url": (app.get("link") or app.get("official_link")
                            or ar.get("url") or summary.get("url")),
        "apply_email": _norm(app.get("email")) or None,
        "apply_phone": _norm(app.get("phone")) or None,
        "audience": AUDIENCE,
        "palestinians_eligible": eligible,
        "review_flags": flags,
        "raw": {
            "nationality": nationality,
            "funding_type_ar": raw_ar,
            "funding_type_en": raw_en,
            "levels_from_title": from_title,
            "levels_from_criteria": from_criteria,
            "fields_ar": criteria_values(ar, "مجال الفرصة"),
            "fields_en": criteria_values(en, "Opportunity Field"),
            "eligibility_ar": text(sec_ar, "qualifications"),
            "eligibility_en": text(sec_en, "qualifications"),
            "how_to_apply": clean_how_to_apply(app.get("how_to_apply")),
            "age": criteria_values(ar, "العمر"),
            "gender": criteria_values(ar, "الجنس"),
        },
    }