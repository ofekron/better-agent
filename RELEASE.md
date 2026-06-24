# Release Integrity

Releases must be reproducible enough for users to verify what they installed.

## Required Release Steps

1. Start from a clean working tree.
2. Run the publication checklist in `PUBLICATION_CHECKLIST.md`.
3. Create a signed release tag.
4. Build desktop/mobile artifacts from that tag.
5. Generate SHA-256 checksums for every artifact.
6. Sign artifacts or the checksum manifest.
7. Publish release notes that identify:
   - source commit;
   - tag;
   - build machine or CI pipeline;
   - artifact filenames;
   - SHA-256 checksums;
   - signing key fingerprint or verification key.

## Marketplace Artifacts

Marketplace extension artifacts must be verified by digest and signature before
load. The public repo may contain verification keys. Private signing keys and
the release pipeline must stay outside the public repo.

## Do Not Release

Do not release from:

- a dirty working tree;
- an unsigned tag;
- a folder containing `better-agent-private/`;
- a folder containing `.env`, `.better-claude/`, virtualenvs, node_modules, or
  generated download artifacts;
- a build whose dependencies have not been audited from a clean environment.

