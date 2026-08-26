---
name: daily-market-review
description: "每日资本市场总体复盘。整理、补充、修正和查看指定交易日的涨跌停、市场宽度、连板、两融、指数、成交额、市值与估值数据。用户提到市场复盘、总体复盘、涨跌停名单、连板、两融，或要求从图片、链接、网站、截图或自然语言提取并保存时使用。"
metadata:
  author: stock-pilot
  version: 0.3.0
  category: finance
  tags:
    - a-share
    - market-review
    - daily-review
    - limit-up
    - streak
    - 市场复盘
    - 总体复盘
---

# 每日市场复盘

整理、补充、修正和展示指定已收盘交易日的资本市场总体复盘数据。不做买卖建议。

## 运行方式

使用系统 `python3` 和标准库，不依赖 Stock Pilot 仓库、虚拟环境或 pip 安装。

默认数据库：

```text
~/.marketreview/market_review.sqlite3
```

路径优先级：

```text
CLI --db
> MARKETREVIEW_HOME/market_review.sqlite3
> ~/.marketreview/market_review.sqlite3
```

Skill 通过 JSON CLI 读写，不直接 import 包，也不直接操作 SQLite：

```bash
python3 scripts/cli.py --db /tmp/marketreview-test.sqlite3 get --date 2026-08-21
python3 scripts/cli.py save-review --date 2026-08-21 --input -
python3 scripts/cli.py save-events --date 2026-08-21 --input -
python3 scripts/cli.py delete-event --date 2026-08-21 --market sh --code 600519 --direction up
python3 scripts/cli.py replace-direction --date 2026-08-21 --market sh --code 600519 --old-direction up --input -
```

CLI 输出统一为：

```json
{"ok": true, "data": {}, "error": null}
```

## 两个核心功能

### 1. 写入

- 理解用户请求范围（完整复盘，或点名字段/类别）
- 通过网页、图片、API 或用户文字取得数据
- 完成业务校验后调用 CLI 写入
- 三只指数（上证、深证成指、创业板指）可通过内置腾讯日 K 自动补齐

### 2. 读取展示

- 调用 `get` 读取原子字段、事件、统计摘要和缺失字段
- 按 `references/数据字段与口径.md` 格式化展示
- 读取时不自动取数、不修改数据库

## 边界

- 写入和显示相互独立
- 有效涨停、炸板、连板等派生值由 `get` 返回的 `summary` 统计，不单独入库
- 来源、网页链接、图片和采集方式不入库
- 多 Agent 共享 `~/.marketreview/market_review.sqlite3`；SQLite 已启用 WAL 和 busy timeout

## 参考文档

- `references/数据字段与口径.md` — 字段、单位、事件身份、保存语义
- `references/Skill行为说明.md` — 采集、校验、统计、展示、用户交互
- `references/资本市场复盘指标说明与统计口径.md` — 指标附录
