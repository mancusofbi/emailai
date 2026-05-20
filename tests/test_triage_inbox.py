"""Unit tests for triage_inbox helpers.

Run from the repo root with:  python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import triage_inbox as t


class IsAuthErrorTests(unittest.TestCase):
    def test_detects_invalid_grant(self):
        self.assertTrue(t.is_auth_error("error: invalid_grant: Bad Request"))

    def test_detects_authentication_failed(self):
        self.assertTrue(t.is_auth_error("error[auth]: Authentication failed: Failed to get token"))

    def test_case_insensitive(self):
        self.assertTrue(t.is_auth_error("INVALID_GRANT"))

    def test_unrelated_error(self):
        self.assertFalse(t.is_auth_error("error[api]: Invalid id value"))

    def test_empty(self):
        self.assertFalse(t.is_auth_error(""))
        self.assertFalse(t.is_auth_error(None))  # type: ignore[arg-type]


class ParseClaudeJsonTests(unittest.TestCase):
    def test_clean_array(self):
        text = '[{"id":"a","category":"skim"}]'
        self.assertEqual(t.parse_claude_json(text), [{"id": "a", "category": "skim"}])

    def test_array_with_surrounding_noise(self):
        text = 'Here you go:\n[{"id":"a","category":"important"}]\nThanks!'
        self.assertEqual(
            t.parse_claude_json(text),
            [{"id": "a", "category": "important"}],
        )

    def test_array_with_markdown_fences(self):
        text = '```json\n[{"id":"a","category":"maybe","reason":"x"}]\n```'
        self.assertEqual(
            t.parse_claude_json(text),
            [{"id": "a", "category": "maybe", "reason": "x"}],
        )

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            t.parse_claude_json("")

    def test_no_array_raises(self):
        with self.assertRaises(ValueError):
            t.parse_claude_json("Sorry, I cannot help with that")

    def test_malformed_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            t.parse_claude_json("[not valid json]")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        self.patcher = mock.patch.object(t, "CHECKPOINT_FILE", self.tmp.name)
        self.patcher.start()
        os.remove(self.tmp.name)  # start clean — load should handle missing file

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_load_when_missing_returns_empty_set(self):
        self.assertEqual(t.load_checkpoint(), set())

    def test_round_trip(self):
        ids = {"a", "b", "c"}
        t.save_checkpoint(ids)
        self.assertEqual(t.load_checkpoint(), ids)

    def test_load_corrupt_returns_empty_set(self):
        with open(self.tmp.name, "w") as f:
            f.write("not json {{{")
        self.assertEqual(t.load_checkpoint(), set())

    def test_clear_removes_file(self):
        t.save_checkpoint({"x"})
        self.assertTrue(os.path.exists(self.tmp.name))
        t.clear_checkpoint()
        self.assertFalse(os.path.exists(self.tmp.name))

    def test_clear_when_missing_is_noop(self):
        t.clear_checkpoint()  # should not raise


class RunGwsAuthDetectionTests(unittest.TestCase):
    """run_gws should raise AuthExpiredError on invalid_grant, RuntimeError otherwise."""

    def _fake_completed(self, returncode, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_success_parses_json(self):
        with mock.patch.object(t.subprocess, "run",
                               return_value=self._fake_completed(0, stdout='{"ok": 1}')):
            self.assertEqual(t.run_gws("gmail", "any"), {"ok": 1})

    def test_invalid_grant_raises_auth_expired(self):
        stderr = "error[auth]: Authentication failed: invalid_grant: Bad Request"
        with mock.patch.object(t.subprocess, "run",
                               return_value=self._fake_completed(1, stderr=stderr)):
            with self.assertRaises(t.AuthExpiredError):
                t.run_gws("gmail", "any")

    def test_other_error_raises_runtime_error(self):
        with mock.patch.object(t.subprocess, "run",
                               return_value=self._fake_completed(1, stderr="error[api]: Invalid id")):
            with self.assertRaises(RuntimeError) as ctx:
                t.run_gws("gmail", "any")
            self.assertNotIsInstance(ctx.exception, t.AuthExpiredError)


class ClassifyBatchErrorMessageTests(unittest.TestCase):
    """When claude CLI fails with empty stderr, the error should still be informative."""

    def test_empty_stderr_falls_back_to_stdout(self):
        with mock.patch.object(t.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="boom from stdout", stderr="")):
            with mock.patch.object(t, "CLASSIFY_RETRIES", 0):
                with self.assertRaises(RuntimeError) as ctx:
                    t.classify_batch([{"id": "a", "is_newsletter": False,
                                       "from": "x", "subject": "y", "snippet": "z"}],
                                     persona="p")
                self.assertIn("boom from stdout", str(ctx.exception))

    def test_empty_stderr_and_stdout_uses_exit_code(self):
        with mock.patch.object(t.subprocess, "run",
                               return_value=mock.Mock(returncode=2, stdout="", stderr="")):
            with mock.patch.object(t, "CLASSIFY_RETRIES", 0):
                with self.assertRaises(RuntimeError) as ctx:
                    t.classify_batch([{"id": "a", "is_newsletter": False,
                                       "from": "x", "subject": "y", "snippet": "z"}],
                                     persona="p")
                self.assertIn("exit 2", str(ctx.exception))


class AuthErrorPropagationTests(unittest.TestCase):
    """If gws auth expires mid-run, helpers must propagate AuthExpiredError instead
    of swallowing it as a generic failure — otherwise the script would keep hammering
    expired credentials for the rest of the batch."""

    def test_get_message_meta_propagates_auth_expired(self):
        with mock.patch.object(t, "run_gws", side_effect=t.AuthExpiredError("expired")):
            with self.assertRaises(t.AuthExpiredError):
                t.get_message_meta("abc")

    def test_get_message_meta_swallows_other_errors(self):
        with mock.patch.object(t, "run_gws", side_effect=RuntimeError("transient")):
            self.assertIsNone(t.get_message_meta("abc"))

    def test_archive_batch_propagates_auth_expired(self):
        with mock.patch.object(t, "apply_label_and_archive",
                               side_effect=t.AuthExpiredError("expired")):
            with self.assertRaises(t.AuthExpiredError):
                t.archive_batch(
                    [{"id": "m1", "category": "skim"}],
                    {"orders": "L1", "maybe": "L2", "skim": "L3"},
                )


if __name__ == "__main__":
    unittest.main()
