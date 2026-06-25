from __future__ import annotations

PRIMARY_SERVICE = "better-agent"
LEGACY_SERVICE = "better-claude"


def service_names(primary: str | None = None, legacy: str | None = None) -> tuple[str, ...]:
    primary = PRIMARY_SERVICE if primary is None else primary
    legacy = LEGACY_SERVICE if legacy is None else legacy
    names: list[str] = []
    for name in (primary, legacy):
        if name and name not in names:
            names.append(name)
    return tuple(names)


def home_suffix() -> str:
    """Per-instance suffix for AUTH credentials.

    Empty for the default home so existing keychain entries keep working
    unchanged; otherwise a filesystem-safe slug of the resolved
    BETTER_AGENT_HOME, so each Better Agent instance owns its own auth
    (user/password) instead of all sharing one global keychain entry.
    """
    try:
        import paths
        home = paths.ba_home().resolve()
        default = paths._default_home().resolve()
    except Exception:
        return ""
    if home == default:
        return ""
    import re
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(home)).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[-63:] or ""


def auth_services() -> tuple[str, ...]:
    """Home-scoped (primary, legacy) services for AUTH credentials only.

    Other keychain users (config secrets, credential-broker master key,
    provider-key sinks) intentionally keep using the unsuffixed global
    `service_names()` so they remain shared/unchanged.
    """
    suffix = home_suffix()
    if not suffix:
        return service_names()
    return service_names(f"{PRIMARY_SERVICE}-{suffix}", f"{LEGACY_SERVICE}-{suffix}")
