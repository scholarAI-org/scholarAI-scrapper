BOT_NAME = "scholarships"

SPIDER_MODULES = ["scholarships.spiders"]
NEWSPIDER_MODULE = "scholarships.spiders"

ROBOTSTXT_OBEY = True

# Be gentle with the sources
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1
AUTOTHROTTLE_MAX_DELAY = 30
RETRY_TIMES = 2
DOWNLOAD_TIMEOUT = 30

# No USER_AGENT here: Scrapy's honest default is used (for9a rejects a "Mozilla/5.0 (compatible; ...)"
# look-alike from cloud IPs). The Telegram spider sets its own in custom_settings.

# Pipelines are set per spider (custom_settings). The database writer will be
# added here once the backend schema is final.
ITEM_PIPELINES = {}

FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = "INFO"

# end the run gracefully before the CI job timeout (45 min) if a source never finishes
CLOSESPIDER_TIMEOUT = 40 * 60