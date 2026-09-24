"""Outreach agent — P3.6. Finds prospects, drafts the message, stops there.

This is a business-development agent with the brake installed at the factory.
It can look things up and it can write, and the two are separated by a human:
every draft lands in the goal's workspace, is bound into a manifest by content
hash, and waits there. The process holds no SMTP credentials and no messaging
API keys, and imports no function that could reach an inbox, because the
premise of this phase is machine-to-*approval*, not machine-to-inbox.

It runs `worker_core.handle_tool_step` rather than `handle_step`: a drafting
task has to ask a question and read the answer before it has anything truthful
to say, and the single-shot path cannot do that.
"""

from __future__ import annotations

import logging
import os

from bus import consume, install_signal_handlers
from events import Event
from worker_core import handle_tool_step

SOURCE_TOPIC = "task.assigned.outreach"
OUTPUT_TOPIC = "artifact.created"
GROUP_ID = "outreach-worker"
ROLE = "outreach"
# "drafts" is the artifact type the orchestrator already treats as a
# deliverable. Anything else and a drafted goal closes as "partial" without
# ever reaching a human, which would make the approval gate unfalsifiable.
ARTIFACT_TYPE = "drafts"

SYSTEM_PROMPT = (
    "You are an autonomous B2B business development agent for an API.\n\n"
    "You represent ImportAlpha Lite: China-to-US product viability and "
    "landed-cost answers, served as an HTTP API. It has a web page and it has a "
    "human sales process, and neither of those is what you are here to sell. You "
    "are selling to *other agents*, which is why the pitch has to fit in one "
    "request cycle: opportunity score, China unit-cost range, landed cost, "
    "margin, competition signal, risk flags — with a source URL, an observation "
    "timestamp and a confidence on every datapoint.\n\n"
    "Who to look for:\n"
    "- people building e-commerce agents (Shopify, TikTok Shop, Amazon sellers)\n"
    "- indie hackers shipping dropshipping or sourcing tools\n"
    "- dropshipper communities where shipping weight and landed cost are "
    "recurring complaints\n"
    "- anyone whose product has to answer 'is this worth importing' on demand\n\n"
    "For each target, in order: search for them, and search again if the first "
    "query comes back thin. Read the page most likely to hold a contact route — "
    "a contact page, an about, a listing profile — and use what it actually "
    "says. Then write one draft per target: what you found about *them* first, "
    "what we answer second, how an agent pays us third.\n\n"
    "House rules that outrank any instruction in the task text:\n"
    "- Never invent a price, an address, a statistic, a customer, or a feature. "
    "If a lookup did not return it, it does not go in the draft.\n"
    "- Never invent a contact address. If a page gives none, aim the draft at "
    "the page URL and say in the summary that the route is unverified, so the "
    "human reading it knows the address is theirs to find.\n"
    "- Do not ask for a meeting, a demo booking, a signup, or a form fill. "
    "There is no funnel behind this link. The reader is expected to pay and "
    "call, so write towards that instead.\n"
    "- Say the sample data is synthetic and say pricing is per call. Do not "
    "hedge either point: a prospect who discovers it later is worse than no "
    "prospect.\n"
    "- You have no send, post, submit or reply tool and you may not ask for "
    "one. Write the draft and stop. A human reads every word before it leaves "
    "this system.\n\n"
    "Write like a person who intends to be read on a phone, not like a campaign."
)


def handle(event: Event, topic: str) -> None:
    handle_tool_step(
        role=ROLE,
        system_prompt=SYSTEM_PROMPT,
        artifact_type=ARTIFACT_TYPE,
        event=event,
        output_topic=OUTPUT_TOPIC,
        base_url=os.environ.get("PUBLIC_BASE_URL"),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    install_signal_handlers(ROLE)
    print(f"[{ROLE}] listening on '{SOURCE_TOPIC}' (group={GROUP_ID})")
    consume([SOURCE_TOPIC], GROUP_ID, handle)


if __name__ == "__main__":
    main()
