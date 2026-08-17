import re
import json
from datetime import datetime, timezone

import scrapy
from bs4 import BeautifulSoup

from scholarships.items import ScholarshipItem


class For9aSpider(scrapy.Spider):
    """
    Collects scholarships from for9a.com via the site's GraphQL API,
    filtered for Palestinian nationality and "scholarships" category.
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
        paginatorInfo {
          total
          hasMorePages
        }
        data {
          id
          url
          title
          deadline
          no_deadline
          country { place { name } }
          organization { name }
        }
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
        yield self.make_opportunities_request(page=1)

    def make_opportunities_request(self, page):
        """Builds a POST request for a given page of the opportunities list."""
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
        """
        Handles the GraphQL response for the opportunities list.
        Requests each opportunity's full page, and the next page if available.
        """
        data = json.loads(response.text)
        result = data["data"]["opportunities"]

        self.logger.info(f"Page {page}: got {len(result['data'])} opportunities")

        for opportunity_summary in result["data"]:
            yield scrapy.Request(
                url=opportunity_summary["url"],
                callback=self.parse_opportunity_page,
                cb_kwargs={"opportunity_summary": opportunity_summary},
            )

        if result["paginatorInfo"]["hasMorePages"]:
            yield self.make_opportunities_request(page=page + 1)

    def parse_opportunity_page(self, response, opportunity_summary):
        """
        Handles a single opportunity's full HTML page: extracts __NEXT_DATA__,
        then requests applicationInfo (the third and final request in the chain).
        """
        next_data_text = response.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
        next_data = json.loads(next_data_text)
        opp_details = next_data["props"]["pageProps"]["data"]["opportunity"]

        description_html = ""
        benefits_html = ""
        qualifications_html = ""
        for section in opp_details.get("descriptions", []):
            if section["header"] == "وصف الفرصة":
                description_html = section["body"]
            elif section["header"] == "المنافع والفوائد":
                benefits_html = section["body"]
            elif section["header"] == "معايير التقديم":
                qualifications_html = section["body"]

        image_url = None
        if opp_details.get("image"):
            image_url = (
                f"https://images.for9a.com/thumb/max-2000-auto-100-jpeg/"
                f"opportunity/{opp_details['image']['name']}"
            )

        partial_data = {
            "opportunity_summary": opportunity_summary,
            "description_html": description_html,
            "benefits_html": benefits_html,
            "qualifications_html": qualifications_html,
            "image_url": image_url,
        }

        opp_id = int(opportunity_summary["id"])
        payload = {
            "query": self.application_info_query,
            "variables": {"id": opp_id},
            "operationName": "applicationInfo",
        }

        yield scrapy.Request(
            url=self.graphql_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
            callback=self.parse_application_info,
            cb_kwargs=partial_data,
        )

    def parse_application_info(self, response, opportunity_summary, description_html,
                                benefits_html, qualifications_html, image_url):
        """
        Final step in the chain: receives the application link, checks the
        deadline validity, and builds the final Item (only if still active).
        """
        deadline_str = opportunity_summary["deadline"]
        no_deadline = opportunity_summary["no_deadline"]

        if not no_deadline and deadline_str:
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d")
            if deadline_date < datetime.now():
                self.logger.info(
                    f"⏭️ Skipped expired opportunity: {opportunity_summary['title']} "
                    f"(deadline: {deadline_str})"
                )
                return

        app_data = json.loads(response.text)["data"]["applicationInfo"]
        apply_link = app_data["link"] or app_data["official_link"] or opportunity_summary["url"]

        country = None
        if opportunity_summary["country"] and opportunity_summary["country"]["place"]:
            country = opportunity_summary["country"]["place"]["name"]

        organization_name = None
        if opportunity_summary["organization"]:
            organization_name = opportunity_summary["organization"]["name"]

        item = ScholarshipItem()

        # --- Fields shared across all sources ---
        item["source"] = "for9a"
        item["source_id"] = opportunity_summary["id"]
        item["title"] = opportunity_summary["title"]
        item["slug"] = self.slugify(opportunity_summary["title"])
        item["source_url"] = opportunity_summary["url"]
        item["organization_name"] = organization_name
        item["country"] = country
        item["deadline"] = deadline_str
        item["no_deadline"] = no_deadline
        item["image_url"] = image_url
        item["description_html"] = self.clean_promotional_content(description_html)
        item["benefits_html"] = self.clean_promotional_content(benefits_html)
        item["qualifications_html"] = self.clean_promotional_content(qualifications_html)
        item["apply_link"] = apply_link
        item["apply_email"] = app_data["email"] or None
        item["apply_phone"] = app_data["phone"] or None
        item["status"] = "pending"
        item["scraped_at"] = datetime.now(timezone.utc).isoformat()
        item["reviewed_at"] = None
        item["reviewed_by"] = None

        # --- Fields specific to the Ministry source, unused here but kept
        #     for a consistent record structure across all sources ---
        item["pdf_url"] = None
        item["attachments"] = []
        item["is_extension"] = False

        yield item

    def slugify(self, title):
        """Converts a title into a URL-friendly slug."""
        text = title.strip().lower()
        text = re.sub(r'[^\w\s-]', '', text)
        text = re.sub(r'[\s]+', '-', text)
        return text[:80]

    def clean_promotional_content(self, html_content):
        """
        Removes promotional paragraphs containing internal links back to
        for9a.com (e.g., "Learn more about...", "See more...").
        """
        if not html_content:
            return html_content

        soup = BeautifulSoup(html_content, "html.parser")
        for paragraph in soup.find_all("p"):
            links_in_paragraph = paragraph.find_all("a", href=True)
            for link in links_in_paragraph:
                if "for9a.com" in link["href"]:
                    paragraph.decompose()
                    break

        return str(soup)