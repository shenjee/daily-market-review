"""The CI workflow runs the real test suite and does not carry credentials."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


class CiWorkflowTest(unittest.TestCase):
    def test_workflow_uses_isolated_postgres_and_no_secrets(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests -p 'test_*.py'", text)
        self.assertIn("MARKETREVIEW_PGPORT=55432", text)
        self.assertIn("--auth=trust", text)
        self.assertIn("permissions:", text)
        self.assertIn("contents: read", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("sb_secret", text)
        self.assertNotIn("service_role", text)
        self.assertNotIn("PGPASSWORD", text)
        self.assertNotIn("SUPABASE", text)
        self.assertNotIn("~/.marketreview", text)


if __name__ == "__main__":
    unittest.main()
