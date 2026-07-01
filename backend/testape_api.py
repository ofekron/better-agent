"""REST surface for TestApe-backed detection, integrated into the Better Agent backend.

Inherits the global loopback browser-trust gate applied to every /api route in
main.py. The optional navigation target is further confined to loopback origins
inside the detector.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import testape_login_detector as detector
import testape_chat_panel_detector as chat_detector

router = APIRouter(prefix="/api/testape", tags=["testape"])


@router.get("/login-state")
def get_login_state(
    adapter_id: str | None = Query(
        None, description="TestApe web adapter to probe; omit to auto-pick the first connected one"
    ),
    url: str | None = Query(
        None, description="Loopback URL to navigate the adapter to before detecting; omit to inspect the current page"
    ),
    fs_url: str | None = Query(None, description="TestApe FS server URL"),
) -> dict:
    """Report whether the app open in a TestApe browser is on the login/setup screen or authenticated."""
    try:
        return detector.detect_login_state(
            adapter_id=adapter_id,
            url=url,
            fs_url=fs_url or detector.FS_DEFAULT,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # TestApe not running, adapter error, eval_js failure
        raise HTTPException(status_code=502, detail=f"testape detection failed: {exc}")


@router.get("/chat-panel/validate")
def validate_chat_panel(
    adapter_id: str | None = Query(
        None, description="TestApe web adapter to probe; omit to auto-pick the first connected one"
    ),
    session_id: str | None = Query(
        None, description="Better Agent session id expected in the visible chat panel"
    ),
    url: str | None = Query(
        None, description="Loopback URL to navigate the adapter to before validating; omit to inspect the current page"
    ),
    fs_url: str | None = Query(None, description="TestApe FS server URL"),
) -> dict:
    try:
        result = chat_detector.validate_chat_panel(
            adapter_id=adapter_id,
            session_id=session_id,
            url=url,
            fs_url=fs_url or detector.FS_DEFAULT,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"testape chat panel validation failed: {exc}")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result)
    return result
