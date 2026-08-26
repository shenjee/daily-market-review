# daily-market-review

每日资本市场总体复盘 Skill（MVP）。

## 功能

- **写入**：整理、补充、修正指定交易日的复盘原子字段和涨跌停事件
- **读取展示**：读取已保存数据，统计并格式化展示

## 依赖

- 系统 `python3`（>= 3.11 推荐）
- 标准库 only（sqlite3、json、urllib 等）
- 零第三方 Python 依赖

## 数据位置

```text
~/.marketreview/market_review.sqlite3
```

可通过环境变量覆盖：

```bash
export MARKETREVIEW_HOME="$HOME/.marketreview"
```

测试时可显式传入：

```bash
python3 scripts/cli.py get --date 2026-08-21 --db /tmp/marketreview-test.sqlite3
```

## CLI

```bash
python3 scripts/cli.py get --date 2026-08-21

python3 scripts/cli.py save-review --date 2026-08-21 --input - <<'EOF'
{"pe_sh": 17.0, "advancing_count": 3210}
EOF

python3 scripts/cli.py save-events --date 2026-08-21 --input - <<'EOF'
{"events": [{"market": "sh", "code": "600519", "name": "贵州茅台", "direction": "up", "closed_at_limit": true, "limit_rate_bp": 1000, "streak_height": 4}]}
EOF

# 测试时可显式指定数据库路径（--db 放在子命令之前）
python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 get --date 2026-08-21
```

## 安装到 Agent

将整个仓库软链接到 Agent 的 Skill 目录，例如：

```bash
ln -s /path/to/daily-market-review ~/.codex/skills/daily-market-review
```

Codex、Cursor、OpenClaw 等 Agent 各自安装，但共享同一份 `~/.marketreview/` 用户数据。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 目录结构

```text
daily-market-review/
├── SKILL.md
├── README.md
├── scripts/
│   ├── cli.py
│   └── marketreview/
├── assets/
│   ├── trading_calendar.json
│   └── securities_master.json
├── references/
└── tests/
```
