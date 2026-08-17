import os
import re
import json
from datetime import datetime, timezone

import scrapy
from dotenv import load_dotenv
from google import genai
from google.genai import types

from scholarships.items import ScholarshipItem
from scholarships.utils_ministry import (
    fix_arabic_line_order,
    extract_deadline_from_text,
    is_extension_post,
)

load_dotenv()


class MinistrySpider(scrapy.Spider):
    """
    Collects scholarships from the Palestinian Ministry of Education website.
    Each blog post is classified as an extension notice, a non-scholarship
    post (results/news), or a scholarship post containing one or more
    instruction PDFs. PDFs are parsed with Google Gemini to extract
    structured data.
    """

    name = "ministry"

    base_url = "https://www.mohe.pna.ps/scholarships/Blog/5"
    max_pages = 1
    max_gemini_requests_per_run = 18

    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gemini_requests_made = 0

    gemini_prompt_multi = """
    Read the attached PDF files (each file contains the instructions for a
    separate scholarship program, taken from the same Ministry announcement).
    For each file individually, extract the following information as JSON,
    and return an array with one object per file, in the same order:

    [
    {
        "is_valid_scholarship": true or false,
        "title": "...",
        "country": "...",
        "organization": "...",
        "deadline": "YYYY-MM-DD or null",
        "description": "...",
        "benefits": "...",
        "requirements": "..."
    },
    ...
    ]

    Set "is_valid_scholarship" to false only if the file is just an empty
    application form, or a supplementary list (e.g. eligible universities or
    specializations) rather than a complete scholarship announcement. In that
    case, set the remaining fields to null.

    Return only the JSON array, with no additional text before or after it.
    """

    async def start(self):
        yield scrapy.Request(
            url=f"{self.base_url}?Page=1",
            callback=self.parse,
            cb_kwargs={"page": 1},
        )

    def parse(self, response, page):
        """Collects post links from a listing page and follows each one."""
        links = response.xpath('//a[contains(text(), "قراءة المزيد")]/@href').getall()

        if not links:
            self.logger.info(f"Page {page}: no links found - stopping")
            return

        self.logger.info(f"Page {page}: found {len(links)} links")

        for link in links:
            yield response.follow(link, callback=self.parse_post)

        if page >= self.max_pages:
            self.logger.info(f"🛑 Reached max pages ({self.max_pages}) - stopping")
            return

        yield scrapy.Request(
            url=f"{self.base_url}?Page={page + 1}",
            callback=self.parse,
            cb_kwargs={"page": page + 1},
        )

    def parse_post(self, response):
        """
        Handles a single post: classifies it as an extension notice, a
        non-scholarship post, or a scholarship post with one or more
        instruction PDFs, then requests the PDF(s) accordingly.
        """
        title_h1 = response.xpath("//h1/text()").get()
        title_h1 = title_h1.strip() if title_h1 else "بدون عنوان"

        if is_extension_post(title_h1):
            yield from self._parse_extension_post(response, title_h1)
            return

        groups = self._extract_program_groups(response)

        if not groups:
            self.logger.info(f"⏭️ Not a scholarship (results/news): {title_h1}")
            return

        # Download every "instructions" PDF for this post, then send them all
        # to Gemini together once they've all arrived (see collect_pdf).
        meta = {"groups": groups, "title": title_h1, "post_url": response.url, "pdf_bytes": {}}

        for group in groups:
            yield scrapy.Request(
                url=group["instructions_pdf"],
                callback=self.collect_pdf,
                cb_kwargs={"meta": meta, "pdf_url": group["instructions_pdf"]},
                dont_filter=True,
            )

    def _parse_extension_post(self, response, title_h1):
        """Extracts the new deadline from a deadline-extension notice."""
        body_text_parts = response.xpath("//body//text()").getall()
        full_text = fix_arabic_line_order(" ".join(body_text_parts))
        new_deadline = extract_deadline_from_text(full_text)

        self.logger.info(f"🔄 Extension: {title_h1} → new deadline: {new_deadline}")

        item = ScholarshipItem()
        item["source"] = "ministry"
        item["is_extension"] = True
        item["title"] = title_h1
        item["deadline"] = new_deadline.strftime("%Y-%m-%d") if new_deadline else None

        # --- Fields not applicable to an extension record, kept for schema consistency ---
        item["source_id"] = None
        item["slug"] = None
        item["source_url"] = response.url
        item["organization_name"] = None
        item["country"] = None
        item["no_deadline"] = False
        item["image_url"] = None
        item["description_html"] = None
        item["benefits_html"] = None
        item["qualifications_html"] = None
        item["apply_link"] = None
        item["apply_email"] = None
        item["apply_phone"] = None
        item["pdf_url"] = None
        item["attachments"] = []
        item["status"] = "pending"
        item["scraped_at"] = datetime.now(timezone.utc).isoformat()
        item["reviewed_at"] = None
        item["reviewed_by"] = None

        yield item

    def _extract_program_groups(self, response):
        """
        Walks through the post's paragraphs and groups links by program:
        each "تعليمات" (instructions) link starts a new group, and any
        following "تقديم"/"التسجيل" (apply) link or extra PDF link
        (attachment) belongs to that group, until the next instructions
        link starts a new one.
        """
        groups = []
        current_group = None

        for paragraph in response.css("p"):
            link = paragraph.css("a::attr(href)").get()
            if not link:
                continue

            paragraph_text = " ".join(paragraph.css("::text").getall())
            full_link = response.urljoin(link)

            if "تعليمات" in paragraph_text and full_link.lower().endswith(".pdf"):
                current_group = {
                    "instructions_pdf": full_link,
                    "attachments": [],
                    "application_link": None,
                }
                groups.append(current_group)
                continue

            if current_group is None:
                continue

            if "تقديم" in paragraph_text or "التسجيل" in paragraph_text:
                current_group["application_link"] = full_link
            elif full_link.lower().endswith(".pdf"):
                current_group["attachments"].append(full_link)

        return groups

    def collect_pdf(self, response, meta, pdf_url):
        """
        Collects each program's instructions PDF in memory. Once all PDFs
        for the post have arrived, sends them to Gemini in a single request.
        """
        meta["pdf_bytes"][pdf_url] = response.body

        if len(meta["pdf_bytes"]) < len(meta["groups"]):
            return  # still waiting for other PDFs in this post

        results = self._extract_data_with_gemini(meta)
        if results is None:
            return

        yield from self._build_items(meta, results)

    def _extract_data_with_gemini(self, meta):
        """Uploads all collected PDFs and asks Gemini to extract structured data for each."""
        if self.gemini_requests_made >= self.max_gemini_requests_per_run:
            self.logger.warning(
                f"🛑 Reached the per-run Gemini limit "
                f"({self.max_gemini_requests_per_run} requests) - stopping to avoid "
                f"exhausting today's quota. Remaining posts will be picked up on the next run."
            )
            raise scrapy.exceptions.CloseSpider("gemini_quota_reached")

        temp_files = []
        for i, group in enumerate(meta["groups"]):
            temp_filename = f"/tmp/{meta['post_url'].rstrip('/').split('/')[-2]}-{i + 1}.pdf"
            with open(temp_filename, "wb") as f:
                f.write(meta["pdf_bytes"][group["instructions_pdf"]])
            temp_files.append(temp_filename)

        try:
            uploaded_files = [self.gemini_client.files.upload(file=tf) for tf in temp_files]
            gemini_response = self.gemini_client.models.generate_content(
                model="gemini-3.5-flash",
                contents=uploaded_files + [self.gemini_prompt_multi],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            self.gemini_requests_made += 1
            return json.loads(gemini_response.text)
        except Exception as e:
            self.logger.warning(f"⚠️ Gemini processing failed for {meta['post_url']}: {e}")
            return None

    def _build_items(self, meta, results):
        """Builds one ScholarshipItem per valid program returned by Gemini."""
        has_multiple = len(meta["groups"]) > 1

        for i, (group, data) in enumerate(zip(meta["groups"], results), start=1):
            if not data.get("is_valid_scholarship"):
                self.logger.info(f"⏭️ Gemini excluded: {meta['title']} ({i})")
                continue

            record_title = f"{meta['title']} ({i})" if has_multiple else meta["title"]
            post_id = meta["post_url"].rstrip("/").split("/")[-2]
            source_id = f"{post_id}-{i}" if has_multiple else post_id

            self.logger.info(f"✅ Scholarship: {data.get('title', record_title)}")

            item = ScholarshipItem()

            # --- Fields shared across all sources ---
            item["source"] = "ministry"
            item["source_id"] = source_id
            item["title"] = data.get("title") or record_title
            item["slug"] = self.slugify(data.get("title") or record_title)
            item["source_url"] = meta["post_url"]
            item["organization_name"] = data.get("organization")
            item["country"] = data.get("country")
            item["deadline"] = data.get("deadline")
            item["no_deadline"] = False
            item["image_url"] = None
            item["description_html"] = data.get("description")
            item["benefits_html"] = data.get("benefits")
            item["qualifications_html"] = data.get("requirements")
            item["apply_link"] = group["application_link"]
            item["apply_email"] = None
            item["apply_phone"] = None
            item["status"] = "pending"
            item["scraped_at"] = datetime.now(timezone.utc).isoformat()
            item["reviewed_at"] = None
            item["reviewed_by"] = None

            # --- Fields specific to the Ministry source ---
            item["is_extension"] = False
            item["pdf_url"] = group["instructions_pdf"]
            item["attachments"] = group["attachments"]

            yield item

    def slugify(self, title):
        """Converts a title into a URL-friendly slug."""
        text = title.strip().lower()
        text = re.sub(r'[^\w\s-]', '', text)
        text = re.sub(r'[\s]+', '-', text)
        return text[:80]