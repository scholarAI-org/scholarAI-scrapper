import json
from datetime import datetime

from scholarships.utils_ministry import is_deadline_still_valid


class MinistryPipeline:
    """
    Collects all scraped Ministry items, filters out expired scholarships,
    and saves the result to a JSON file. Extension notices are kept as
    independent records (not automatically linked to their original
    scholarship) — the admin reviews and matches them manually.
    """

    def open_spider(self, spider):
        self.items = []

    def process_item(self, item, spider):
        if item.get("source") != "ministry":
            return item

        self.items.append(dict(item))
        return item

    def close_spider(self, spider):
        final_valid = []

        for record in self.items:
            deadline_str = record.get("deadline")
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d") if deadline_str else None

            if not is_deadline_still_valid(deadline_date):
                continue

            final_valid.append(record)

        with open("ministry_output.json", "w", encoding="utf-8") as f:
            json.dump(final_valid, f, ensure_ascii=False, indent=2)

        spider.logger.info(f" Saved {len(final_valid)} active records to ministry_output.json")