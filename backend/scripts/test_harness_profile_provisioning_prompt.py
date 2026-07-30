from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-harness-profile-provisioning-prompt-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness_profile_store  # noqa: E402
import main  # noqa: E402
import workers_api  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def _profile(profile_id: str, prompt: str) -> dict:
    profile = harness_profile_store.create_profile({"id": profile_id, "name": profile_id})
    return harness_profile_store.set_profile_meta(
        profile_id,
        {"provisioning_prompt": prompt},
        revision=profile["revision"],
    )


def main_test() -> None:
    profile = _profile("prompted.profile", "Profile prep")
    child = harness_profile_store.create_profile({"id": "prompted.child", "name": "Prompted Child"})
    harness_profile_store.set_profile_meta(
        "prompted.child",
        {"base_profile_id": profile["id"]},
        revision=child["revision"],
    )

    explicit = workers_api._worker_provision_prompt_for_body(
        body={
            "provision_prompt": "Explicit prep",
            "harness_profile_id": "prompted.profile",
        },
        bc_session_id="",
        description="worker",
    )
    check(explicit == "Explicit prep", "explicit provision_prompt wins over profile prompt")

    inherited = workers_api._worker_provision_prompt_for_body(
        body={"harness_profile_id": "prompted.child"},
        bc_session_id="",
        description="worker",
    )
    check(inherited == "Profile prep", "child profile inherits base provisioning prompt")

    sess = session_manager.create(
        name="Existing Worker",
        model="test-model",
        cwd="/tmp",
        orchestration_mode="native",
        provider_id=None,
        harness_profile_id="prompted.profile",
    )
    from_session = workers_api._worker_provision_prompt_for_body(
        body={},
        bc_session_id=sess["id"],
        description="Existing Worker",
    )
    check(from_session == "Profile prep", "existing session profile supplies provisioning prompt")

    fallback = workers_api._worker_provision_prompt_for_body(
        body={},
        bc_session_id="",
        description="Fallback Worker",
    )
    check("Fallback Worker" in fallback, "default provisioning template remains fallback")


if __name__ == "__main__":
    main_test()
    print("PASS harness profile provisioning prompt")
