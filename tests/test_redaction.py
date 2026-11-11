"""Suite logs and printed lines scrub KIRO_API_KEY."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "harness"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _state_guard

# run_suite reads the hook log path, and so makes the state dir, on import.
_IMPORT = _state_guard.Isolation(__name__ + " import")
_IMPORT.start()
try:
    import run_suite
finally:
    _IMPORT.stop()

_ISOLATION = _state_guard.Isolation(__name__)


def setUpModule():
    _ISOLATION.start()


def tearDownModule():
    _ISOLATION.stop()


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.api_key = "unit-test-api-key-value"
        self.previous = os.environ.get("KIRO_API_KEY")
        os.environ["KIRO_API_KEY"] = self.api_key

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("KIRO_API_KEY", None)
        else:
            os.environ["KIRO_API_KEY"] = self.previous

    def test_guard_text_redacts_the_api_key(self):
        cleaned = run_suite.guard_text(f"prefix {self.api_key} suffix")
        self.assertNotIn(self.api_key, cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_guard_text_fails_when_scrub_leaves_the_key(self):
        with mock.patch.object(run_suite, "scrub", lambda text: text), self.assertRaises(run_suite.SecretLeakError):
            run_suite.guard_text(self.api_key)

    def test_write_log_and_print_do_not_keep_the_key(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        with mock.patch.object(run_suite, "LOG_DIR", base):
            path = run_suite.write_log(
                "sample",
                [{"banner": f"see {self.api_key}", "interesting": [f"{self.api_key}"]}],
            )
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
        self.assertNotIn(self.api_key, body)
        row = json.loads(body)
        self.assertIn("[redacted]", row["banner"])
        printed = []

        def capture(text, flush=False):
            printed.append((text, flush))

        with mock.patch("builtins.print", capture):
            run_suite.say(f"status {self.api_key}")
        self.assertEqual(len(printed), 1)
        text, flush = printed[0]
        self.assertTrue(flush)
        self.assertNotIn(self.api_key, text)
        self.assertIn("[redacted]", text)


if __name__ == "__main__":
    unittest.main()
