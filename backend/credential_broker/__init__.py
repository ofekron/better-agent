"""Secure credential broker.

A provider (MCP tool) proposes an operation TEMPLATE that needs a secret
(an ``{{secret}}`` placeholder somewhere in it) plus a sink/target. The
user enters the secret value once and approves the *operation*. From then
on the broker — and only the broker — holds the secret value; it executes
the frozen operation itself and returns a guarded result. Claude only ever
holds a ``consent_id`` handle, never the value.

Security guarantees this package enforces (see CLAUDE.md "Security is the
top priority"):

  * non-exposure   — the value lives only encrypted at rest + transiently
                     in broker memory; it never enters the event pipeline,
                     WS frames, REST responses, logs, traces, or Claude's
                     context.
  * consent-integrity — the broker stores its OWN copy of the descriptor
                     under a consent_id; callers only ever pass the
                     consent_id, never re-supply the descriptor, so the
                     executed operation is exactly the approved one.
  * confinement    — the destination is pinned to the approved descriptor;
                     a provider's request is rejected unless its computed
                     sink is on the provider's allowed-sinks pin.

This package is the pure, testable core. Process isolation (a hardened,
code-signed daemon), user-presence gating, and the MCP/REST/WS surface
layer on top of it.
"""
