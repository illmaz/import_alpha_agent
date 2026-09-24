"""Pluggable web lookup for the outreach and marketing workers.

Two capabilities and nothing else: `search_web` (a query in, ranked results
out) and `read_url` (a URL in, page text out). Both are *read* operations.
There is no tool here that sends anything to anyone — a worker cannot publish,
post or email because no such function exists in this module, not because a
prompt forbids it. See docs/DECISIONS.md.

`FakeSearchAPI` is the default. It returns a small, obviously-synthetic corpus
so the reasoning logic is testable offline and a dogfood run needs no key and
makes no network call. Every fixture result carries a `.example.com` host
(RFC 2606, reserved for exactly this) and a `confidence` of 0.25, so nothing
in the corpus can be mistaken for an observation about a real company. A
worker drafting from it is drafting about fictional targets, which is the
honest state of an offline run.

The real backend is opt-in via SEARCH_PROVIDER and needs a key. It is thin and
uses urllib rather than a new dependency: this module's job is fetching text,
not owning an HTTP client.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("search")

# Refused before any request is made. A lookup tool that can read `file://` or
# reach the metadata endpoint of a cloud host is a read primitive pointed at
# our own infrastructure, and the text it returns is fed straight to a model
# that is being asked to write messages.
BLOCKED_SCHEMES = frozenset({"file", "ftp", "gopher"})
BLOCKED_HOSTS = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "wpad"}
)
# 169.254.169.254 is the AWS/GCP/OpenStack IMDS address, and the private
# ranges are where the compose network and the SQLite file live.
_BLOCKED_NET_PREFIXES = ("127.", "10.", "172.16.", "192.168.", "169.254.", "::1")

MAX_PAGE_CHARS = 20_000
MAX_RESULTS = 10


class SearchError(RuntimeError):
    """A lookup could not be performed. Carried back to the model as text."""


class UnsafeURL(SearchError):
    """A URL was refused by policy rather than failing to fetch."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SearchResult:
    """One ranked hit. Provenance fields are never optional.

    AGENTS.md requires source_url and observed_at on every datapoint, so the
    type makes them mandatory rather than leaving it to the caller. A result
    without a confidence is a result nobody should act on.
    """

    title: str
    url: str
    snippet: str
    source_url: str
    observed_at: str
    confidence: float
    # Why the corpus holds this row: "fixture" for the canned corpus, the
    # provider name for a live lookup. Keeps a synthetic result from being
    # quoted as though someone had observed it.
    data_class: str = "fixture"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Page:
    url: str
    text: str
    observed_at: str
    truncated: bool = False
    data_class: str = "fixture"
    title: str = ""
    # The contact route the page itself publishes, if any. Separate from `text`
    # because a draft's recipient must be traceable to a field rather than to a
    # substring a model happened to notice, and because "no contact published"
    # has to be representable without inventing one.
    contact: str = ""
    # What a directory says it needs from a submission, when the page is one.
    # Carried separately from `text` so a manifest can be built against named
    # requirements rather than guessed from prose.
    requires: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_RESERVED_HOSTS = ("example.com", "example.net", "example.org", "example.edu")


def _is_reserved(host: str) -> bool:
    """RFC 2606 reserves the domain *and everything below it*.

    An exact-match test on "example.com" would let every fixture URL in this
    repo — they all live on subdomains such as agenttoolbox.example.com — pass
    as a fetch target, which is the opposite of why the rule exists: a live
    provider's placeholder row must never be mistaken for observed data.
    """
    return (
        host in _RESERVED_HOSTS
        or any(host.endswith("." + reserved) for reserved in _RESERVED_HOSTS)
        or host.endswith(".example")
    )


def validate_url(url: str, allow_reserved: bool = False) -> str:
    """Return the url if it is safe to touch, else raise UnsafeURL.

    `allow_reserved` is for a URL that is only ever *named*, never fetched. The
    RFC 2606 hosts carry the fixture corpus, and refusing them as fetch targets
    is what stops a live provider's placeholder rows from being mistaken for
    data — but the same rule would make the offline dogfood unable to state a
    recipient, which is a label and not a request.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"refused non-http(s) scheme: {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURL(f"refused url with no host: {url!r}")
    if host in BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeURL(f"refused internal host: {url!r}")
    if host.startswith(_BLOCKED_NET_PREFIXES):
        raise UnsafeURL(f"refused private or link-local address: {url!r}")
    if not allow_reserved and _is_reserved(host):
        raise UnsafeURL(f"refused reserved example host: {url!r}")
    return url


def validate_target_url(url: str) -> str:
    """A draft's destination. Same guards, minus the reserved-host rule.

    Nothing fetches this string today: a directory recipient tells a human where
    to paste a manifest. A backend that ever POSTs to a recipient must call
    validate_url on it without this flag, and that is the line a future
    implementer should not be able to cross by accident.
    """
    return validate_url(url, allow_reserved=True)


class SearchAPI:
    """The interface a worker holds. Read-only by construction."""

    name = "base"

    def search(self, query: str, max_results: int = MAX_RESULTS) -> List[SearchResult]:
        raise NotImplementedError

    def read_url(self, url: str) -> Page:
        raise NotImplementedError


# --------------------------------------------------------------------------
# The offline corpus
# --------------------------------------------------------------------------
# Fictional companies and directories on reserved hosts. The point of naming
# them at all is that a worker has something concrete to reason over; the
# point of the hosts is that nothing here corresponds to a real person who
# could be emailed. Each entry's `context` is the body a read_url returns.
_FIXTURE_TARGETS: List[dict] = [
    {
        "name": "Cartwave AI",
        "url": "https://cartwave.example.com/agents",
        "blurb": "Builds Shopify restock bots that answer customer questions about inventory.",
        "tags": ["ecommerce agent builders", "shopify", "seller bot"],
        "contact": "team@cartwave.example.com",
        "context": (
            "Cartwave AI is a three-person team shipping Shopify restock agents. "
            "Their public docs describe an agent that answers 'should I reorder this' "
            "against the store's own sales history, with no external product data. "
            "Roadmap mentions landed-cost awareness as a future item."
        ),
    },
    {
        "name": "Tikrank Copilot",
        "url": "https://tikrank.example.com/blog",
        "blurb": "TikTok Shop selling copilot; picks winning products from trending audio.",
        "tags": ["ecommerce agent builders", "tiktok shop", "product research"],
        "contact": "hello@tikrank.example.com",
        "context": (
            "Tikrank Copilot surfaces trending TikTok Shop products and estimates "
            "saturation. Their write-up of the scoring model says supply-side cost is "
            "estimated from public listing prices, not from a sourcing feed."
        ),
    },
    {
        "name": "Binford Sourcing",
        "url": "https://binford.example.com",
        "blurb": "Procurement agent for home goods; asks suppliers for quotes by email.",
        "tags": ["procurement agent", "sourcing", "home organization"],
        "contact": "ops@binford.example.com",
        "context": (
            "Binford automates RFQs for home goods buyers in the US. The founder "
            "writes that most time goes to checking whether a quoted unit price "
            "survives freight and duty, which they currently do in a spreadsheet."
        ),
    },
    {
        "name": "Ledgerlight",
        "url": "https://ledgerlight.example.com",
        "blurb": "Indie-built margin calculator; author posts build-in-public updates.",
        "tags": ["indie hackers", "margin calculator", "dropshipping tools"],
        "contact": "builder@ledgerlight.example.com",
        "context": (
            "Ledgerlight is a solo project. The author's last update asks whether "
            "anyone knows an API for China unit-cost ranges, because guessing "
            "freight is the biggest source of wrong margin numbers in the tool."
        ),
    },
    {
        "name": "Dropship Deep Six",
        "url": "https://dropshipdeepsix.example.com/forum",
        "blurb": "Community forum for dropshippers; recurring thread on shipping weight traps.",
        "tags": ["dropshipper communities", "forum", "shipping weight"],
        "contact": "mods@dropshipdeepsix.example.com",
        "context": (
            "A long-running forum thread on products that look profitable and are "
            "not, because volumetric weight doubles freight. Regulars ask for a way "
            "to check a candidate before ordering a sample."
        ),
    },
]

_FIXTURE_DIRECTORIES: List[dict] = [
    {
        "name": "AgentToolbox Directory",
        "url": "https://agenttoolbox.example.com/submit",
        "blurb": "Curated list of tools callable by LLM agents; accepts submissions.",
        "tags": ["ai agent tool directories", "agent directory"],
        "requires": "name, one-line description, OpenAPI URL, pricing page",
    },
    {
        "name": "ToolBelt Registry",
        "url": "https://toolbelt.example.com/register",
        "blurb": "Registry of agent functions with schema validation on submit.",
        "tags": ["api marketplaces", "tool registry", "function calling"],
        "requires": "OpenAPI document, auth scheme, machine-readable pricing",
    },
    {
        "name": "ChainHub Integrations",
        "url": "https://chainhub.example.com/integrations",
        "blurb": "Community hub for chain and framework integrations.",
        "tags": ["langchain hubs", "framework integration", "directory"],
        "requires": "package or endpoint, usage example, license, docs URL",
    },
    {
        "name": "ComposeMarket",
        "url": "https://composemarket.example.com/listings",
        "blurb": "Marketplace for agent toolkits; bills per call.",
        "tags": ["composio registries", "marketplace", "per-call billing"],
        "requires": "per-call price, settlement method, latency, sample response",
    },
    {
        "name": "MCP Index",
        "url": "https://mcpindex.example.com/add",
        "blurb": "Index of model-context servers and remote APIs.",
        "tags": ["mcp directory", "api marketplace", "agent tool directories"],
        "requires": "transport, auth, tool list, discovery document",
    },
    {
        "name": "PromptStack Tools",
        "url": "https://promptstack.example.com/tools",
        "blurb": "Directory of paid APIs for agent stacks.",
        "tags": ["api marketplaces", "paid api directory"],
        "requires": "pricing, rate limits, SLA, OpenAPI URL",
    },
    {
        "name": "Autobay Agents",
        "url": "https://autobay.example.com/agents",
        "blurb": "Marketplace where agents hire agents; requires a machine-readable offer.",
        "tags": ["agent marketplace", "machine readable", "discovery"],
        "requires": "llms.txt or equivalent, payment terms, accepted networks",
    },
    {
        "name": "SellerStack Connectors",
        "url": "https://sellerstack.example.com/connectors",
        "blurb": "Connector catalogue for e-commerce automation platforms.",
        "tags": ["ecommerce integrations", "connector directory"],
        "requires": "category, supported endpoints, auth, data freshness",
    },
    {
        "name": "Retriever List",
        "url": "https://retrieverlist.example.com",
        "blurb": "Newsletter-run list of data APIs; editorial review before listing.",
        "tags": ["data api directory", "api marketplaces"],
        "requires": "what the data is, where it comes from, how current it is",
    },
    {
        "name": "OpenAgents Catalog",
        "url": "https://openagents.example.com/catalog",
        "blurb": "Open-source catalog of agent tools; submissions via pull request.",
        "tags": ["open source agent directory", "registry"],
        "requires": "licence, source or spec, self-hostability, cost",
    },
    {
        "name": "SignalWire API Wall",
        "url": "https://signalwire-apiwall.example.com",
        "blurb": "Aggregator of pay-per-call APIs for agent builders.",
        "tags": ["api marketplace", "pay per call"],
        "requires": "price per call, metering model, sandbox availability",
    },
    {
        "name": "Devpost Agent Track",
        "url": "https://agenttrack.example.com/directory",
        "blurb": "Directory seeded from hackathons; lists APIs teams actually used.",
        "tags": ["developer directory", "agent tools"],
        "requires": "getting-started guide, free tier, docs quality",
    },
]

_MATCH_RE = re.compile(r"[a-z0-9]+")

# A query containing any of these is asking where to be listed, not whom to
# write to. Used only to choose which fixture pool answers; a live provider does
# not consult it, because a real engine already ranked the intent.
#
# Deliberately narrow. The first version of this set included "agent", "tool"
# and "api", which are in both kinds of query — "e-commerce agent builders" is
# prospecting — so every outreach search was routed to the directory pool and
# the offline run drafted five letters addressed to listing pages. A routing
# word has to discriminate, not merely appear.
_DIRECTORY_WORDS = frozenset(
    {
        "directory",
        "directories",
        "registry",
        "registries",
        "marketplace",
        "marketplaces",
        "hub",
        "hubs",
        "index",
        "catalog",
        "catalogue",
        "listing",
        "listings",
        "submit",
        "submission",
        "submitting",
        "langchain",
        "llamaindex",
        "composio",
        "mcp",
        "openapi",
        "distribution",
        "seo",
    }
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_CONTACT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Asset filenames reach this regex too — `sprite@2x.png` is a real convention
# and is not a way to reach a person.
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".woff", ".woff2")


def _first_contact(raw: str) -> str:
    """The first plausible address in a page body, or '' when none is published.

    Empty is the honest answer and it is load-bearing: a draft with no address
    is aimed at the page and labelled unverified, while a draft with a guessed
    address is a send that either bounces or reaches a stranger.
    """
    for match in _CONTACT_RE.finditer(raw or ""):
        candidate = match.group(0).rstrip(".")
        # Case-folded only for this test. The returned value keeps the page's
        # own casing, because the local part of an address is case-bearing.
        if candidate.lower().endswith(_ASSET_SUFFIXES) or len(candidate) > 80:
            continue
        return candidate
    return ""


def normalise_contact(raw: str) -> str:
    """An address in the shape a mail backend accepts, or '' if it is not one.

    Scraped and fixture addresses arrive in the forms pages publish them in:
    `mailto:` prefixes, a trailing comma from prose, an all-caps domain. The
    domain is case-free, the local part is not, so only half of it is folded.
    """
    text = (raw or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:") :].strip()
    if not _CONTACT_RE.fullmatch(text):
        # A page said "Write to ops@co.dev." rather than publishing the address
        # on its own line. Take the address out of the sentence instead of
        # passing the sentence on as a recipient.
        found = _CONTACT_RE.search(text)
        if not found:
            return ""
        text = found.group(0)
    text = text.rstrip(".,;:")
    local, sep, domain = text.partition("@")
    if not sep or not local or "." not in domain:
        return ""
    return f"{local}@{domain.lower()}"


def _tokens(text: str) -> List[str]:
    return _MATCH_RE.findall((text or "").lower())


def _score(query: str, entry: dict) -> int:
    """Token overlap against name, blurb and tags. Deliberately crude."""
    haystack = " ".join(
        [entry.get("name", ""), entry.get("blurb", ""), " ".join(entry.get("tags", []))]
    ).lower()
    return sum(1 for token in set(_tokens(query)) if token in haystack)


class FakeSearchAPI(SearchAPI):
    """Canned, deterministic, offline. The default provider.

    Two pools, because one corpus answers both kinds of question badly: an
    outreach query that returns tool directories produces letters addressed to
    listing pages, and a distribution query that returns indie hackers produces
    manifests addressed to people. The pool is chosen from the query's own
    vocabulary, so the worker's phrasing decides and no caller has to pass a
    flag the model could not have thought of.

    `targets` lets a test replace the prospect pool, and `fail_with` makes an
    error path reachable without a network.
    """

    name = "fake"

    def __init__(
        self,
        targets: Optional[List[dict]] = None,
        extra_results: Optional[List[SearchResult]] = None,
        fail_with: Optional[str] = None,
    ) -> None:
        self._prospects = list(targets) if targets is not None else list(_FIXTURE_TARGETS)
        # An injected prospect list is also an injected directory list: a test
        # that names its candidates should not find the fixture corpus leaking
        # in through the other pool, which is what made `_pool`'s fallback
        # (return everything when no token matches) silently answer from data
        # the test never asked for.
        self._directories = (
            list(_FIXTURE_DIRECTORIES)
            if targets is None
            else [row for row in targets if row.get("requires")]
        )
        self._extra = list(extra_results or [])
        self._fail_with = fail_with
        self.calls: List[str] = []

    @property
    def _rows(self) -> List[dict]:
        return [*self._prospects, *self._directories]

    def _pool(self, query: str) -> List[dict]:
        words = set(_tokens(query))
        if words & _DIRECTORY_WORDS:
            return self._directories or self._rows
        return self._prospects or self._rows

    def search(self, query: str, max_results: int = MAX_RESULTS) -> List[SearchResult]:
        self.calls.append(query)
        if self._fail_with:
            raise SearchError(self._fail_with)

        rows = self._pool(query)
        scored = [(_score(query, row), row) for row in rows]
        scored = [(points, row) for points, row in scored if points > 0]
        # Stable sort on (-score, url) so the same query always yields the
        # same order. A dict-order dependency would make the offline dogfood
        # run non-reproducible, which is the whole reason it exists.
        scored.sort(key=lambda pair: (-pair[0], pair[1]["url"]))

        results = [
            SearchResult(
                title=row["name"],
                url=row["url"],
                snippet=row["blurb"],
                source_url=row["url"],
                observed_at=_utc_now_iso(),
                # Fixed and low on purpose: it is a fixture, not a measurement.
                confidence=0.25,
            )
            for _, row in scored[: max(1, min(int(max_results), MAX_RESULTS))]
        ]
        return results + self._extra

    def read_url(self, url: str) -> Page:
        self.calls.append(url)
        if self._fail_with:
            raise SearchError(self._fail_with)
        for row in self._rows:
            if row["url"] == url:
                text = row.get("context") or row["blurb"]
                return Page(
                    url=url,
                    title=row["name"],
                    text=f"{row['name']} — {row['blurb']}\n\n{text}",
                    observed_at=_utc_now_iso(),
                    # Without this the only contact route a draft can use is the
                    # page it came from, and an "outreach message" addressed to a
                    # website is not outreach.
                    contact=normalise_contact(row.get("contact", "")),
                    requires=row.get("requires", ""),
                )
        raise SearchError(f"no fixture page for {url!r} (FakeSearchAPI is offline)")


class HttpSearchAPI(SearchAPI):
    """Live backend for SEARCH_PROVIDER=tavily.

    Thin on purpose: it maps a provider response onto SearchResult and nothing
    else. `opener` is injectable so the parsing is testable without a socket,
    which is the only reason it exists as a parameter.
    """

    name = "http"

    def __init__(self, api_key: str, model: str = "tavily", opener=None) -> None:
        if not (api_key or "").strip():
            raise SearchError("SEARCH_API_KEY is required for a live search provider")
        self._api_key = api_key
        self._model = model
        self._opener = opener or urllib.request.urlopen

    def search(self, query: str, max_results: int = MAX_RESULTS) -> List[SearchResult]:
        payload = json.dumps(
            {"api_key": self._api_key, "query": query, "max_results": max_results, "search_depth": "basic"}
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "ImportAlphaBot/0.1"},
        )
        body = self._fetch(request, "search")
        rows = body.get("results") or []
        observed = _utc_now_iso()
        results = []
        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url:
                # A row with no address cannot be cited, and the result type
                # requires source_url. Keep provenance empty rather than a
                # citable-looking row that resolves nowhere.
                continue
            results.append(
                SearchResult(
                    title=str(row.get("title") or ""),
                    url=url,
                    snippet=str(row.get("content") or "")[:500],
                    source_url=url,
                    observed_at=observed,
                    confidence=0.5,
                    data_class="search_provider",
                )
            )
        return results[: max(1, min(int(max_results), MAX_RESULTS))]

    def read_url(self, url: str) -> Page:
        validate_url(url)
        request = urllib.request.Request(
            url, headers={"User-Agent": "ImportAlphaBot/0.1", "Accept": "text/plain, text/html"}
        )
        raw = self._fetch(request, "read", binary=True)
        decoded = raw.decode("utf-8", "replace")
        text = _html_to_text(decoded)
        truncated = len(text) > MAX_PAGE_CHARS
        title_match = _TITLE_RE.search(decoded)
        return Page(
            url=url,
            title=title_match.group(1).strip() if title_match else "",
            # Scraped out of the page rather than asked of the model. An address
            # a model inferred from prose is a guess with an @ in it, and the
            # whole point of the field is that a recipient is traceable.
            #
            # The raw markup is searched before the stripped text: a contact
            # link is `<a href="mailto:hi@x.com">email</a>`, and by the time the
            # tags are gone the address is gone with them — which is how a first
            # version came up empty on every page that published one properly.
            contact=normalise_contact(_first_contact(decoded) or _first_contact(text)),
            text=text[:MAX_PAGE_CHARS],
            observed_at=_utc_now_iso(),
            truncated=truncated,
            data_class="web",
        )

    def _fetch(self, request, what: str, binary: bool = False):
        try:
            with self._opener(request, timeout=20) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            raise SearchError(f"{what} failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SearchError(f"{what} failed: {exc}") from exc
        if binary:
            return data
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise SearchError(f"{what} returned a body that is not JSON") from exc


_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]{2,}")


def _html_to_text(html: str) -> str:
    """Strip markup. Not a parser, and not pretending to be one."""
    text = _TAG_RE.sub(" ", html or "")
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    )
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def get_search() -> SearchAPI:
    """Build the provider named by SEARCH_PROVIDER. Defaults to fake.

    The default is the offline one for the same reason FakeLLM is the default
    model: a fresh clone and the whole test suite must work with no key and no
    egress. Real lookup is opt-in, and a typo in the provider name is an error
    rather than a silent fallback to the network.
    """
    provider = os.environ.get("SEARCH_PROVIDER", "fake").strip().lower()
    if provider in ("", "fake"):
        return FakeSearchAPI()
    if provider in ("http", "tavily"):
        return HttpSearchAPI(os.environ.get("SEARCH_API_KEY", ""), model=provider)
    raise ValueError(f"unknown SEARCH_PROVIDER: {provider!r} (fake, tavily)")
