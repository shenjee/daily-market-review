"""Runtime instruction files must not point at developer-only documentation."""

from __future__ import annotations

import shutil
import tempfile
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
    "涨跌停四类采集执行流程",
    "库内无涨跌停事件",
    "库内已保存事件的派生统计",
    "部分采集",
    "已核验零事件",
    "采集结果与保存结果",
    "扩大为四类全量",
)

REQUIRED_BEHAVIOR_MARKERS = (
    "## 读库失败（写入路径）",
    "## 涨跌停四类采集执行流程",
    "### 流程适用范围",
    "ST 硬规则",
    "limit_up_pool",
    "open_limit_pool",
    "lower_limit_pool",
    "完整复盘硬门禁",
    "写入前确定",
    "前日记录缺失",
    "采集结果与保存结果",
    "写入失败",
    "## 指标核验、重试与换源",
    "### 两融失败结束条件（专属，不自行换源）",
    "## 采集状态、数据库状态与未完成项",
    "不因读库失败改写请求范围",
    "成功与失败分开",
    "点名范围",
    "部分采集",
    "不触发",
)

RELEASE_TREE_MARKERS = (
    "├── contracts/",
    "│   └── supabase_rpc_v1.json",
    "├── sql/",
    "│   └── migrations/",
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

    def test_release_package_lists_backup_runtime_dependencies(self) -> None:
        text = (SKILL_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        missing = [marker for marker in RELEASE_TREE_MARKERS if marker not in text]
        self.assertEqual(missing, [], f"发布清单缺少备份依赖: {missing}")

    def test_simulated_release_tree_exposes_backup_defaults(self) -> None:
        # 按 CONTRIBUTING 清单搭隔离树，确认 pg_backup 默认合同/迁移路径可解析。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "daily-market-review"
            root.mkdir(parents=True)
            for name in ("SKILL.md", "README.md", "LICENSE"):
                (root / name).write_text("x\n", encoding="utf-8")
            for relative in (
                "config/marketreview.config.example",
                "config/supabase.secret.example",
                "contracts/supabase_rpc_v1.json",
                "sql/migrations/0001_marketreview_v1.sql",
                "assets/.keep",
                "references/.keep",
                "scripts/pg_backup.py",
                "scripts/marketreview/pg_backup.py",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("supabase_rpc_v1.json"):
                    shutil.copy2(SKILL_ROOT / relative, path)
                elif relative.endswith(".sql"):
                    path.write_text("-- stub\n", encoding="utf-8")
                else:
                    path.write_text("# stub\n", encoding="utf-8")
            self.assertTrue((root / "contracts" / "supabase_rpc_v1.json").is_file())
            self.assertTrue((root / "sql" / "migrations" / "0001_marketreview_v1.sql").is_file())
            payload = (root / "contracts" / "supabase_rpc_v1.json").read_text(encoding="utf-8")
            self.assertIn("marketreview_probe", payload)
            # Resolve the same way scripts/marketreview/pg_backup.py does: parents[2] from that file.
            module_path = root / "scripts" / "marketreview" / "pg_backup.py"
            package_root = module_path.resolve().parents[2]
            self.assertEqual(package_root, root.resolve())
            self.assertTrue((package_root / "contracts" / "supabase_rpc_v1.json").is_file())
            self.assertTrue((package_root / "sql" / "migrations").is_dir())


if __name__ == "__main__":
    unittest.main()
