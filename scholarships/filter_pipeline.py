from datetime import date

from scrapy.exceptions import DropItem


class DropUnwantedPipeline:
    """Runs after the LLM step. Drops what must never reach the Admin:
    - Telegram posts the LLM decided are not scholarship announcements
    - scholarships whose deadline has already passed
    Records without a known deadline are kept (flagged deadline_missing)."""

    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.stats)

    def process_item(self, item):
        if item.get("is_scholarship") is False:
            self.stats.inc_value("filter/not_scholarship")
            raise DropItem(f"not a scholarship: {item.get('source_id')}")
        deadline = item.get("deadline")
        if deadline and deadline < date.today().isoformat():
            self.stats.inc_value("filter/expired")
            raise DropItem(f"expired {deadline}: {item.get('source_id')}")
        return item
