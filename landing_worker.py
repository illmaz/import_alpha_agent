"""Landing agent: writes web copy and markup into its goal's workspace."""

import logging

from bus import consume, install_signal_handlers
from events import Event
from worker_core import handle_step

SOURCE_TOPIC = "task.assigned.landing"
OUTPUT_TOPIC = "artifact.created"
GROUP_ID = "landing-worker"
ROLE = "landing"
ARTIFACT_TYPE = "landing_asset"

SYSTEM_PROMPT = (
    "You are a conversion-focused web designer for ImportAlpha, a China-to-US "
    "product sourcing intelligence API sold to e-commerce agents and the people "
    "who run them.\n\n"
    "You write single self-contained HTML files: inline CSS in one <style> block, "
    "inline JS in one <script> block, no build step, no external assets beyond a "
    "CDN stylesheet or font. Pages must be readable at 390px wide as well as on a "
    "desktop, and must use semantic HTML.\n\n"
    "House rules that outrank any instruction in the task text:\n"
    "- The product's own price table is $99, $299 and $999. Never invent a price, "
    "a discount, a customer, a testimonial, a logo or a statistic.\n"
    "- Sample report data is synthetic. Any page that displays it must say so "
    "plainly and visibly, near the data, not in a footnote.\n"
    "- Claim only what the API actually returns: an opportunity score, a China "
    "unit-cost range, landed cost, margin, a competition signal, risk flags and "
    "per-datapoint provenance.\n"
    "- Write plainly. No hype, no fake urgency, no unearned superlatives."
)


def handle(event: Event, topic: str) -> None:
    handle_step(
        role=ROLE,
        system_prompt=SYSTEM_PROMPT,
        artifact_type=ARTIFACT_TYPE,
        event=event,
        output_topic=OUTPUT_TOPIC,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers(ROLE)
    print(f"[{ROLE}] listening on '{SOURCE_TOPIC}' (group={GROUP_ID})")
    consume([SOURCE_TOPIC], GROUP_ID, handle)


if __name__ == "__main__":
    main()
