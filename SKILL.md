---
name: daily-market-review
description: "每日资本市场总体复盘。整理、补充、修正和查看指定交易日的涨跌停、市场宽度、连板、每日梯队、两融、指数、成交额、市值与估值数据。用户提到市场复盘、总体复盘、涨跌停名单、连板、每日梯队、两融，或要求从图片、链接、网站、截图或自然语言提取并保存时使用。"
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
    - ladder
    - 市场复盘
    - 总体复盘
    - 每日梯队
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

Skill 通过 JSON CLI 读写，不直接 import 包，也不直接操作 SQLite。执行前先取得当前 `SKILL.md` 所在目录的绝对路径，并用它替换下列命令中的 `<skill-dir>`；不能假设当前工作目录就是 Skill 安装目录：

```bash
python3 "<skill-dir>/scripts/cli.py" get --date 2026-08-21
python3 "<skill-dir>/scripts/cli.py" save-review --date 2026-08-21 --input -
python3 "<skill-dir>/scripts/cli.py" save-events --date 2026-08-21 --input -
python3 "<skill-dir>/scripts/cli.py" save-event-details --date 2026-08-21 --input -
python3 "<skill-dir>/scripts/cli.py" delete-event --date 2026-08-21 --market sh --code 600519 --direction up
python3 "<skill-dir>/scripts/cli.py" replace-direction --date 2026-08-21 --market sh --code 600519 --old-direction up --input -
```

CLI 输出统一为：

```json
{"ok": true, "data": {}, "error": null}
```

## 按任务加载说明文档

下表限制的是 Agent 加载的说明文档，不限制执行 CLI、读取交易日历和读取用户数据库。普通市场请求不加载开发文档。

| 任务 | 加载的说明文档 |
| --- | --- |
| 查看已保存复盘 | 仅本文件 |
| 写入或修订总体复盘字段（如补齐两融、指数、成交额） | 本文件 + `references/Skill行为说明.md` + `references/数据字段与口径.md` |
| 写入或修订涨跌停事件、连板或每日梯队 | 上述三份 + `references/资本市场复盘指标说明与统计口径.md` |
| 解释指标或统计公式 | `references/资本市场复盘指标说明与统计口径.md`；涉及存储语义时再加 `references/数据字段与口径.md` |

写入和修订必须同时读取行为说明与字段合同，不要自行判断可否省略。

## 日期规则

用 `assets/trading_calendar.json` 判断交易日（周末以及文件中的闭市日期都不是交易日）。读取该日历不是加载说明文档。日度复盘以 Asia/Shanghai 当天 15:00 为收盘边界。北交所与沪深使用同一交易日。

未指定日期：查看和写入都使用最近一个已收盘交易日。

用户说「今天」「今日」：

- 查看：当前为交易日且已过 15:00 → 当日；否则回退最近一个已收盘交易日。
- 写入：当前为交易日且已过 15:00 → 当日。当前为交易日但尚未收盘 → 拒绝写入，不得改写到上一交易日。当前不是交易日 → 不落库，并提示最近交易日。

用户显式给出日历日期：

- 查看：按该日期调用 `get`。不是交易日或没有数据时按下方「无数据显示」，不要改写成最近交易日。
- 写入：目标日必须是已收盘交易日。显式指定非交易日则不落库，并提示最近交易日；显式指定当日且尚未收盘则拒绝正式记录。历史已收盘交易日可直接写入或修订。

## 两个核心功能

### 1. 写入

- 理解用户请求范围（完整复盘、涨跌停事件、每日梯队扩展，或点名字段/类别）
- 通过网页、图片、API 或用户文字取得数据
- 完成业务校验后调用 CLI 写入
- 事件扩展只能关联已保存的涨跌停事件；`limit_up_reasons` 只允许 `direction=up`
- 三只指数（上证、深证成指、创业板指）可通过内置腾讯日 K 自动补齐

### 2. 读取展示

- 调用 `get` 始终读取原子字段、事件、统计摘要、缺失字段和 `ladder`
- 按下方规则格式化；只看总体复盘时可忽略 `ladder`
- 读取时不自动取数、不修改数据库、不追问缺失项

## 展示规则

`review` 为 `null` 且 `events` 为空时，显示「无复盘数据」，到此结束。不要把这种情况渲染成各指标为 `0`。

有事件但没有总体复盘行时，原子字段留空，仍展示 `summary` 中由事件得到的涨跌停与连板指标。

`review` 中的 `null` 单元格留空，不显示 `—`、`0` 或其他占位符。已确认的数值为 `0` 时必须显示 `0`。某日没有对应涨跌停事件时，`summary` 里的事件数量按 `0` 展示，这与「无复盘数据」不同。

单位：

- `review` 里的成交额、市值、融资余额按元存储，展示为亿元或万亿元。
- `review` 里的比率按小数存储，展示为百分数。
- `summary` 里 `broken_rate_pct`、`streak_rate_pct` 和指数 `change_pct` 已是百分数数值，直接加 `%`，不要再乘 100。
- 指数点位、PE、平均股价按原单位，保留两位小数。
- 个股成交额展示为万元或亿元；个股比率展示为百分数。

涨跌停比使用 `summary.limit_up_down_ratio.display`。两边都为 0 或基础数据缺失时留空。

总体复盘按类别使用简单表格，只展示指标和值，不展示采集方式或更新时间：

| 类别 | 指标来源 |
| --- | --- |
| 涨跌停 | `summary`：有效涨停、20% 涨停、打开跌停、收盘跌停、涨停炸板、炸板率 |
| 市场宽度 | `review` 回头波、中位数涨跌幅、上涨/下跌家数；`summary` 涨跌停比 |
| 连板 | `summary`：首板、连板、连板率、最高板、最高板代表、`streak_by_height` |
| 两融 | `review` 三项融资余额；`summary.margin_balance_total` |
| 指数 | `review` 收盘点位；`summary` 中对应指数的涨跌点数和涨跌幅 |
| 成交额 | `review` 上海/深圳/创业板/北京；`summary.turnover_amount_total` |
| 市值与估值 | `review` 总市值、流通市值、四项 PE |
| 平均股价 | `review.avg_stock_price` |

连板高度按 `streak_by_height` 动态分组。总体复盘如需紧凑，可将 11 板及以上合并为 `11板+`。

每日梯队（用户要求查看梯队或完整复盘含梯队时）：

- 使用 `ladder.groups`，从最高板到首板；同高度已按 `market + code` 排好。
- 可展示板块、涨停原因、竞价占比、开盘涨幅、当日成交额、换手率、龙头和备注。
- 多值字段按保存顺序以 ` / ` 连接。
- `is_leader=true` 显示「龙头」，`false` 和 `null` 均留空。
- `ladder.broken_limit_up`、`opened_limit_down`、`closed_limit_down` 在梯队之后作为独立名单。

## 边界

- 写入和显示相互独立
- 有效涨停、炸板、连板等派生值由 `get` 返回的 `summary` 统计，不单独入库
- 每日梯队、竞价占比和开盘涨幅由 `get` 返回的 `ladder` 生成，不单独入库
- 来源、网页链接、图片和采集方式不入库
- 多 Agent 共享 `~/.marketreview/market_review.sqlite3`；SQLite 已启用 WAL、busy timeout 和 foreign keys
