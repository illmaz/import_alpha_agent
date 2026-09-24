"""P3.7: the discovery layer an autonomous buyer arrives at.

Two things are load-bearing here beyond "it returns 200". Every one of these
must work with no credentials, because an agent with no account is the whole
point; and nothing may advertise a payment route the code will refuse, because
the cost of that mistake is someone's funds.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.v1.agent_endpoints import AGENT_GUIDE, LLMS_TXT
from app.main import app
from app.services import x402
from app.services.x402_middleware import CREDIT_PRICE_BASE_UNITS, PAYMENT_HEADER

client = TestClient(app)

DISCOVERY_PATHS = ["/llms.txt", "/agent-guide", "/openapi.json", "/v1/agent/info"]


# ---------- public, every one of them ----------


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
def test_discovery_needs_no_credentials(path: str) -> None:
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
def test_discovery_answers_head_probes(path: str) -> None:
    """Crawlers check existence with HEAD before fetching.

    The StaticFiles mount at "/" answers 404 for any method the API routes
    decline, so a discovery document that does not declare HEAD tells a
    probing agent it does not exist — and discovery stops there.
    """
    assert client.head(path).status_code == 200


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
def test_discovery_does_not_leak_a_payment_challenge(path: str) -> None:
    """Discovery must never 402. An agent cannot pay to learn how to pay."""
    assert client.get(path).status_code != 402


# ---------- /llms.txt ----------


def test_llms_txt_is_served_as_plain_text() -> None:
    response = client.get("/llms.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_llms_txt_follows_the_spec_shape() -> None:
    body = client.get("/llms.txt").text

    assert body.startswith("# "), "llms.txt opens with an H1 title"
    assert "\n> " in body, "llms.txt carries a blockquote summary"
    assert "## " in body, "llms.txt is organised into sections"


def test_llms_txt_points_at_the_live_payment_source() -> None:
    """A static file must not be the authority on a wallet address."""
    body = client.get("/llms.txt").text

    assert "/v1/agent/info" in body
    assert "/agent-guide" in body
    assert "/openapi.json" in body


def test_llms_txt_warns_that_data_is_curated() -> None:
    body = client.get("/llms.txt").text.lower()

    assert "curated" in body


# ---------- /agent-guide ----------


def test_agent_guide_is_served_as_markdown_not_html() -> None:
    response = client.get("/agent-guide")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "<html" not in response.text.lower()


def test_agent_guide_documents_both_auth_methods() -> None:
    body = client.get("/agent-guide").text

    assert "Authorization: Bearer" in body
    assert PAYMENT_HEADER in body


def test_agent_guide_links_testnet_faucets() -> None:
    body = client.get("/agent-guide").text.lower()

    assert "faucet" in body
    assert "base-sepolia" in body or "base sepolia" in body


def test_agent_guide_has_worked_curl_examples() -> None:
    body = client.get("/agent-guide").text

    assert body.count("curl") >= 4


# ---------- /openapi.json ----------


def test_openapi_is_public_and_valid() -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"]
    assert spec["paths"]


def test_openapi_describes_the_paying_endpoints() -> None:
    spec = client.get("/openapi.json").json()

    for path in ("/v1/reports", "/v1/opportunities", "/v1/agent/info"):
        assert path in spec["paths"], path


# ---------- /v1/agent/info ----------


def test_agent_info_reports_price_and_header() -> None:
    payment = client.get("/v1/agent/info").json()["payment"]

    assert payment["amount_base_units"] == CREDIT_PRICE_BASE_UNITS
    assert payment["decimals"] == x402.USDC_DECIMALS
    assert payment["header"] == PAYMENT_HEADER
    assert payment["credits_granted"] == 1


def test_agent_info_price_matches_the_402_challenge() -> None:
    """Discovery and rejection must quote the same number.

    An agent that reads one and is billed by the other has no way to reconcile
    a mismatch, so this pins them together.
    """
    info = client.get("/v1/agent/info").json()["payment"]
    challenge = client.post("/v1/reports", json={"category": "home_organization"})

    assert challenge.status_code in (401, 402)
    if challenge.status_code == 402:
        accepts = challenge.json()["accepts"][0]
        assert accepts["amount_base_units"] == info["amount_base_units"]
        assert accepts["header"] == info["header"]


def test_agent_info_lists_both_auth_methods() -> None:
    methods = {m["method"] for m in client.get("/v1/agent/info").json()["auth_methods"]}

    assert methods == {"api_key", "x402"}


def test_agent_info_marks_mainnet_as_not_accepted() -> None:
    """The money question.

    base-mainnet is a real network this code knows and refuses. Advertising it
    as supported would take real USDC for a call that then fails.
    """
    networks = {n["name"]: n for n in client.get("/v1/agent/info").json()["networks"]}

    assert networks["base-mainnet"]["accepted"] is False
    assert networks["base-mainnet"]["note"]
    assert "not accepted" in networks["base-mainnet"]["note"].lower()
    assert networks["base-sepolia"]["accepted"] is True


def test_every_accepted_network_is_a_testnet() -> None:
    """Whatever the registry grows to, an accepted mainnet is a bug."""
    for network in client.get("/v1/agent/info").json()["networks"]:
        if network["accepted"]:
            assert network["is_testnet"], network["name"]


def test_agent_info_advertises_only_networks_the_code_knows() -> None:
    names = {n["name"] for n in client.get("/v1/agent/info").json()["networks"]}

    assert names == set(x402.NETWORKS)


def test_agent_info_links_the_other_discovery_documents() -> None:
    docs = client.get("/v1/agent/info").json()["docs"]

    assert docs["llms_txt"] == "/llms.txt"
    assert docs["agent_guide"] == "/agent-guide"
    assert docs["openapi"] == "/openapi.json"


def test_agent_info_endpoint_list_matches_the_openapi_spec() -> None:
    """Guards the list from rotting as routes move."""
    spec_paths = set(client.get("/openapi.json").json()["paths"])
    listed = client.get("/v1/agent/info").json()["endpoints"]

    for entry in listed:
        path = entry["path"].replace("{report_id}", "{report_id}")
        assert path in spec_paths, f"{path} is advertised but not in the spec"


def test_agent_info_survives_an_unconfigured_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with no wallet must still answer, not 500.

    "Payment is unavailable here" is a useful answer for an agent; a stack
    trace is not.
    """
    def unconfigured() -> str:
        raise x402.X402NotConfigured("no wallet")

    monkeypatch.setattr("app.api.v1.agent_endpoints.x402.get_seller_address", unconfigured)
    response = client.get("/v1/agent/info")

    assert response.status_code == 200
    payment = response.json()["payment"]
    assert payment["pay_to"] is None
    assert "unavailable" in payment


# ---------- the files behind the routes ----------


def test_discovery_files_are_present_in_the_repository() -> None:
    assert LLMS_TXT.is_file()
    assert AGENT_GUIDE.is_file()


def test_missing_discovery_file_reports_503_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = LLMS_TXT.parent / "definitely-not-here.txt"
    monkeypatch.setattr("app.api.v1.agent_endpoints.LLMS_TXT", missing)

    assert client.get("/llms.txt").status_code == 503


# ---------- the static mount must not shadow any of this ----------


def test_static_mount_does_not_claim_the_discovery_paths() -> None:
    """The landing mount at "/" is registered last and matches everything.

    If these routers were ever included after it, /llms.txt would quietly
    become a 404 from StaticFiles.
    """
    assert client.get("/llms.txt").headers["content-type"].startswith("text/plain")
    assert client.get("/agent-guide").headers["content-type"].startswith("text/markdown")
    assert client.get("/").status_code == 200


def test_json_documents_are_actually_json() -> None:
    for path in ("/openapi.json", "/v1/agent/info"):
        json.loads(client.get(path).text)


# ---------- the human page points at the machine layer ----------


@pytest.mark.parametrize("target", ["/llms.txt", "/agent-guide", "/openapi.json"])
def test_landing_page_links_the_discovery_endpoints(target: str) -> None:
    """The dogfood result.

    A discovery layer nothing links to is one an agent has to already know
    about, which defeats the point.
    """
    assert target in client.get("/").text


def test_landing_page_has_a_for_agents_section() -> None:
    assert "for agents" in client.get("/").text.lower()
