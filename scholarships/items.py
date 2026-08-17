import scrapy


class ScholarshipItem(scrapy.Item):
    """
    Unified schema for a scholarship record, regardless of its source
    (for9a or the Ministry website). Every spider must populate every
    field, using None/empty defaults for fields that don't apply to
    that particular source, so all records share the same structure
    when stored in the database.
    """

    # --- Identification & source ---
    source = scrapy.Field()          # "for9a" or "ministry"
    source_id = scrapy.Field()       # unique ID from the original site
    title = scrapy.Field()
    slug = scrapy.Field()
    source_url = scrapy.Field()

    # --- Summary fields (for the scholarship card) ---
    organization_name = scrapy.Field()
    country = scrapy.Field()
    deadline = scrapy.Field()
    no_deadline = scrapy.Field()
    image_url = scrapy.Field()

    # --- Full details (for the scholarship detail page) ---
    description_html = scrapy.Field()
    benefits_html = scrapy.Field()
    qualifications_html = scrapy.Field()

    # --- Application info ---
    apply_link = scrapy.Field()
    apply_email = scrapy.Field()
    apply_phone = scrapy.Field()

    # --- Ministry-source-specific fields ---
    pdf_url = scrapy.Field()
    attachments = scrapy.Field()     # list of additional PDF URLs (e.g. eligible specializations)
    is_extension = scrapy.Field()    # True if this record is a deadline-extension notice

    # --- Review workflow ---
    status = scrapy.Field()          # "pending" | "published" | "rejected"
    scraped_at = scrapy.Field()
    reviewed_at = scrapy.Field()
    reviewed_by = scrapy.Field()