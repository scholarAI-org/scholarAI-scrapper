import scrapy
from scrapy.http import TextResponse

from scholarships import db
from scholarships.telegram_parser import (build_draft, classify_page, page_title,
                                          page_to_text, parse_channel_page, pick_official_link)


class TelegramSpider(scrapy.Spider):
    """
    Preview spider for a PUBLIC Telegram channel (https://t.me/s/<channel>).

    Takes the latest `limit` posts, follows each post's official link (shortened
    links are resolved by redirects), and lets the LLM decide whether the post is
    a scholarship and extract the data. With DATABASE_URL set, only posts newer than
    the newest stored one are read (since_id), and the result is upserted.

    Usage:
        scrapy crawl telegram -a limit=10
        scrapy crawl telegram -a channel=eduscope2 -a since_id=190
        scrapy crawl telegram -a llm=0        # without the LLM step
        scrapy crawl telegram -a browser=1    # re-read blocked/JS pages with a real browser
    Non-scholarship posts and expired scholarships are dropped (filter_pipeline).
    """

    name = "telegram"

    custom_settings = {
        "ITEM_PIPELINES": {
            "scholarships.db_pipeline.SkipUnchangedPipeline": 100,
            "scholarships.telegram_pipeline.BrowserFallbackPipeline": 250,
            "scholarships.telegram_pipeline.TelegramLLMPipeline": 300,
            "scholarships.filter_pipeline.DropUnwantedPipeline": 400,
            "scholarships.db_pipeline.PostgresPipeline": 500,
        },
        "FEEDS": {
            "output/telegram.json": {
                "format": "json", "encoding": "utf8", "indent": 2, "overwrite": True,
            }
        },
        "DOWNLOAD_TIMEOUT": 25,
        "RETRY_TIMES": 1,
        # identify ourselves honestly instead of the default "Scrapy/x" (often blocked)
        "USER_AGENT": "Mozilla/5.0 (compatible; ScholarAI-Scraper/1.0)",
    }

    def __init__(self, limit="10", channel="eduscope2", since_id=None, llm="1", browser="0", *a, **kw):
        super().__init__(*a, **kw)
        self.limit = int(limit)
        self.channel = channel
        self.since_id = int(since_id) if since_id else 0
        self.use_llm = str(llm) != "0"
        self.use_browser = str(browser) == "1"
        self.posts = {}

    async def start(self):
        if not self.since_id and not db.dry_run():
            try:
                conn = db.connect()
                try:
                    self.since_id = db.max_telegram_id(conn, self.channel)
                finally:
                    conn.close()
                self.logger.info(f"since_id from database: {self.since_id}")
            except Exception as e:  # no history: fall back to the latest `limit` posts
                self.logger.warning(f"could not read since_id from the database: {e!r}")
        yield scrapy.Request(f"https://t.me/s/{self.channel}", callback=self.parse_channel)

    # ---------------------------------------------------------- channel
    def parse_channel(self, response):
        posts, before = parse_channel_page(response.text)
        for p in posts:
            if p["id"] > self.since_id:
                self.posts[p["id"]] = p
        self.logger.info(f"{response.url}: {len(posts)} posts (kept {len(self.posts)})")
        enough = len(self.posts) >= self.limit
        # stop paging when old enough or at the since_id boundary
        reached_old = bool(posts) and min(p["id"] for p in posts) <= self.since_id
        if before and not enough and not reached_old:
            yield scrapy.Request(f"https://t.me/s/{self.channel}?before={before}",
                                 callback=self.parse_channel)
            return
        yield from self._dispatch()

    def _dispatch(self):
        newest = sorted(self.posts.values(), key=lambda p: p["id"], reverse=True)[: self.limit]
        self.logger.info(f"Processing posts {[p['id'] for p in newest]}")
        for post in sorted(newest, key=lambda p: p["id"]):
            if not post["text"]:
                self.logger.info(f"Skipped {post['id']}: no text (media only)")
                continue
            link = pick_official_link(post["links"])
            if not link:
                yield build_draft(post, {"status": "none"})
                continue
            yield scrapy.Request(
                link, callback=self.parse_page, errback=self.page_failed,
                cb_kwargs={"post": post}, dont_filter=True,
            )

    # ----------------------------------------------------- official page
    def parse_page(self, response, post):
        if not isinstance(response, TextResponse):
            ctype = response.headers.get("Content-Type", b"").decode("latin-1")
            status = "pdf" if "pdf" in ctype.lower() else "not_html"
            yield build_draft(post, {"status": status, "url": response.url})
            return
        text = page_to_text(response.text)
        status = classify_page(response.url, response.text, text)
        yield build_draft(post, {
            "status": status, "url": response.url,
            "title": page_title(response.text),
            "text": text if status == "ok" else None,
        })

    def page_failed(self, failure):
        post = failure.request.cb_kwargs["post"]
        self.logger.warning(f"Official page failed for {post['id']}: {failure.value!r}")
        yield build_draft(post, {"status": "unreachable", "url": failure.request.url,
                                 "error": self._error_text(failure)})

    @staticmethod
    def _error_text(failure):
        resp = getattr(failure.value, "response", None)
        if resp is not None:  # HttpError: say which status the site answered with
            return f"HTTP {resp.status}"
        return repr(failure.value)[:200]
