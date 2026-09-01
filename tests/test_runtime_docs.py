"""Runtime instruction files must not point at developer-only documentation."""

from __future__ import annotations

import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_LITERAL = "docs/"


def _runtime_markdown_files() -> list[Path]:
    files = [SKILL_ROOT / "SKILL.md", SKILL_ROOT / "README.md"]
    files.extend(sorted((SKILL_ROOT / "references").glob("*.md")))
    return files


class TestRuntimeDocsIsolation(unittest.TestCase):
    def test_runtime_markdown_does_not_mention_developer_doc_dir(self) -> None:
        offenders: list[str] = []
        for path in _runtime_markdown_files():
            text = path.read_text(encoding="utf-8")
            if FORBIDDEN_LITERAL in text:
                offenders.append(str(path.relative_to(SKILL_ROOT)))
        self.assertEqual(
            offenders,
            [],
            "运行包说明文件不得引用开发文档目录，否则发布后会断链",
        )


if __name__ == "__main__":
    unittest.main()
