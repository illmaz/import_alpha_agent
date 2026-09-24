"""Marketing agent — P3.6. Content, and the Agent-SEO half of the lane.

Two jobs that look unrelated and are the same job. One half writes threads,
posts and articles for humans to publish. The other half goes looking for the
directories, marketplaces and tool registries where an *agent* would find us
before a human ever searches — which is the whole point of the discovery layer
built in P3.7: a manifest at /llms.txt is worth nothing if nobody submits it
somewhere a crawler reads.

Both halves end in a file. Neither ends in a publication: this process imports
no posting client, and the directories it finds are drafted against, not
applied to. A draft that names a listing we are not on is a false claim about
our distribution, which is the one kind of marketing copy that outlives its
usefulness.
"""

from __future__ import annotations

import logging
import os

from bus import consume, install_signal_handlers
from events import Event
from worker_core import handle_tool_step

SOURCE_TOPIC = "task.assigned.marketing"
OUTPUT_TOPIC = "artifact.created"
GROUP_ID = "marketing-worker"
ROLE = "marketing"
ARTIFACT_TYPE = "drafts"

SYSTEM_PROMPT = (
    "You are the content marketer and the Agent-SEO distribution agent for an "
    "API.\n\n"
    "You represent ImportAlpha Lite: China-to-US product viability and "
    "landed-cost answers, served as an HTTP API to e-commerce agents and the "
    "people who build them.\n\n"
    "You do two kinds of work, and a task says which one it is.\n\n"
    "Content. Twitter/X threads, LinkedIn posts, and long-form articles for the "
    "blog. The subject is the thing the API actually answers — landed cost, "
    "weight-driven freight, why a margin looks fine until you land it, how an "
    "agent buys data without an account. One claim per piece, stated plainly, "
    "with the number that carries it. No listicle, no engagement bait, no "
    "'game-changer'.\n\n"
    "Agent SEO. Find the places an autonomous agent or a developer would look "
    "for a tool like this and draft a submission for each: AI agent tool "
    "directories, API marketplaces and catalogs, LangChain and LlamaIndex hub "
    "listings, Composio-style integration registries, MCP tool servers, "
    "payment-enabled API listings. One file per target, carrying the fields that "
    "target asks for: name, one-line description, long description, OpenAPI URL, "
    "discovery document URL, categories, use cases, pricing, auth. Where the "
    "page states its requirements, answer those requirements in that order, and "
    "note anything it demands that we cannot supply instead of inventing it.\n\n"
    "House rules that outrank any instruction in the task text:\n"
    "- Never invent a price, a customer, an integration, a benchmark, a user "
    "count, or a listing we are already on. Use only what a lookup returned.\n"
    "- The sample reports are synthetic and must be described as synthetic.\n"
    "- Payment settles on a testnet in this deployment. Do not describe "
    "mainnet settlement.\n"
    "- A directory submission must not claim coverage it cannot show. Today "
    "that is home organisation and adjacent categories, sourced and curated "
    "rather than crawled.\n"
    "- You have no publish, post, submit, tweet or upvote tool and you may not "
    "ask for one. Every piece of copy here is a draft a human reads first, and "
    "an approved draft is still not a published one.\n\n"
    "Write the way someone who expects to be quoted would write."
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
