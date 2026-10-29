"""Parsing helpers for a PUBLIC Telegram channel preview (https://t.me/s/<channel>)
and for the official pages the posts link to. No scrapy dependency."""
import ipaddress
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# zero-width / bidi control characters (some channels pad posts with hundreds)
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u061c\ufeff\u00ad]")
# decorative emoji / pictographs / dingbats / variation selectors
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️⃣]")
URL = re.compile(r"https?://[^\s<>\"')\]]+")

# links that are never the scholarship's official page
SKIP_HOSTS = ("t.me", "telegram.me", "telegram.org", "wa.me", "whatsapp.com",
              "chat.whatsapp.com", "instagram.com", "facebook.com", "fb.com",
              "twitter.com", "x.com", "tiktok.com", "youtube.com", "youtu.be",
              "linkedin.com", "snapchat.com")


def clean_text(text):
    """Remove invisible chars and emoji, keep paragraph structure."""
    t = INVISIBLE.sub("", text or "")
    t = EMOJI.sub("", t)
    t = t.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def is_official_candidate(url):
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:  # IP literals: never local / private / link-local (cloud metadata) addresses
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global:
            return False
    except ValueError:
        pass
    return not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def pick_official_link(links):
    for u in links:
        if is_official_candidate(u):
            return u
    return None


def parse_channel_page(html):
    """-> (posts, before_cursor). posts are dicts:
    id (int), url, date (ISO with offset or None), text, links, edited, has_media."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            channel, pid = msg["data-post"].rsplit("/", 1)
            pid = int(pid)
        except Exception:
            continue
        body = msg.select_one("div.tgme_widget_message_text")
        links = []
        text = ""
        if body is not None:
            for br in body.find_all("br"):
                br.replace_with("\n")
            for a in body.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("http") and href not in links:
                    links.append(href)
            text = clean_text(body.get_text())
            for u in URL.findall(text):  # plain-text URLs not wrapped in <a>
                u = u.rstrip(".,;:!؟،")
                if u not in links:
                    links.append(u)
        t = msg.select_one("a.tgme_widget_message_date time")
        meta = msg.select_one(".tgme_widget_message_meta")
        meta_txt = meta.get_text(" ") if meta else ""
        posts.append({
            "id": pid,
            "channel": channel,
            "url": f"https://t.me/{channel}/{pid}",
            "date": t.get("datetime") if t is not None else None,
            "text": text,
            "links": links,
            "edited": "edited" in meta_txt.lower() or "تم التعديل" in meta_txt,
            "has_media": bool(msg.select_one(
                ".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player, "
                ".tgme_widget_message_document_wrap")),
            "forwarded": bool(msg.select_one(".tgme_widget_message_forwarded_from")),
        })
    more = soup.select_one("a.tme_messages_more[data-before]")
    before = int(more["data-before"]) if more is not None else None
    return posts, before


def _is_link_only_list(lst):
    """A <ul>/<ol> that is just a menu: every item is nothing but a link."""
    items = lst.find_all("li", recursive=False)
    if len(items) < 3:
        return False
    for li in items:
        a = li.find("a")
        if a is None or re.sub(r"\s+", "", li.get_text()) != re.sub(r"\s+", "", a.get_text()):
            return False
    return True


def page_to_text(html, max_chars=12000):
    """Readable text of an official page: menus, cookie banners and chrome removed."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer",
                     "header", "form", "iframe", "aside", "button"]):
        tag.decompose()
    # cookie / consent / breadcrumb blocks. Never remove a page-level container:
    # some sites put e.g. class="cookie-open" on <body> or on the main wrapper.
    def _small_chrome(tag):
        return tag.name not in ("html", "body", "main", "article") and len(tag.get_text()) < 400

    for attr, pat in (("class", r"cookie|consent|breadcrumb|skip-?link"),
                      ("id", r"cookie|consent|breadcrumb")):
        for tag in soup.find_all(attrs={attr: re.compile(pat, re.I)}):
            if tag.parent is not None and _small_chrome(tag):
                tag.decompose()
    for lst in soup.find_all(["ul", "ol"]):
        if lst.parent is not None and _is_link_only_list(lst):
            lst.decompose()
    root = (soup.find("main") or soup.find(attrs={"role": "main"})
            or soup.find("article") or soup.body or soup)
    lines = []
    for ln in root.get_text("\n").split("\n"):
        ln = re.sub(r"\s+", " ", INVISIBLE.sub("", ln)).strip()
        if len(ln) >= 3 and (not lines or lines[-1] != ln):
            lines.append(ln)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0]
    return text


def page_title(html):
    soup = BeautifulSoup(html or "", "html.parser")
    return re.sub(r"\s+", " ", soup.title.get_text()).strip() if soup.title else None


def _utc(iso):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def build_draft(post, page):
    """Contract-shaped record, still empty where the LLM must fill it.
    page: {"status": ok|none|unreachable|pdf|empty, "url", "title", "text"}"""
    page = page or {"status": "none"}
    first_line = (post["text"].split("\n", 1)[0] if post["text"] else "") or None
    official = pick_official_link(post["links"])
    return {
        "source": "telegram",
        "source_id": f"{post['channel']}/{post['id']}",
        "source_url": post["url"],
        "audience": "GAZA",
        "is_scholarship": None,
        "title_ar": first_line,
        "title_en": None,
        "cover_image_url": None,
        "provider_name_ar": None,
        "provider_name_en": None,
        "host_countries": [],
        "study_levels": [],
        "fields_of_study_scope": None,
        "fields_of_study": [],
        "funding_type": None,
        "financial_coverage_ar": None,
        "financial_coverage_en": None,
        "funding_duration": None,
        "deadline": None,
        "updated_at": _utc(post["date"]) if post.get("date") else None,
        "description_ar": None,
        "description_en": None,
        "benefits": [],
        "required_documents": [],
        "application_url": (page.get("url") if page.get("status") == "ok" else None) or official or post["url"],
        "apply_email": None,
        "apply_phone": None,
        "palestinians_eligible": None,
        "review_flags": [],
        "raw": {
            "post_text": post["text"],
            "post_links": post["links"],
            "edited": post["edited"],
            "official_link": official,
            "page_url": page.get("url"),
            "page_status": page.get("status"),
            "page_title": page.get("title"),
            "page_error": page.get("error"),
            "page_text": page.get("text"),
        },
    }


INTERSTITIAL_HOSTS = ("encurtador.dev",)  # shortener ad pages that need JavaScript


def classify_page(url, html, text):
    """-> status for an official page that was fetched: ok | empty | shortener | closed"""
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in INTERSTITIAL_HOSTS):
        return "shortener"
    if "docs.google.com/forms" in url and url.rstrip("/").endswith("closedform"):
        return "closed"
    low = (html or "").lower()
    if "docs.google.com/forms" in url and re.search(
            r"يجب تسجيل الدخول|sign in to continue|you need to sign in|accounts\.google\.com", low):
        return "login"  # the form is behind a Google sign-in wall
    if len(text) < 300 or "habilite o javascript" in low:
        return "empty"
    return "ok"


def apply_page(draft, page):
    """Replace the page info of a draft (used after a browser re-fetch)."""
    raw = draft["raw"]
    raw["page_status"] = page.get("status")
    raw["page_url"] = page.get("url")
    raw["page_title"] = page.get("title")
    raw["page_text"] = page.get("text")
    raw["page_error"] = page.get("error")
    raw["page_fetch"] = page.get("via", "http")
    if page.get("url") and page.get("status") == "ok":
        draft["application_url"] = page["url"]  # only a page that really loaded
    return draft