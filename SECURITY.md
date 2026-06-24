# Security

Better Agent can launch provider CLIs, execute tool calls, read and write local
files, expose extension surfaces, and persist session data. Treat any
vulnerability as potentially able to affect the user's code, files, credentials,
or machine.

## Reporting Vulnerabilities

Do not file public issues for security problems.

Report vulnerabilities privately to Ofek Ron. If the hosted repository supports
private vulnerability reports, use that channel. Otherwise contact the
maintainer privately before sharing details in any public issue, discussion, or
merge request.

Include:

1. affected version or commit;
2. reproduction steps;
3. impact;
4. whether secrets, files, commands, extensions, marketplace artifacts, auth, or
   WebSocket/REST endpoints are involved.

## Security Boundary

Better Agent is intended for users who understand that agentic coding tools can
perform destructive actions. Users are responsible for where they run it, what
projects they open, which extensions they install, which providers they connect,
and which tool calls they approve.

Run Better Agent only in trusted environments. Do not expose the backend to an
untrusted network. Do not install untrusted extensions. Do not paste secrets
into prompts or logs.

## High-Risk Areas

Security reports are especially important for:

1. command execution or subprocess spawning;
2. filesystem access or path traversal;
3. authentication, session cookies, tokens, or credential storage;
4. WebSocket origin/auth checks and REST endpoints;
5. extension install, update, provisioning, permissions, or backend routes;
6. marketplace artifact signatures, entitlements, and trust roots;
7. secret leakage through logs, traces, prompts, session history, or UI state;
8. network exposure, worker-node routing, or remote execution.

## Release Gate

Changes touching subprocesses, filesystem access, auth, approvals, networking,
extensions, marketplace code, or secret handling require security review before
release.

## Repository Settings

Before public visibility, the hosted repository should enable protected default
branch rules, required merge requests, CODEOWNERS approval for sensitive paths,
passing CI before merge, signed release tags, and private vulnerability
reporting.
