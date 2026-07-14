from __future__ import annotations


BFF_SERVICE_TOKEN_NAME = "bff-service.token"
BFF_SERVICE_TOKEN_HEADER = "X-Better-Agent-BFF-Token"
BFF_SERVICE_TOKEN_KIND = "bff"
BFF_SERVICE_SCOPE = "bff_service"
PROJECT_CATALOG_SCHEMA_VERSION = 1


def project_candidate_from_session(session: object) -> dict | None:
    if not isinstance(session, dict):
        return None
    cwd = session.get("cwd")
    if (
        not isinstance(cwd, str)
        or not cwd
        or session.get("bare_config")
        or session.get("source") == "import"
        or session.get("cwd_explicit", True) is False
    ):
        return None
    candidate = {
        "path": cwd,
        "node_id": session.get("node_id") or "primary",
        "name": session.get("project_name") or "",
        "created_at": session.get("created_at") or "",
        "updated_at": session.get("updated_at") or "",
    }
    return {key: value for key, value in candidate.items() if value not in (None, "")}
