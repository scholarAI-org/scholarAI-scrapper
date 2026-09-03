import nh3


ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "h2",
    "h3",
    "a",
}


ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
}


def sanitize_html(html):
    if not html:
        return html

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https"},
    )