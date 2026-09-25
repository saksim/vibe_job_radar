"""A failed network observation may expose only fixed browser categories."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    from run_native_liepin_probe import failure_policy_metadata


class NativeFailureProbeTests(unittest.TestCase):
    def test_cors_and_blocked_reason_preserve_only_known_categories(self):
        self.assertEqual(failure_policy_metadata({
            'blockedReason': 'origin',
            'corsErrorStatus': {'corsError': 'AllowOriginMismatch', 'failedParameter': 'private-origin'},
            'requestId': 'private-request', 'errorText': 'private-input',
        }), {'blocked_reason': 'origin', 'cors_error': 'AllowOriginMismatch'})

    def test_missing_and_unrecognized_values_are_not_reported_as_known_causes(self):
        self.assertEqual(failure_policy_metadata({}), {'blocked_reason': None, 'cors_error': None})
        for value in ('private-value', ['origin'], {'unexpected': 'secret'}):
            with self.subTest():
                self.assertEqual(failure_policy_metadata({
                    'blockedReason': value, 'corsErrorStatus': {'corsError': value, 'failedParameter': 'secret'},
                }), {'blocked_reason': 'unclassified', 'cors_error': 'unclassified'})
