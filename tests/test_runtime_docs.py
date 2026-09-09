"""Runtime instruction files must not point at developer-only documentation."""

from __future__ import annotations

import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_LITERAL = "docs/"

REQUIRED_SKILL_MARKERS = (
    "## 请求分流（默认意图）",
    "## 读库失败",
    "DB_UNAVAILABLE",
    "操作意图",
    "字段范围",
    "库内无涨跌停事件",
    "库内已保存事件的派生统计",
    "部分采集",
    "已核验零事件",
)

REQUIRED_BEHAVIOR_MARKERS = (
    "## 读库失败（写入路径）",
    "## 指标核验、重试与换源",
    "### 涨跌停数据核对",
    "### 两融失败结束条件（专属，不自行换源）",
    "## 采集状态、数据库状态与未完成项",
    "不因读库失败改写请求范围",
    "成功与失败分开",
    "点名范围",
    "部分采集",
)


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

    def test_skill_documents_routing_and_db_failure(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        missing = [marker for marker in REQUIRED_SKILL_MARKERS if marker not in text]
        self.assertEqual(missing, [], f"SKILL.md 缺少验收所需段落: {missing}")

    def test_behavior_documents_verification_and_status_split(self) -> None:
        text = (SKILL_ROOT / "references" / "Skill行为说明.md").read_text(encoding="utf-8")
        missing = [marker for marker in REQUIRED_BEHAVIOR_MARKERS if marker not in text]
        self.assertEqual(missing, [], f"Skill行为说明.md 缺少验收所需段落: {missing}")


if __name__ == "__main__":
    unittest.main()
