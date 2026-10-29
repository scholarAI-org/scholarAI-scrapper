import pytest

from scholarships.for9a_parser import detect_levels, pair_benefits
from scholarships.llm_enrich import LLMClient, load_keys, verified
from scholarships.telegram_enrich import TgExtraction
from scholarships.telegram_parser import (clean_text, is_official_candidate,
                                          parse_channel_page, pick_official_link)


@pytest.mark.parametrize("text,expected", [
    ("Mastercard Foundation Scholars Program", []),
    ("Doctors Without Borders fellowship", []),
    ("Master's degree in Law", ["MASTER"]),
    ("Fully funded Masters scholarship", ["MASTER"]),
    ("Postdoctoral fellowship", ["POSTDOC"]),
    ("PhD and Master programmes", ["PHD", "MASTER"]),
    ("منحة ماجستير ودكتوراه", ["PHD", "MASTER"]),
    ("Undergraduate scholarship", ["BACHELOR"]),
    ("", []),
])
def test_detect_levels(text, expected):
    assert detect_levels(text) == expected


def test_pair_benefits_different_lengths_drops_english():
    out = pair_benefits(["a", "b"], ["x"])
    assert [b["text_en"] for b in out] == [None, None]
    assert [b["text_ar"] for b in out] == ["a", "b"]


def test_pair_benefits_same_length_pairs():
    out = pair_benefits(["a"], ["x"])
    assert out == [{"order": 1, "text_ar": "a", "text_en": "x"}]


def test_verified_quote():
    assert verified("two years, fully funded", "It is Two-Years, fully funded!")
    assert not verified("three years", "It lasts two years")
    assert not verified("", "anything")


def test_load_keys_dedupes_and_ignores_empty():
    env = {"GROQ_API_KEY": "a", "GROQ_API_KEY_2": "", "GROQ_API_KEYS": "a, b"}
    assert load_keys(env, "GROQ_API_KEY") == ["a", "b"]


def test_provider_order_follows_env():
    keys = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
    assert [p["name"] for p in LLMClient(env=keys).providers] == ["groq", "gemini"]
    env = {**keys, "LLM_PROVIDERS": "gemini,groq"}
    assert [p["name"] for p in LLMClient(env=env).providers] == ["gemini", "groq"]
    env = {**keys, "LLM_PROVIDERS": "gemini"}
    assert [p["name"] for p in LLMClient(env=env).providers] == ["gemini"]


def test_llm_disabled_without_keys():
    assert not LLMClient(env={}).enabled


@pytest.mark.parametrize("value,expected", [
    ("2026-12-31", "2026-12-31"), ("2026-13-45", None), ("31/12/2026", None), (None, None)])
def test_deadline_must_be_a_real_date(value, expected):
    assert TgExtraction(is_scholarship=True, deadline=value).deadline == expected


def test_null_lists_from_llm_are_accepted():
    ex = TgExtraction.model_validate({"is_scholarship": False, "benefits": None,
                                      "host_countries": None, "study_levels": None})
    assert ex.benefits == [] and ex.host_countries == []


@pytest.mark.parametrize("url,ok", [
    ("https://example.edu/apply", True),
    ("https://t.me/x", False),
    ("http://localhost:8080/", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://192.168.1.1/", False),
    ("http://10.0.0.5/", False),
    ("http://[::1]/", False),
    ("http://printer.local/", False),
])
def test_official_candidate(url, ok):
    assert is_official_candidate(url) is ok


def test_pick_official_link_skips_social():
    assert pick_official_link(["https://t.me/a", "https://wa.me/1", "https://uni.edu/x"]) == "https://uni.edu/x"


def test_clean_text_strips_invisible_and_emoji():
    assert clean_text("​Hello \U0001F600 world‏\n\n\n\nBye") == "Hello world\n\nBye"


CHANNEL_HTML = """
<div class="tgme_widget_message" data-post="eduscope2/7">
  <div class="tgme_widget_message_text">Scholarship <a href="https://uni.edu/apply">apply</a></div>
  <a class="tgme_widget_message_date"><time datetime="2026-10-01T10:00:00+00:00"></time></a>
</div>
<a class="tme_messages_more" data-before="7"></a>
"""


def test_parse_channel_page():
    posts, before = parse_channel_page(CHANNEL_HTML)
    assert before == 7
    assert posts[0]["id"] == 7 and posts[0]["links"] == ["https://uni.edu/apply"]
    assert posts[0]["url"] == "https://t.me/eduscope2/7"


def test_filter_drops_non_scholarships_and_expired():
    from scrapy.exceptions import DropItem
    from scrapy.statscollectors import MemoryStatsCollector
    from scholarships.filter_pipeline import DropUnwantedPipeline
    p = DropUnwantedPipeline(MemoryStatsCollector.__new__(MemoryStatsCollector))
    p.stats._stats = {}
    with pytest.raises(DropItem):
        p.process_item({"source_id": "1", "is_scholarship": False})
    with pytest.raises(DropItem):
        p.process_item({"source_id": "2", "is_scholarship": True, "deadline": "2020-01-01"})
    for ok in ({"source_id": "3", "is_scholarship": True, "deadline": "2999-01-01"},
               {"source_id": "4", "deadline": None},
               {"source_id": "5", "is_scholarship": None, "deadline": None}):
        assert p.process_item(ok) is ok


@pytest.mark.parametrize("values,expected", [
    (["لا يشترط جنسية محددة"], True),
    (["No specific nationality required"], True),
    (["فلسطيني", "سوري"], True),
    (["Palestinian"], True),
    (["سوري", "لبناني"], False),
    ([], None),
    (None, None),
])
def test_palestinians_eligible(values, expected):
    from scholarships.for9a_parser import palestinians_eligible
    assert palestinians_eligible(values) is expected


def _opp(nationality):
    crit = [{"label": "الجنسية", "value": nationality}] if nationality is not None else []
    return {"id": 1, "title": "منحة", "applicant_criteria": crit, "descriptions": [],
            "locations": [], "updated_at": 1760000000}


def test_for9a_record_has_audience_and_eligibility():
    from scholarships.for9a_parser import build_record
    rec = build_record({"id": 1}, _opp(["لا يشترط جنسية محددة"]), None, None)
    assert rec["audience"] == "PALESTINIAN" and rec["palestinians_eligible"] is True
    assert "palestinians_not_eligible" not in rec["review_flags"]
    rec = build_record({"id": 1}, _opp(["سوري"]), None, None)
    assert rec["palestinians_eligible"] is False and "palestinians_not_eligible" in rec["review_flags"]
    rec = build_record({"id": 1}, _opp(None), None, None)
    assert rec["palestinians_eligible"] is None and "nationality_unknown" in rec["review_flags"]


def test_expired_check_keeps_deadline_of_today():
    from datetime import date, timedelta
    from scholarships.spiders.for9a_spider import For9aSpider
    sp = For9aSpider()
    mk = lambda d: {"id": 1, "title": "t", "deadline": d, "no_deadline": False}  # noqa: E731
    assert sp._is_active({}, mk(date.today().isoformat())) is True
    assert sp._is_active({}, mk((date.today() - timedelta(days=1)).isoformat())) is False
    assert sp._is_active({}, mk("31/12/2026")) is True  # unreadable: keep, never crash


def test_llm_cache_is_trimmed(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scholarships.llm_pipeline import LLMEnrichmentPipeline
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output").mkdir()
    (tmp_path / "output/llm_cache.json").write_text(json.dumps({str(i): {} for i in range(30)}))
    p = LLMEnrichmentPipeline()
    p.MAX_CACHE = 10
    p.crawler = SimpleNamespace(spider=SimpleNamespace(logger=SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None)))
    p.open_spider()
    assert list(p.cache) == [str(i) for i in range(20, 30)]


def test_verified_tolerates_literal_backslash_n():
    src = "المستويات الدراسية:\n• الماجستير (Master’s)\n• الدكتوراه (PhD)"
    assert verified("المستويات الدراسية:\\n• الماجستير (Master’s)\\n• الدكتوراه (PhD)", src)


def test_application_url_ignores_unloaded_page():
    from scholarships.telegram_parser import apply_page
    draft = {"raw": {}, "application_url": "https://x.org/official"}
    apply_page(draft, {"url": "https://shortener.dev/interstitial", "status": "shortener"})
    assert draft["application_url"] == "https://x.org/official"
    apply_page(draft, {"url": "https://x.org/final", "status": "ok"})
    assert draft["application_url"] == "https://x.org/final"