import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_scheduler_diag as diag


class ApiSchedulerDiagTests(unittest.TestCase):
    def test_structured_journal_and_problem_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "api.jsonl"
            with patch.dict(os.environ, {
                "API_SCHEDULER_JOURNAL": str(journal),
                "CC_CWD": temp,
            }, clear=False):
                diag.write_event("OR_PROXY_OK", status=200)
                diag.write_event(
                    "KEEPALIVE_HTTP_ERROR", level="error", status=404
                )
                rows = diag._recent_entries(10)
        self.assertEqual([row["code"] for row in rows], [
            "OR_PROXY_OK", "KEEPALIVE_HTTP_ERROR",
        ])
        self.assertFalse(diag._is_problem(rows[0]))
        self.assertTrue(diag._is_problem(rows[1]))
        self.assertNotIn("authorization", json.dumps(rows).lower())


if __name__ == "__main__":
    unittest.main()
