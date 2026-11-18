# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The async queue message contract: the keys a job carries BESIDE its payload.

ONE DEFINITION, imported by every producer (submit_api, relay_server's promotion, sweeper) and by
the consumer (worker). Added 2026-09-22 after the wire-proof review for the premium relay found the
same field spelled two ways and dropped a third way:

  - submit_api wrote "capability"; relay_server's promotion wrote "capability_class"; the worker
    read only "capability". So the one path that actually PRODUCES a premium capability on the
    queue (promotion) lost it, and a promoted interactive+premium job would have been served by the
    standard pool: defect 3, still open on the path it was meant to close.
  - the sweeper re-enqueued only client_id and payload, so an orphaned job lost BOTH its capability
    and its arrival route (the phase3b defect-2 fix, which made the route travel, never reached the
    sweeper), because the claim record stored nothing else to restore.

ROUTING SIBLINGS decide WHERE a job is served. They are siblings of payload, never inside it, so
job_key(client_id, payload) and therefore exactly-once are unchanged. Every producer writes them,
the claim persists them, the sweeper restores them, the worker reads them. Nothing else spells them.
"""

PATH = "path"                     # the route the request arrived on (/v1/completions, /v1/chat/completions)
CAPABILITY = "capability_class"   # economy | standard | premium; absent means standard at the gateway
SLA = "sla_class"                 # recorded by promotion; the worker's lane is its own INFERENCE_OBJECTIVE

ROUTING_SIBLINGS = (PATH, CAPABILITY)


def routing_of(body: dict) -> dict:
    """The routing siblings present on a message body, and only those."""
    return {k: body[k] for k in ROUTING_SIBLINGS if body.get(k)}
