"""Platform daemon host — root supervisor for extension-declared daemons.

Extensions declare daemons in better-agent-extension.json under
``entrypoints.daemons``. Two lifecycles exist:

- ``backend``: supervised children of the backend process (see
  ``backend/extension_daemons.py``) — they start and stop with it.
- ``supervisor``: outlive the backend. The host in this package installs a
  copy of the extension package under ``ba_home()/daemons/<ext>/<name>/`` and
  supervises it across backend restarts. Installs are selftest-gated and a
  last-known-good copy is kept for rollback.

The host is platform code, deliberately small and stdlib-only. It runs from
the launcher's own checkout (run.sh spawns ``python -m daemonhost``; the
desktop shell runs it in-process) and is versioned with that checkout — it
updates when the launcher restarts, never live.

The backend never talks to daemons directly; it publishes the desired
supervisor-daemon set to ``ba_home()/daemons/registry.json`` (see
``backend/extension_daemons.py``) and reads back the host-owned
``state.json`` as a projection for the UI.
"""
