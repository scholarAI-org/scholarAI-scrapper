import json
import os

from scrapy.exceptions import DropItem

from scholarships import db


class SkipUnchangedPipeline:
    """Runs FIRST. Drops records already stored with the same source version, so
    they cost no LLM calls. Does nothing without a database."""

    @classmethod
    def from_crawler(cls, crawler):
        obj = cls()
        obj.crawler = crawler
        return obj

    def open_spider(self):
        self.known = {}
        if db.dry_run():
            return
        conn = db.connect()
        try:
            self.known = db.existing_versions(conn, self.crawler.spider.name)
        finally:
            conn.close()
        self.crawler.spider.logger.info(f"{len(self.known)} records already stored")

    def process_item(self, item):
        stored = self.known.get(str(item.get("source_id")))
        version = db._parse_ts(item.get("updated_at"))
        if stored is not None and version is not None and stored == version:
            self.crawler.stats.inc_value("db/unchanged")
            raise DropItem(f"unchanged: {item.get('source_id')}")
        return item


class PostgresPipeline:
    """Runs LAST. Upserts on (source, source_id); never touches status or rows an
    Admin already reviewed. DB_DRY_RUN=1 (or no DATABASE_URL) only logs the rows.
    Writes output/db_report_<spider>.json, which tools/check_output.py turns into a CI failure."""

    conn = None
    count = None  # stays None if open_spider failed (e.g. database unreachable)

    @classmethod
    def from_crawler(cls, crawler):
        obj = cls()
        obj.crawler = crawler
        try:  # never leave a report from an earlier run that could be mistaken for this one
            os.remove(f"output/db_report_{crawler.spidercls.name}.json")
        except (OSError, AttributeError):
            pass
        return obj

    def open_spider(self):
        self.log = self.crawler.spider.logger
        self.dry = db.dry_run()
        self.conn = None if self.dry else db.connect()
        self.count = {"inserted": 0, "updated": 0, "skipped_reviewed": 0,
                      "invalid": 0, "errors": 0, "dry_run": self.dry}
        self.problems = []
        self.log.info("DB dry-run: nothing will be written" if self.dry else "PostgreSQL connected")

    def process_item(self, item):
        row = db.to_row(item)
        problems = db.validate(row)
        if problems:
            self.count["invalid"] += 1
            self.problems.append(f"{row['source']}/{row['source_id']}: {'; '.join(problems)}")
            self.log.error(f"Invalid record {row['source_id']}: {problems}")
            return item
        if self.dry:
            self.count["inserted"] += 1
            return item
        try:
            self._write(row)
        except Exception as e:  # lost connection, constraint, ...: keep going with the rest
            self.count["errors"] += 1
            self.problems.append(f"{row['source']}/{row['source_id']}: {e!r}"[:300])
            self.log.error(f"DB write failed {row['source_id']}: {e!r}")
            self._reconnect()
        return item

    def _write(self, row):
        with self.conn.cursor() as cur:
            cur.execute(db.UPSERT_SQL, db.adapt(row))
            res = cur.fetchone()
        self.conn.commit()
        if res is None:
            self.count["skipped_reviewed"] += 1
        elif res[0]:
            self.count["inserted"] += 1
        else:
            self.count["updated"] += 1

    def _reconnect(self):
        try:
            self.conn.rollback()
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = db.connect()

    def close_spider(self):
        if self.conn:
            self.conn.close()
        if self.count is None:  # never opened: the real error is already in the log
            return
        report = {**self.count, "unchanged": self.crawler.stats.get_value("db/unchanged", 0),
                  "problems": self.problems[:20]}
        os.makedirs("output", exist_ok=True)
        with open(f"output/db_report_{self.crawler.spider.name}.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        self.log.info(f"DB report: {report}")