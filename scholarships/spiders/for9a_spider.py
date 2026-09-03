import re
import json
from datetime import datetime, timezone

import scrapy
from bs4 import BeautifulSoup

from scholarships.items import ScholarshipItem
from scholarships.utils_html import sanitize_html


class For9aSpider(scrapy.Spider):
    """
    Scrapes scholarships from for9a.com via GraphQL API.
    """

    name = "for9a"
    graphql_url = "https://lara.for9a.com/graphql"

    opportunities_query = """
    query opportunities($first: Int!, $page: Int!, $landing_filter: LandingFilter, $upcoming: Boolean, $no_deadline: Boolean, $most_applied: Boolean, $published_today: Boolean) {
      opportunities(
        first: $first
        page: $page
        input: {type: EDU, landing_filter: $landing_filter, upcoming: $upcoming, no_deadline: $no_deadline, most_applied: $most_applied, published_today: $published_today}
      ) {
        paginatorInfo {total, hasMorePages}
        data {id, url, title, deadline, no_deadline, country {place {name}}, organization {name}}
      }
    }
    """

    application_info_query = """
    query applicationInfo($id: Int!) {
      applicationInfo(id: $id) {
        email
        phone
        link
        official_link
        how_to_apply
      }
    }
    """

    async def start(self):
        yield self._make_opportunities_request(page=1)

    def _make_opportunities_request(self, page):
        variables = {
            "first": 15,
            "page": page,
            "landing_filter": {
                "type": "منح-دراسية",
                "subType": "all",
                "funding": "all",
                "opportunityLocation": "all",
                "field": "all",
                "nationality": "فلسطينيين",
            },
        }

        payload = {"query": self.opportunities_query, "variables": variables}

        return scrapy.Request(
            url=self.graphql_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
            callback=self.parse_opportunities_list,
            cb_kwargs={"page": page},
        )

    def parse_opportunities_list(self, response, page):
        data = json.loads(response.text)
        result = data["data"]["opportunities"]

        self.logger.info(f"Page {page}: {len(result['data'])} opportunities")

        for opportunity in result["data"]:
            yield scrapy.Request(
                url=opportunity["url"],
                callback=self.parse_opportunity_page,
                cb_kwargs={"opportunity_summary": opportunity},
            )

        if result["paginatorInfo"]["hasMorePages"]:
            yield self._make_opportunities_request(page=page + 1)

    def parse_opportunity_page(self, response, opportunity_summary):
        next_data_text = response.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
        next_data = json.loads(next_data_text)
        opp_details = next_data["props"]["pageProps"]["data"]["opportunity"]

        description_html = self._combine_descriptions(
            opp_details.get("descriptions", [])
        )

        image_url = self._extract_image_url(opp_details)

        payload = {
            "query": self.application_info_query,
            "variables": {"id": int(opportunity_summary["id"])},
            "operationName": "applicationInfo",
        }

        yield scrapy.Request(
            url=self.graphql_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
            callback=self.parse_application_info,
            cb_kwargs={
                "opportunity_summary": opportunity_summary,
                "description_html": description_html,
                "image_url": image_url,
            },
        )

    def parse_application_info(self, response, opportunity_summary, description_html, image_url):
        if not self._is_active(opportunity_summary):
            return

        app_data = json.loads(response.text)["data"]["applicationInfo"]
        apply_link = (
            app_data["link"]
            or app_data["official_link"]
            or opportunity_summary["url"]
        )

        item = ScholarshipItem()

        item["source"] = "for9a"
        item["source_id"] = opportunity_summary["id"]
        item["title"] = opportunity_summary["title"]
        item["slug"] = self._slugify(opportunity_summary["title"])
        item["source_url"] = opportunity_summary["url"]
        item["organization_name"] = self._get_organization(opportunity_summary)
        item["country"] = self._get_country(opportunity_summary)
        item["deadline"] = opportunity_summary["deadline"]
        item["no_deadline"] = opportunity_summary["no_deadline"]
        item["image_url"] = image_url

        item["description_html"] = sanitize_html(
            self._clean_promotional_content(description_html)
        )
        item["benefits_html"] = ""
        item["qualifications_html"] = ""

        item["apply_link"] = apply_link
        item["apply_email"] = app_data.get("email")
        item["apply_phone"] = app_data.get("phone")

        item["status"] = "pending"
        item["scraped_at"] = datetime.now(timezone.utc).isoformat()
        item["reviewed_at"] = None
        item["reviewed_by"] = None

        item["pdf_url"] = None
        item["attachments"] = []
        item["is_extension"] = False

        yield item

    # ==================== Helper Methods ====================

    def _combine_descriptions(self, descriptions):
        combined = ""
        for section in descriptions:
            header = section.get("header", "")
            body = section.get("body", "")
            if header:
                combined += f"<h3>{header}</h3>"
            if body:
                combined += body
        return combined

    def _extract_image_url(self, opp_details):
        if not opp_details.get("image"):
            return None
        
        image_name = opp_details["image"]["name"]
        return f"https://images.for9a.com/thumb/max-2000-auto-100-jpeg/opportunity/{image_name}"

    def _is_active(self, opportunity_summary):
        deadline_str = opportunity_summary.get("deadline")
        no_deadline = opportunity_summary.get("no_deadline")

        if no_deadline or not deadline_str:
            return True

        deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d")
        if deadline_date < datetime.now():
            self.logger.info(f"⏰ Skipped expired: {opportunity_summary['title']}")
            return False

        return True

    def _get_organization(self, opportunity_summary):
        org = opportunity_summary.get("organization")
        return org.get("name") if org else None

    def _get_country(self, opportunity_summary):
        country_obj = opportunity_summary.get("country")
        if country_obj and country_obj.get("place"):
            return country_obj["place"].get("name")
        return None

    def _slugify(self, title):
        text = title.strip().lower()
        text = re.sub(r'[^\w\s-]', '', text)
        text = re.sub(r'[\s]+', '-', text)
        return text[:80]


    def _clean_promotional_content(self, html_content):
        if not html_content:
            return html_content

        soup = BeautifulSoup(html_content, "html.parser")
        
        for link in soup.find_all("a"):
            link.replace_with(link.get_text())

        for paragraph in soup.find_all("p"):
            if not paragraph.get_text().strip():
                paragraph.decompose()

        return str(soup)