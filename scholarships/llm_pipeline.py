import asyncio
import hashlib
import json
import os
import threading

from dotenv import load_dotenv
from scholarships.llm_enrich import SYSTEM_PROMPT, Extraction, LLMClient, LLMError, apply_extraction

load_dotenv()


class LLMEnrichmentPipeline:
    """Fills the LLM-derived fields. Never drops an item: on failure the item
    goes through with an 'llm_failed' / 'llm_skipped' review flag.

    Results are cached in output/llm_cache.json so re-runs do not spend API
    quota on unchanged scholarships. Other sources (see telegram_pipeline.py)
    subclass this and override the hooks below.
    """

    cache_path = "output/llm_cache.json"
    MAX_CACHE = 2000
    extraction_cls = Extraction
    prompt = SYSTEM_PROMPT

    # ---- hooks -------------------------------------------------------
    def make_client(self):
        return LLMClient()

    def apply(self, item, ex, meta):
        return apply_extraction(item, ex, meta)

    def cache_key(self, rec):
        digest = hashlib.sha1(json.dumps(
            [rec.get("description_en"), rec["raw"].get("eligibility_en"),
             rec["raw"].get("how_to_apply")], ensure_ascii=False).encode()).hexdigest()[:12]
        prompt = hashlib.sha1(self.prompt.encode()).hexdigest()[:6]  # new prompt -> new cache
        return f"{rec['source_id']}:{rec.get('updated_at')}:{digest}:{prompt}"

    # ---- scrapy plumbing --------------------------------------------
    @classmethod
    def from_crawler(cls, crawler):
        obj = cls()
        obj.crawler = crawler
        return obj

    def open_spider(self):
        spider = self.crawler.spider
        self.logger = spider.logger
        self.lock = threading.Lock()  # one LLM call at a time (rate limits)
        self.client = self.make_client()
        self.cache = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
        if len(self.cache) > self.MAX_CACHE:  # keep the newest entries (dicts keep insertion order)
            self.cache = dict(list(self.cache.items())[-self.MAX_CACHE:])
        if not self.client.enabled:
            self.logger.warning("No GEMINI_API_KEY / GROQ_API_KEY set: LLM step skipped")
        else:
            names = [f"{p['name']}x{len(p['keys'])}" for p in self.client.providers]
            self.logger.info(f"LLM providers: {', '.join(names)}")

    async def process_item(self, item):
        if not getattr(self.crawler.spider, "use_llm", True):
            return item
        # blocking HTTP + sleeps run in a thread so Scrapy keeps crawling
        return await asyncio.to_thread(self._enrich, item)

    def _enrich(self, item):
        if not self.client.enabled:
            item["review_flags"].append("llm_skipped")
            return item
        key = self.cache_key(item)
        with self.lock:
            return self._enrich_locked(item, key)

    def _enrich_locked(self, item, key):
        try:
            cached = self.cache.get(key)
            if cached:
                ex, meta = self.extraction_cls.model_validate(cached["extraction"]), cached["meta"]
            else:
                ex, meta = self.client.extract(item)
                self.cache[key] = {"extraction": ex.model_dump(), "meta": meta}
                self._save()
            flags = self.apply(item, ex, meta)
            self.logger.info(f"LLM ok {item['source_id']} via {meta['provider']}#{meta['key']} "
                             f"in {meta.get('seconds', 0)}s flags={flags}")
            for err in meta.get("failed_attempts", []):
                self.logger.warning(f"LLM retry {item['source_id']}: {err}")
        except LLMError as e:
            self.logger.warning(f"LLM failed {item['source_id']}: {e}")
            item["review_flags"].append("llm_failed")
        except Exception as e:
            self.logger.error(f"LLM step error {item['source_id']}: {e!r}")
            item["review_flags"].append("llm_failed")
        return item

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cache_path)  # a crash never leaves a half-written cache