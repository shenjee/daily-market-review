# 开发指南

本文档面向参与 Daily Market Review 开发和调试的贡献者，不是最终用户安装说明。

开发前请阅读：

- [PRD](docs/PRD.md)
- [数据库设计](docs/数据库设计.md)
- `references/数据字段与口径.md`
- `references/资本市场复盘指标说明与统计口径.md`

[V2 每日梯队开发实现](docs/V2每日梯队开发实现.md) 是已落地的历史实现规格，不作为现行产品合同。

## 获取源码

```bash
git clone https://github.com/shenjee/daily-market-review.git
cd daily-market-review
```

开发期间如需让 Agent 直接加载当前工作副本，可以将仓库软链接到相应 Agent 的 Skill 目录。该方式仅用于本地开发，不应作为发布版本的安装方式。

以 Codex 为例：

```bash
ln -s "$(pwd)" ~/.codex/skills/daily-market-review
```

## CLI 调试

`--db` 必须放在子命令之前。调试和测试时应显式指定临时数据库，避免修改真实用户数据：

```bash
python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 get --date 2026-08-21

python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 save-review --date 2026-08-21 --input - <<'EOF'
{"pe_sh": 17.0, "advancing_count": 3210}
EOF

python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 save-events --date 2026-08-21 --input - <<'EOF'
{"events": [{"market": "sh", "code": "600519", "name": "贵州茅台", "direction": "up", "closed_at_limit": true, "limit_rate_bp": 1000, "streak_height": 4}]}
EOF

python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 save-event-details --date 2026-08-21 --input - <<'EOF'
{"details": [{"market": "sh", "code": "600519", "direction": "up", "sectors": ["白酒"], "limit_up_reasons": ["业绩增长"], "is_leader": true}]}
EOF
```

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 发布包内容

最终用户版本包命名为 `daily-market-review-vX.Y.Z.zip`，解压后根目录为 `daily-market-review/`，应只包含运行所需内容：

```text
daily-market-review/
├── SKILL.md
├── README.md
├── scripts/
├── assets/
└── references/
```

不要把 `.git/`、`tests/`、`__pycache__/`、本地数据库、编辑器配置或其他开发产物放入版本包。公开分发时，发布包中应包含 `LICENSE`。

## 发布检查清单

正式发布前：

1. 运行全部单元测试。
2. 确认或更新 `assets/trading_calendar.json` 的覆盖范围。
3. 确认运行时说明文件不含开发文档目录字面量（由 `tests/test_runtime_docs.py` 覆盖；也可手动扫描 `SKILL.md`、`README.md` 和 `references/`）。
4. 按上述目录结构生成 `daily-market-review-vX.Y.Z.zip`。
5. 创建 GitHub Release，上传版本包（不要依赖 GitHub 自动生成的 Source code 压缩包）。
6. 用解压后的 ZIP 独立安装测试一次，确认不依赖仓库中的额外文件（如 `tests/`）。
