import asyncio
import hashlib
import json

from scholarships.llm_pipeline import LLMEnrichmentPipeline
from scholarships.telegram_enrich import (TG_SYSTEM_PROMPT, TgExtraction, TelegramLLMClient,
                                          apply_tg_extraction)
from scholarships.telegram_parser import apply_page


class BrowserFallbackPipeline:
    """Re-reads the official page with a real browser when plain HTTP could not
    (blocked / JavaScript-rendered / link shortener). Enabled with -a browser=1."""

    RETRY = {"unreachable", "empty", "shortener"}

    @classmethod
    def from_crawler(cls, crawler):
        obj = cls()
        obj.crawler = crawler
        return obj

    def open_spider(self):
        self.spider = self.crawler.spider
        self.fetcher = None

    def close_spider(self):
        if self.fetcher:
            self.fetcher.close()

    async def process_item(self, item):
        if not getattr(self.spider, "use_browser", False):
            return item
        raw = item["raw"]
        url = raw.get("official_link")
        if raw.get("page_status") not in self.RETRY or not url:
            return item
        if "IgnoreRequest" in str(raw.get("page_error")):
            return item  # Scrapy skipped it because of robots.txt: respect that
        if self.fetcher is None:
            from scholarships.browser_fetch import BrowserFetcher
            self.fetcher = BrowserFetcher()
        page = await asyncio.to_thread(self.fetcher.fetch, url)
        self.spider.logger.info(
            f"Browser {item['source_id']}: {raw.get('page_status')} -> {page['status']} "
            f"{page.get('error') or ''}")
        apply_page(item, page)
        return item


class TelegramLLMPipeline(LLMEnrichmentPipeline):
    cache_path = "output/llm_cache_telegram.json"
    extraction_cls = TgExtraction
    prompt = TG_SYSTEM_PROMPT

    def make_client(self):
        return TelegramLLMClient()

    def apply(self, item, ex, meta):
        return apply_tg_extraction(item, ex, meta)

    def cache_key(self, rec):
        raw = rec["raw"]
        digest = hashlib.sha1(json.dumps([raw.get("post_text"), raw.get("page_text")],
                                         ensure_ascii=False).encode()).hexdigest()[:12]
        prompt = hashlib.sha1(self.prompt.encode()).hexdigest()[:6]
        return f"{rec['source_id']}:{digest}:{prompt}"