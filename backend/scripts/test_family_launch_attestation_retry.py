import unittest
from unittest.mock import MagicMock, patch

from codex_execution_common import ExecutionContractError
from provider_family_execution_runtime import family_launch_from_artifact


class TestFamilyLaunchAttestationRetry(unittest.TestCase):
    @patch("provider_family_execution_runtime._contract_payload")
    @patch("provider_family_execution_runtime.family_execution_kind")
    @patch("provider_family_execution_runtime.FamilyLaunchAttestation")
    def test_retry_succeeds_on_second_attempt(
        self, mock_attestation_cls, mock_family_kind, mock_payload
    ):
        mock_family_kind.return_value = "claude"
        mock_payload.return_value = {}

        mock_instance = MagicMock()
        mock_instance.family = "claude"
        # First call fails, second call succeeds
        mock_instance.attest.side_effect = [False, True]
        mock_attestation_cls.from_payload.return_value = mock_instance

        artifact = MagicMock()
        res = family_launch_from_artifact(artifact, max_retries=1)
        self.assertEqual(res, mock_instance)
        self.assertEqual(mock_instance.attest.call_count, 2)

    @patch("provider_family_execution_runtime._contract_payload")
    @patch("provider_family_execution_runtime.family_execution_kind")
    @patch("provider_family_execution_runtime.FamilyLaunchAttestation")
    def test_retry_fails_when_exceeded(
        self, mock_attestation_cls, mock_family_kind, mock_payload
    ):
        mock_family_kind.return_value = "claude"
        mock_payload.return_value = {}

        mock_instance = MagicMock()
        mock_instance.family = "claude"
        mock_instance.attest.return_value = False
        mock_attestation_cls.from_payload.return_value = mock_instance

        artifact = MagicMock()
        with self.assertRaises(ExecutionContractError):
            family_launch_from_artifact(artifact, max_retries=1)

    def test_attest_with_reason_returns_structured_failure(self):
        from provider_family_launch_attestation import FamilyLaunchAttestation

        mock_attestation = MagicMock(spec=FamilyLaunchAttestation)
        mock_attestation.fingerprint = "abc"
        mock_attestation._computed_fingerprint.return_value = "def"
        mock_attestation.family = "claude"

        ok, reason = FamilyLaunchAttestation.attest_with_reason(mock_attestation)
        self.assertFalse(ok)
        self.assertEqual(reason, "fingerprint_mismatch")


if __name__ == "__main__":
    unittest.main()

