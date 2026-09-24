"""The pluggable web-lookup layer: determinism offline, and refusals on the wire.

Two halves matter here. `FakeSearchAPI` has to be good enough that the whole
drafting loop can be tested without a key or a network, and `HttpSearchAPI` has
to refuse the URLs a fetched page must never be pointed at — because the input
to `read_url` comes out of a search result produced by a third party.
"""

from __future__ import annotations

import io
import json

import pytest

from search import (
    FakeSearchAPI,
    HttpSearchAPI,
    Page,
    SearchError,
    SearchResult,
    UnsafeURL,
    normalise_contact,
    validate_target_url,
    validate_url,
)

PROSPECT_QUERIES = [
    "e-commerce agent builders",
    "indie hackers dropshipping margin tools",
    "dropshipper community forum shipping weight",
]
DIRECTORY_QUERIES = [
    "ai agent tool directories api marketplaces",
    "langchain hubs composio registries",
]


# --- the fake --------------------------------------------------------------


def test_the_default_provider_is_the_fake_one(monkeypatch):
    """The safe default must be the default, not an opt-in."""
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    assert isinstance(FakeSearchAPI(), FakeSearchAPI)
    from search import get_search

    assert isinstance(get_search(), FakeSearchAPI)


def test_an_unknown_provider_raises_instead_of_reaching_the_network(monkeypatch):
    """A typo in .env must not quietly buy a real search bill."""
    monkeypatch.setenv("SEARCH_PROVIDER", "tavilyy")
    from search import get_search

    with pytest.raises(ValueError):
        get_search()


@pytest.mark.parametrize("query", PROSPECT_QUERIES)
def test_every_prospect_query_returns_only_businesses(query):
    """A prospecting search that returned a directory would be drafted at a
    listing site instead of the five builders the goal asked for."""
    results = FakeSearchAPI().search(query, max_results=8)
    assert results, f"no fixture matched {query!r}"
    for row in results:
        assert "directory" not in row.title.lower() or "example.com" in row.url
        assert row.url.startswith("https://")


def test_prospects_have_a_contact_route_and_directories_do_not():
    """`contact` is what an email draft is aimed at; a directory is aimed at a
    page, and inventing an address for it would be a wasted send."""
    for query in PROSPECT_QUERIES:
        for row in FakeSearchAPI().search(query, max_results=8):
            page = FakeSearchAPI().read_url(row.url)
            assert "@" in page.contact, f"{row.url} has no contact route"
    for query in DIRECTORY_QUERIES:
        for row in FakeSearchAPI().search(query, max_results=8):
            page = FakeSearchAPI().read_url(row.url)
            assert page.requires, f"{row.url} states no submission requirements"


def test_the_fake_is_deterministic_across_instances():
    """Tests and dogfood runs have to be comparable, so ordering cannot drift."""
    a = [r.url for r in FakeSearchAPI().search(DIRECTORY_QUERIES[0], max_results=10)]
    b = [r.url for r in FakeSearchAPI().search(DIRECTORY_QUERIES[0], max_results=10)]
    assert a == b
    assert len(set(a)) == len(a)


def test_a_directory_query_routes_to_directories_and_not_to_prospects():
    directory_urls = {
        r.url for q in DIRECTORY_QUERIES for r in FakeSearchAPI().search(q, max_results=10)
    }
    prospect_urls = {
        r.url for q in PROSPECT_QUERIES for r in FakeSearchAPI().search(q, max_results=10)
    }
    assert directory_urls and prospect_urls
    assert not (directory_urls & prospect_urls)


def test_at_least_ten_directories_exist_for_the_acceptance_run():
    urls = {r.url for q in DIRECTORY_QUERIES for r in FakeSearchAPI().search(q, max_results=20)}
    assert len(urls) >= 10


def test_five_prospects_exist_for_the_acceptance_run():
    urls = {r.url for q in PROSPECT_QUERIES for r in FakeSearchAPI().search(q, max_results=20)}
    assert len(urls) >= 5


def test_max_results_is_honoured():
    assert len(FakeSearchAPI().search(DIRECTORY_QUERIES[0], max_results=3)) == 3


def test_an_unmatched_query_returns_nothing_rather_than_a_default():
    """Quietly returning *something* for any query would let the loop draft at
    a company nobody searched for."""
    assert FakeSearchAPI().search("zygote flux capacitor", max_results=8) == []


def test_every_result_carries_provenance_fields():
    """Hard rule in AGENTS.md: no datapoint without source and freshness."""
    for row in FakeSearchAPI().search(DIRECTORY_QUERIES[0], max_results=5):
        assert row.source_url == row.url
        assert row.observed_at
        assert 0 < row.confidence <= 1
        assert row.data_class == "fixture"
        assert json.dumps(row.to_dict())


def test_read_url_of_an_unknown_page_raises():
    with pytest.raises(SearchError):
        FakeSearchAPI().read_url("https://nothing-here.example.com/")


def test_a_failing_provider_surfaces_as_search_error():
    api = FakeSearchAPI(fail_with="quota exceeded")
    with pytest.raises(SearchError):
        api.search("anything")


def test_injected_targets_replace_the_corpus():
    api = FakeSearchAPI(
        targets=[
            {
                "name": "Injected Co",
                "url": "https://injected.example.com",
                "blurb": "does the thing",
                "tags": ["injected"],
                "contact": "hi@Injected.example.com ",
            }
        ]
    )
    results = api.search("e-commerce agent builders", max_results=5)
    assert [r.url for r in results] == ["https://injected.example.com"]
    assert api.read_url("https://injected.example.com").contact == "hi@injected.example.com"


# --- URL policy ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/v1/agent/info",
        "http://localhost/anything",
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://[::1]/",
        "file:///etc/passwd",
        "gopher://example.com/x",
        "",
        "   ",
        "not a url",
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_reserved_hosts_cannot_be_fetched_but_can_be_named():
    """The split the offline dogfood depends on.

    The fixture corpus lives on RFC 2606 hosts. Fetching one must stay refused,
    because that rule is what stops a live provider's placeholder rows being
    mistaken for data. Naming one as a draft's destination must be allowed,
    because nothing is fetched: `validate_target_url` is the flag-off path, and
    a backend that ever POSTs to a recipient has to re-check it without it.
    """
    fixture_url = "https://agenttoolbox.example.com/submit"
    with pytest.raises(UnsafeURL):
        validate_url(fixture_url)
    assert validate_url(fixture_url, allow_reserved=True) == fixture_url
    assert validate_target_url(fixture_url) == fixture_url


def test_an_address_that_only_looks_like_a_url_is_still_refused():
    with pytest.raises(UnsafeURL):
        validate_target_url("mailto:someone@somewhere")
    with pytest.raises(UnsafeURL):
        validate_target_url("https://")


def test_a_real_recipient_passes_both_checks():
    assert validate_url("https://docs.e-commerce.dev/pricing")
    assert validate_target_url("https://docs.e-commerce.dev/pricing")


def test_validate_target_url_still_blocks_metadata_endpoints():
    with pytest.raises(UnsafeURL):
        validate_target_url("http://169.254.169.254/")


def test_a_url_with_credentials_is_refused():
    with pytest.raises(UnsafeURL):
        validate_url("https://user:pass@example.org/")


# --- the live provider -----------------------------------------------------


class FakeHTTP:
    """An opener returning canned bytes, so no test touches a socket."""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.body = body
        self.content_type = content_type
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        response = io.BytesIO(self.body)
        response.headers = {"Content-Type": self.content_type}
        response.status = 200
        response.url = request.full_url
        return response


def test_http_provider_requires_a_key():
    """A whitespace key is no key: it would send an empty bearer and fail far
    from here, inside a provider's 401."""
    for blank in ("", "   ", None):
        with pytest.raises(SearchError):
            HttpSearchAPI(api_key=blank)


def test_http_search_parses_a_provider_response():
    payload = json.dumps(
        {
            "results": [
                {
                    "title": "Widget",
                    "url": "https://widget.example.net/",
                    "content": "makes widgets",
                    "score": 0.9,
                },
                {"title": "no url", "url": "", "content": "dropped"},
            ]
        }
    ).encode()
    http = FakeHTTP(payload, content_type="application/json")
    api = HttpSearchAPI(api_key="k", opener=http)
    results = api.search("widgets", max_results=5)
    assert [r.url for r in results] == ["https://widget.example.net/"]
    assert results[0].source_url == results[0].url
    # Labelled by where it came from, not as generically "live": a snippet from
    # a search engine is one step further from an observation than a page we read.
    assert results[0].data_class == "search_provider"
    assert "api_key" in http.requests[0].data.decode()


def test_http_read_url_extracts_title_and_contact():
    html = (
        b"<html><head><title>Ledgerlight</title></head><body>"
        b"<h1>Ledgerlight</h1><p>margin calculator for indie sellers</p>"
        b"<link rel='icon' href='/sprite@2x.png'>"
        b"<a href='mailto:Builder@LedgerLight.dev.'>email</a>"
        b"</body></html>"
    )
    api = HttpSearchAPI(api_key="k", opener=FakeHTTP(html))
    page = api.read_url("https://ledgerlight.dev/")
    assert page.title == "Ledgerlight"
    # Three things the page actually does: the address hides in a mailto: href,
    # a sprite filename matches the address pattern, and the published form is
    # mixed-case with a trailing full stop.
    assert page.contact == "Builder@ledgerlight.dev"
    assert "margin calculator" in page.text
    assert page.data_class == "web"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("mailto:hi@x.com", "hi@x.com"),
        ("  hi@X.COM  ", "hi@x.com"),
        ("Write to ops@co.dev.", "ops@co.dev"),
        ("no address here", ""),
        ("@x.com", ""),
        ("hi@x", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_contact_normalisation_accepts_only_a_real_address(raw, expected):
    assert normalise_contact(raw) == expected


def test_http_read_url_refuses_a_private_target_before_any_request():
    http = FakeHTTP(b"")
    api = HttpSearchAPI(api_key="k", opener=http)
    with pytest.raises(UnsafeURL):
        api.read_url("http://127.0.0.1:8000/admin")
    assert http.requests == []


def test_a_non_search_response_shape_is_an_error_not_a_crash():
    http = FakeHTTP(b"<html>not json</html>", content_type="text/html")
    api = HttpSearchAPI(api_key="k", opener=http)
    with pytest.raises(SearchError):
        api.search("widgets")


def test_page_to_dict_is_json_serialisable():
    page = Page(url="https://x.example.com", text="hi", observed_at="2026-09-24T00:00:00+00:00")
    assert json.loads(json.dumps(page.to_dict()))["url"] == "https://x.example.com"
