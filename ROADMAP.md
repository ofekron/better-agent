# Public Roadmap

This roadmap is intentionally public and non-commercial. It does not include
private marketplace strategy, private customer plans, pricing, or proprietary
extension roadmaps.

## Before Public Source Availability

- Complete legal review of source-available license terms.
- Run full-history secret scanning with a dedicated scanner.
- Decide whether to rewrite public git history to remove prior private
  marketing drafts and old binary download artifacts.
- Enable protected branches, required reviews, CODEOWNERS approval, and private
  vulnerability reporting on the hosted repository.
- Run a clean dependency/license audit for frontend, backend, desktop, and
  extension SDK surfaces.

## Core Product

- Keep the core repo focused on local sessions, provider choice, live output,
  file context, project history, folders, tags, search, and LAN-ready hosting.
- Keep private and marketplace extensions optional.
- Keep browser, desktop, and mobile surfaces pointed at the same backend-owned
  state model.

## Security

- Harden auth, WebSocket, internal loopback, extension loading, file access, and
  subprocess boundaries.
- Keep marketplace signing keys private.
- Make release provenance and checksum verification routine.

## Contributors

- Use DCO sign-off for contributions unless counsel later requires a CLA.
- Keep extension SDK examples small, inspectable, and permission-scoped.
- Keep public issues focused on bugs, safe feature requests, and extension
  proposals.

