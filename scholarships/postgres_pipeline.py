import os
import psycopg2
from dotenv import load_dotenv
from scholarships.utils_html import sanitize_html

load_dotenv()


class PostgreSQLPipeline:

    def open_spider(self, spider):
        self.connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        self.cursor = self.connection.cursor()
        self.success_count = 0
        self.error_count = 0
        spider.logger.info("PostgreSQL connected")

    def process_item(self, item, spider):
        item["description_html"] = sanitize_html(item.get("description_html"))
        item["benefits_html"] = sanitize_html(item.get("benefits_html"))
        item["qualifications_html"] = sanitize_html(item.get("qualifications_html"))

        sql = """
            INSERT INTO scholarships (
                source, source_id, title, slug, source_url,
                organization_name, country, deadline, no_deadline, image_url,
                description_html, benefits_html, qualifications_html,
                apply_link, apply_email, apply_phone,
                pdf_url, attachments, is_extension, status, scraped_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source, source_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                slug = EXCLUDED.slug,
                source_url = EXCLUDED.source_url,
                organization_name = EXCLUDED.organization_name,
                country = EXCLUDED.country,
                deadline = EXCLUDED.deadline,
                no_deadline = EXCLUDED.no_deadline,
                image_url = EXCLUDED.image_url,
                description_html = EXCLUDED.description_html,
                benefits_html = EXCLUDED.benefits_html,
                qualifications_html = EXCLUDED.qualifications_html,
                apply_link = EXCLUDED.apply_link,
                apply_email = EXCLUDED.apply_email,
                apply_phone = EXCLUDED.apply_phone,
                pdf_url = EXCLUDED.pdf_url,
                attachments = EXCLUDED.attachments,
                is_extension = EXCLUDED.is_extension,
                scraped_at = EXCLUDED.scraped_at;
        """

        values = (
            item.get("source"),
            item.get("source_id"),
            item.get("title"),
            item.get("slug"),
            item.get("source_url"),
            item.get("organization_name"),
            item.get("country"),
            item.get("deadline"),
            item.get("no_deadline"),
            item.get("image_url"),
            item.get("description_html"),
            item.get("benefits_html"),
            item.get("qualifications_html"),
            item.get("apply_link"),
            item.get("apply_email"),
            item.get("apply_phone"),
            item.get("pdf_url"),
            item.get("attachments"),
            item.get("is_extension"),
            item.get("status"),
            item.get("scraped_at"),
        )

        try:
            self.cursor.execute(sql, values)
            self.connection.commit()
            self.success_count += 1
            spider.logger.info(f"Saved: {item.get('source')} - {item.get('source_id')}")
        except Exception as e:
            self.connection.rollback()
            self.error_count += 1
            spider.logger.error(f"Database error for {item.get('source_id')}: {e}")

        return item

    def close_spider(self, spider):
        self.cursor.close()
        self.connection.close()

        spider.logger.info(
            f"PostgreSQL connection closed. "
            f"Success: {self.success_count}, Errors: {self.error_count}"
        )

        if self.error_count > 0:
            raise RuntimeError(
                f"{self.error_count} database writes failed. "
                f"Successful writes: {self.success_count}. "
                f"Check the database logs above."
            )