"""Collect bounded first-party product-update evidence through approved paid read APIs."""

from datetime import date
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from recurse_apis import call  # type: ignore[import-not-found]  # supplied by the isolated runtime

_COMPANIES = (("Linear", "linear.app"), ("Notion", "notion.com"), ("ClickUp", "clickup.com"))


def _official_url(value: object, domain: str) -> str | None:
    """Keep public HTTPS HTML pages on the company's own domain and remove fragments."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or host is None
            or not (host == domain or host.endswith("." + domain))
            or parsed.username is not None
            or parsed.port not in {None, 443}
            or unquote(parsed.path).lower().endswith(".pdf")
        ):
            return None
    except ValueError:
        return None
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _first_official_result(
    results: list[Any], domain: str, seen: set[str]
) -> dict[str, Any] | None:
    """Select one distinct official result without inventing a missing publication date."""
    for result in results:
        if not isinstance(result, dict):
            continue
        url = _official_url(result.get("url"), domain)
        if url is None or url in seen:
            continue
        seen.add(url)
        title = result.get("title")
        published_date = result.get("publishedDate")
        return {
            "url": url,
            "title": title if isinstance(title, str) else "",
            "published_date": published_date if isinstance(published_date, str) else None,
        }
    return None


def company_updates(start_date: str, end_date: str) -> dict[str, Any]:
    """Search and fetch one official update page per company within an explicit UTC date window.

    Args:
        start_date: First publication date, formatted YYYY-MM-DD.
        end_date: Last publication date, formatted YYYY-MM-DD.

    Returns:
        Evidence with source URLs, provider dates, short excerpts and missing reasons.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    companies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for company, domain in _COMPANIES:
        search = call(
            "exa.web.search",
            {
                "query": f"{company} official product updates announcements",
                "type": "instant",
                "numResults": 3,
                "includeDomains": [domain],
                "startPublishedDate": start.isoformat() + "T00:00:00.000Z",
                "endPublishedDate": end.isoformat() + "T23:59:59.999Z",
            },
        )
        if not isinstance(search, dict) or not isinstance(search.get("results"), list):
            raise ValueError("search response must contain a results array")
        selected = _first_official_result(search["results"], domain, seen)
        if selected is None:
            companies.append(
                {"company": company, "source": None, "missing_reason": "no_official_result"}
            )
            continue
        page = call(
            "firecrawl.web.scrape",
            {
                "url": selected["url"],
                "formats": ["markdown"],
                "parsers": [],
                "maxAge": 0,
                "onlyMainContent": True,
            },
        )
        data = page.get("data") if isinstance(page, dict) else None
        markdown = data.get("markdown") if isinstance(data, dict) else None
        metadata = data.get("metadata") if isinstance(data, dict) else None
        status = metadata.get("statusCode") if isinstance(metadata, dict) else None
        if isinstance(status, int) and status >= HTTPStatus.BAD_REQUEST:
            selected["excerpt"] = ""
            missing_reason: str | None = "target_error"
        else:
            selected["excerpt"] = (
                " ".join(markdown.split())[:1200] if isinstance(markdown, str) else ""
            )
            missing_reason = None if selected["excerpt"] else "no_markdown"
        companies.append(
            {
                "company": company,
                "source": selected,
                "missing_reason": missing_reason,
            }
        )
    return {"window": {"start": start.isoformat(), "end": end.isoformat()}, "companies": companies}
