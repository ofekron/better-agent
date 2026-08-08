"""Outbound-only A2A (Agent2Agent protocol) client subsystem.

No inbound/listening network surface is added anywhere in this package —
Better Agent only ever calls OUT to remote A2A agents that the user
registers. See `a2a.client` for the hand-rolled JSON-RPC 2.0 + SSE
client, `a2a.discovery` for agent-card fetch/validation, `a2a.url_policy`
for the outbound URL allowlist, and `a2a.delegation` for the worker-panel
projection that funnels remote task updates through
`OrchestrationStrategy.apply_event`.
"""
from __future__ import annotations
