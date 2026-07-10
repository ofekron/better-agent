"""Single source of truth for subprocess pipe line limits.

Every runner reads newline-delimited JSON from local subprocess pipes
(provider CLI stdout streams and MCP stdio connections). A single line can
legitimately be huge: the Claude CLI embeds base64 image tool_results twice
per line (message content block + toolUseResult.file), and multi-image /
PDF reads multiply that. The claude-agent-sdk default of 1 MiB killed live
turns mid-run; 32 MiB bounds the transient per-line buffer well above any
observed legitimate line while staying a sane DoS ceiling for a local,
user-owned subprocess.

Any new spawn whose stdout is consumed line-wise (readline/readuntil) must
pass ``limit=SUBPROCESS_LINE_LIMIT_BYTES`` — asyncio's default is 64 KiB.
Spawns drained via ``communicate()`` need no limit (read() is unbounded).
"""

SUBPROCESS_LINE_LIMIT_BYTES = 32 * 1024 * 1024
