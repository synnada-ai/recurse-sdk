"""The example executes dependent paid reads against a faithful credential-free helper double."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest

import recurse

EXAMPLE = Path(__file__).parents[1] / "examples" / "company-updates"


class ReadAPIs:
    """Return native provider JSON while recording dependent endpoint arguments."""

    def __init__(self) -> None:
        """Prepare optional responses and error injection without paid network access."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, list[Any]] = {}
        self.failures: dict[str, Exception] = {}

    def call(self, endpoint_id: str, arguments: dict[str, Any]) -> Any:
        """Record one approved request and return its selected native provider response."""
        self.calls.append((endpoint_id, arguments))
        if endpoint_id in self.failures:
            raise self.failures[endpoint_id]
        queue = self.responses.get(endpoint_id)
        if queue:
            return queue.pop(0)
        if endpoint_id == "exa.web.search":
            domain = arguments["includeDomains"][0]
            return {
                "results": [
                    {
                        "url": f"https://{domain}/blog/update#announcement",
                        "title": f"Product update from {domain}",
                        "publishedDate": "2026-10-01T10:00:00Z",
                    }
                ]
            }
        assert endpoint_id == "firecrawl.web.scrape"
        return {"success": True, "data": {"markdown": "Official update.\n\nDetails here."}}


@pytest.fixture
def read_apis() -> ReadAPIs:
    """Provide a fresh bounded provider double for each example execution."""
    return ReadAPIs()


@pytest.fixture
def company_tools(read_apis: ReadAPIs, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Inject only the runtime helper and import the real example source."""
    helper = ModuleType("recurse_apis")
    monkeypatch.setattr(helper, "call", read_apis.call, raising=False)
    monkeypatch.setitem(sys.modules, "recurse_apis", helper)
    specification = importlib.util.spec_from_file_location(
        "company_update_example", EXAMPLE / "company_updates.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_example_composes_search_then_scrape_with_schemas_and_real_source_bundle(
    company_tools: ModuleType, read_apis: ReadAPIs
) -> None:
    """Six bounded reads produce linked evidence without credentials or invented dates."""
    inputs = json.loads((EXAMPLE / "inputs.json").read_text())
    result = company_tools.company_updates(**inputs)
    manifest = recurse.load_tools_manifest(EXAMPLE)
    registration = manifest["tools"]["register"]["company_updates"]
    jsonschema.Draft202012Validator(registration["output_schema"]).validate(result)
    assert registration["apis"] == ["exa.web.search", "firecrawl.web.scrape"]
    assert [endpoint for endpoint, _arguments in read_apis.calls] == [
        "exa.web.search",
        "firecrawl.web.scrape",
        "exa.web.search",
        "firecrawl.web.scrape",
        "exa.web.search",
        "firecrawl.web.scrape",
    ]
    for index, domain in enumerate(("linear.app", "notion.com", "clickup.com")):
        search = read_apis.calls[index * 2][1]
        scrape = read_apis.calls[index * 2 + 1][1]
        assert search["numResults"] == 3
        assert search["type"] == "instant"
        assert search["includeDomains"] == [domain]
        assert search["startPublishedDate"] == "2026-09-14T00:00:00.000Z"
        assert search["endPublishedDate"] == "2026-10-14T23:59:59.999Z"
        assert scrape == {
            "url": f"https://{domain}/blog/update",
            "formats": ["markdown"],
            "parsers": [],
            "maxAge": 0,
            "onlyMainContent": True,
        }
        source = result["companies"][index]["source"]
        assert source["url"] == scrape["url"]
        assert source["published_date"] == "2026-10-01T10:00:00Z"
        assert source["excerpt"] == "Official update. Details here."
    artifacts, record = recurse.build_tools_bundle(EXAMPLE)
    assert artifacts["source"]
    assert record["apiVersion"] == "recurse.tools.application/v1alpha1"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "http://linear.app/update",
        "https:///update",
        "https://linear.app.evil.test/update",
        "https://user:password@linear.app/update",
        "https://linear.app:8443/update",
        "https://linear.app/update.PDF",
        "https://linear.app/update%2Epdf",
        "https://linear.app:invalid/update",
        "https://[invalid]/update",
    ],
)
def test_official_url_filter_rejects_unsafe_or_unsupported_targets(
    company_tools: ModuleType, value: Any
) -> None:
    """Search results cannot redirect the example to unrelated hosts or unsupported documents."""
    assert company_tools._official_url(value, "linear.app") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://linear.app", "https://linear.app/"),
        ("https://BLOG.LINEAR.APP:443/update?id=123#part", "https://blog.linear.app/update?id=123"),
    ],
)
def test_official_url_filter_preserves_meaningful_query_and_normalizes_fragments(
    company_tools: ModuleType, value: str, expected: str
) -> None:
    """Canonicalization preserves page identity and allows official HTTPS subdomains."""
    assert company_tools._official_url(value, "linear.app") == expected


def test_result_selection_deduplicates_and_leaves_missing_fields_empty(
    company_tools: ModuleType,
) -> None:
    """Non-results and duplicate URLs cannot cause extra fetches or fabricated metadata."""
    seen = {"https://linear.app/already"}
    selected = company_tools._first_official_result(
        [
            None,
            {"url": "http://linear.app/no"},
            {"url": "https://linear.app/already#duplicate"},
            {"url": "https://linear.app/new", "title": 1, "publishedDate": None},
        ],
        "linear.app",
        seen,
    )
    assert selected == {"url": "https://linear.app/new", "title": "", "published_date": None}
    assert seen == {"https://linear.app/already", "https://linear.app/new"}


def test_empty_official_results_do_not_trigger_a_substitute_fetch(
    company_tools: ModuleType, read_apis: ReadAPIs
) -> None:
    """Missing official evidence remains explicit instead of selecting another domain or fixture."""
    read_apis.responses["exa.web.search"] = [{"results": []}]
    result = company_tools.company_updates("2026-09-14", "2026-10-14")
    assert result["companies"][0] == {
        "company": "Linear",
        "source": None,
        "missing_reason": "no_official_result",
    }
    assert len(read_apis.calls) == 5


@pytest.mark.parametrize("response", [None, {}, {"results": None}])
def test_malformed_search_response_stops_before_scraping(
    company_tools: ModuleType, read_apis: ReadAPIs, response: Any
) -> None:
    """An upstream response outside its expected contract is a visible failure."""
    read_apis.responses["exa.web.search"] = [response]
    with pytest.raises(ValueError, match="results array"):
        company_tools.company_updates("2026-09-14", "2026-10-14")
    assert len(read_apis.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"data": []},
        {"data": {}},
        {"data": {"markdown": 1}},
        {"data": {"markdown": "   "}},
    ],
)
def test_missing_markdown_keeps_source_but_reports_missing_content(
    company_tools: ModuleType, read_apis: ReadAPIs, response: Any
) -> None:
    """Target errors and absent content never become invented excerpts or successful evidence."""
    read_apis.responses["firecrawl.web.scrape"] = [response]
    result = company_tools.company_updates("2026-09-14", "2026-10-14")
    first = result["companies"][0]
    assert first["source"]["url"] == "https://linear.app/blog/update"
    assert first["source"]["excerpt"] == ""
    assert first["missing_reason"] == "no_markdown"


def test_excerpt_is_bounded_and_missing_publication_date_is_not_filled(
    company_tools: ModuleType, read_apis: ReadAPIs
) -> None:
    """Output remains bounded and preserves uncertainty from provider metadata."""
    read_apis.responses["exa.web.search"] = [{"results": [{"url": "https://linear.app/update"}]}]
    read_apis.responses["firecrawl.web.scrape"] = [{"data": {"markdown": "x" * 3000}}]
    result = company_tools.company_updates("2026-09-14", "2026-10-14")
    source = result["companies"][0]["source"]
    assert source["published_date"] is None
    assert source["title"] == ""
    assert len(source["excerpt"]) == 1200


@pytest.mark.parametrize("status", [403, 404, 500])
def test_target_error_document_is_missing_evidence_even_with_markdown(
    company_tools: ModuleType, read_apis: ReadAPIs, status: int
) -> None:
    """A billable error document cannot masquerade as successful product-update evidence."""
    read_apis.responses["firecrawl.web.scrape"] = [
        {"data": {"markdown": "Forbidden or missing page", "metadata": {"statusCode": status}}}
    ]
    first = company_tools.company_updates("2026-09-14", "2026-10-14")["companies"][0]
    assert first["missing_reason"] == "target_error"
    assert first["source"]["excerpt"] == ""
    assert first["source"]["url"] == "https://linear.app/blog/update"
    assert len(read_apis.calls) == 6


def test_successful_target_metadata_preserves_real_markdown(
    company_tools: ModuleType, read_apis: ReadAPIs
) -> None:
    """An ordinary successful target status leaves its evidence available."""
    read_apis.responses["firecrawl.web.scrape"] = [
        {"data": {"markdown": "Actual update", "metadata": {"statusCode": 200}}}
    ]
    first = company_tools.company_updates("2026-09-14", "2026-10-14")["companies"][0]
    assert first["missing_reason"] is None
    assert first["source"]["excerpt"] == "Actual update"


@pytest.mark.parametrize(
    ("start", "end"), [("2026-10-14", "2026-09-14"), ("not-a-date", "2026-10-14")]
)
def test_invalid_date_window_makes_no_api_requests(
    company_tools: ModuleType, read_apis: ReadAPIs, start: str, end: str
) -> None:
    """Invalid temporal inputs fail before submitting billable provider requests."""
    with pytest.raises(ValueError):
        company_tools.company_updates(start, end)
    assert read_apis.calls == []


def test_api_failure_stops_work_without_repeating_successful_calls(
    company_tools: ModuleType, read_apis: ReadAPIs
) -> None:
    """Provider and budget errors propagate without hidden retries."""
    read_apis.failures["firecrawl.web.scrape"] = RuntimeError("provider request failed")
    with pytest.raises(RuntimeError, match="provider request failed"):
        company_tools.company_updates("2026-09-14", "2026-10-14")
    assert [endpoint for endpoint, _arguments in read_apis.calls] == [
        "exa.web.search",
        "firecrawl.web.scrape",
    ]
