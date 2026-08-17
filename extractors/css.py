"""Pull a value out of an HTML message with a CSS selector.

Worth knowing what soupsieve gives you here: `:has()` and sibling combinators
both work, which is what makes an email like PG&E's tractable. Their statement
labels the amount with an *image*, not text -- there is nothing to match on but
the structure and the image filename:

    td:has(img[src*="amount-due"]) + td strong
"""

from extractors import ExtractionError, extractor


@extractor("css")
def extract(email, config, record):
    from bs4 import BeautifulSoup
    from soupsieve import SelectorSyntaxError

    selector = config.get("selector")
    if not selector:
        raise ExtractionError("a `css` step needs a `selector`")

    html = email.source(config.get("source", "html"))
    soup = BeautifulSoup(html or "", "html.parser")

    try:
        matched = soup.select(selector)
    except (SelectorSyntaxError, NotImplementedError) as e:
        raise ExtractionError(f"bad selector {selector!r}: {e}") from e

    if not matched:
        return None

    attr = config.get("attr")

    def value_of(element):
        if attr:
            return element.get(attr)
        return element.get_text(" ", strip=True)

    if config.get("all"):
        return [v for v in (value_of(e) for e in matched) if v is not None]

    if len(matched) > 1 and config.get("strict"):
        raise ExtractionError(
            f"selector {selector!r} matched {len(matched)} elements; "
            f"make it more specific or set `all: true`"
        )
    return value_of(matched[0])
