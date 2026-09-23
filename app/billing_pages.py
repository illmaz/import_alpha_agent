"""Browser landing pages for Stripe Checkout.

Unauthenticated on purpose: Stripe redirects the customer's browser here after
payment, and a browser carries no API key. They display nothing sensitive —
just an outcome and what to do next.

The success page deliberately does **not** claim the credits have landed.
Crediting happens on the webhook, which is a separate request that may arrive
a moment after the redirect; telling someone their balance is updated before
it is would be the one lie this page could tell.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["billing-pages"])

# Where Stripe should send the browser back to. Override when the API is not
# on localhost (a tunnel, a deployed host).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

SUCCESS_PATH = "/billing/success"
CANCEL_PATH = "/billing/cancel"


def _page(title: str, headline: str, body: str, accent: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — ImportAlpha Lite</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #f6f7f9; color: #1a1d21; padding: 24px;
  }}
  .card {{
    background: #fff; border-radius: 14px; padding: 40px;
    max-width: 30rem; box-shadow: 0 1px 3px rgba(0,0,0,.1), 0 8px 28px rgba(0,0,0,.06);
    border-top: 4px solid {accent};
  }}
  h1 {{ margin: 0 0 12px; font-size: 1.5rem; }}
  p {{ margin: 0 0 14px; color: #414852; }}
  code {{
    background: #eef0f3; padding: 2px 6px; border-radius: 5px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  .muted {{ font-size: .875rem; color: #6b7280; margin-top: 22px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #15181c; color: #e8eaed; }}
    .card {{ background: #1e2227; box-shadow: none; }}
    p {{ color: #b6bcc5; }}
    code {{ background: #2a2f36; }}
    .muted {{ color: #8b929c; }}
  }}
</style>
</head>
<body>
  <main class="card">
    <h1>{headline}</h1>
    {body}
  </main>
</body>
</html>"""


@router.get(SUCCESS_PATH, response_class=HTMLResponse, summary="Post-payment landing page")
async def billing_success(session_id: str | None = None) -> HTMLResponse:
    """Shown after a completed Checkout Session."""
    reference = (
        f"<p>Session <code>{session_id}</code>.</p>" if session_id else ""
    )
    return HTMLResponse(
        _page(
            "Payment received",
            "Payment received",
            f"""
    <p>Thank you. Your credits are being added now — this happens when Stripe
       notifies us, usually within a second or two.</p>
    {reference}
    <p>Check your balance:</p>
    <p><code>curl -H "Authorization: Bearer &lt;your key&gt;" \\<br>
       &nbsp;&nbsp;{PUBLIC_BASE_URL}/v1/account/transactions</code></p>
    <p class="muted">If the balance has not moved after a few seconds, the
       purchase is still recorded against the payment event and can be
       replayed — nothing is lost.</p>
""",
            "#16a34a",
        )
    )


@router.get(CANCEL_PATH, response_class=HTMLResponse, summary="Cancelled-payment landing page")
async def billing_cancel() -> HTMLResponse:
    """Shown when the customer backs out of Checkout."""
    return HTMLResponse(
        _page(
            "Payment cancelled",
            "Payment cancelled",
            """
    <p>No charge was made and your balance is unchanged.</p>
    <p>You can start a new checkout whenever you are ready:</p>
    <p><code>POST /v1/billing/checkout</code> with <code>{"pack_id": "report_pack"}</code></p>
    <p class="muted">Available packs: <code>GET /v1/billing/packs</code></p>
""",
            "#d97706",
        )
    )
