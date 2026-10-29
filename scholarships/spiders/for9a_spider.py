import json
from datetime import date

import scrapy

from scholarships.for9a_parser import build_record


class For9aSpider(scrapy.Spider):
    """
    Preview spider: scrapes for9a (Arabic + English pages) and writes records
    shaped like the frontend contract to output/for9a.json.

    It does NOT touch the database (ITEM_PIPELINES is emptied below).

    Usage:
        scrapy crawl for9a -a limit=10     # first 10 scholarships
        scrapy crawl for9a                 # everything
        scrapy crawl for9a -a llm=0        # without the LLM step
    """

    name = "for9a"
    graphql_url = "https://lara.for9a.com/graphql"

    custom_settings = {
        # DB is written only when DATABASE_URL is set (DB_DRY_RUN=1 forces a dry run). LLM step: -a llm=0 skips it
        "ITEM_PIPELINES": {
            "scholarships.db_pipeline.SkipUnchangedPipeline": 100,
            "scholarships.llm_pipeline.LLMEnrichmentPipeline": 300,
            "scholarships.filter_pipeline.DropUnwantedPipeline": 400,
            "scholarships.db_pipeline.PostgresPipeline": 500,
        },
        "FEEDS": {
            "output/for9a.json": {
                "format": "json",
                "encoding": "utf8",
                "indent": 2,
                "overwrite": True,
            }
        },
    }

    opportunities_query = """
    query opportunities($first: Int!, $page: Int!, $landing_filter: LandingFilter) {
      opportunities(
        first: $first
        page: $page
        input: {type: EDU, landing_filter: $landing_filter}
      ) {
        paginatorInfo {total, hasMorePages}
        data {id, url, title, deadline, no_deadline}
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

    def __init__(self, limit=None, llm="1", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_llm = str(llm) != "0"
        self.limit = int(limit) if limit else None
        self.scheduled = 0

    # ------------------------------------------------------------ listing
    async def start(self):
        yield self._list_request(page=1)

    def _list_request(self, page):
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
        return scrapy.Request(
            url=self.graphql_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"query": self.opportunities_query,
                             "variables": variables}),
            callback=self.parse_list,
            cb_kwargs={"page": page},
        )

    def parse_list(self, response, page):
        result = json.loads(response.text)["data"]["opportunities"]
        self.logger.info(f"Page {page}: {len(result['data'])} opportunities")

        for summary in result["data"]:
            if self.limit and self.scheduled >= self.limit:
                return
            self.scheduled += 1
            yield scrapy.Request(
                url=summary["url"],
                callback=self.parse_ar,
                errback=self.page_failed,
                cb_kwargs={"summary": summary},
            )

        more = result["paginatorInfo"]["hasMorePages"]
        if more and not (self.limit and self.scheduled >= self.limit):
            yield self._list_request(page=page + 1)

    # ------------------------------------------------------- detail pages
    @staticmethod
    def _extract_opportunity(response):
        raw = response.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
        return json.loads(raw)["props"]["pageProps"]["data"]["opportunity"]

    def parse_ar(self, response, summary):
        try:
            ar = self._extract_opportunity(response)
        except Exception as e:
            self.logger.error(f"AR page failed for {summary['id']}: {e!r}")
            return

        if not self._is_active(ar, summary):
            return

        trans_url = ar.get("transUrl")
        if not trans_url:
            yield self._app_info_request(summary, ar, None)
            return

        yield scrapy.Request(
            url=trans_url,
            callback=self.parse_en,
            errback=self.en_failed,
            cb_kwargs={"summary": summary, "ar": ar},
        )

    def page_failed(self, failure):
        """A page request failed. For HTTP errors, log what the site itself says (headers and the
        start of the body) so a block can be told apart from a bug."""
        response = getattr(failure.value, "response", None)
        if response is None:
            self.logger.warning(f"Page request failed: {failure.request.url}: {failure.value!r}")
            return
        keep = ("server", "cf-ray", "cf-mitigated", "retry-after", "content-type")
        headers = {k: response.headers.get(k, b"").decode("latin-1") for k in keep
                   if response.headers.get(k)}
        body = " ".join(response.text[:300].split())
        self.logger.warning(
            f"HTTP {response.status} for {failure.request.url} headers={headers} body={body!r}")

    def parse_en(self, response, summary, ar):
        try:
            en = self._extract_opportunity(response)
        except Exception as e:
            self.logger.warning(f"EN page failed for {summary['id']}: {e!r}")
            en = None
        yield self._app_info_request(summary, ar, en)

    def en_failed(self, failure):
        kw = failure.request.cb_kwargs
        self.logger.warning(
            f"EN request failed for {kw['summary']['id']}: {failure.value!r}"
        )
        yield self._app_info_request(kw["summary"], kw["ar"], None)

    def _app_info_request(self, summary, ar, en):
        payload = {
            "query": self.application_info_query,
            "variables": {"id": int(summary["id"])},
            "operationName": "applicationInfo",
        }
        return scrapy.Request(
            url=self.graphql_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
            callback=self.parse_app_info,
            cb_kwargs={"summary": summary, "ar": ar, "en": en},
            dont_filter=True,
        )

    def parse_app_info(self, response, summary, ar, en):
        try:
            app = json.loads(response.text)["data"]["applicationInfo"]
        except Exception as e:
            self.logger.warning(f"applicationInfo failed for {summary['id']}: {e!r}")
            app = None
        yield build_record(summary, ar, en, app)

    # ------------------------------------------------------------ helpers
    def _is_active(self, ar, summary):
        if ar.get("closed"):
            self.logger.info(f"Skipped closed: {summary['title']}")
            return False
        deadline = summary.get("deadline")
        if summary.get("no_deadline") or not deadline:
            return True
        try:
            expired = date.fromisoformat(str(deadline)[:10]) < date.today()  # a deadline of today is still open
        except ValueError:
            self.logger.warning(f"Unreadable deadline {deadline!r} for {summary['id']}: kept")
            return True
        if expired:
            self.logger.info(f"Skipped expired: {summary['title']}")
            return False
        return True