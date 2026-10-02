---
name: daily-market-review
description: "每日资本市场总体复盘。整理、补充、修正和查看指定交易日的涨跌停、市场宽度、连板、每日梯队、两融、指数、成交额、市值与估值数据。用户提到市场复盘、总体复盘、涨跌停名单、连板、每日梯队、两融，或要求从图片、链接、网站、截图或自然语言提取并保存时使用。"
metadata:
  author: stock-pilot
  version: 0.3.4
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

未指定日期：查看和写入都使用最近一个已收盘交易日；仅单独补齐融资余额且未指定日期时，以当前表里最新交易日期保存，见 `references/Skill行为说明.md`。

用户说「今天」「今日」：

- 查看：当前为交易日且已过 15:00 → 当日；否则回退最近一个已收盘交易日。
- 写入：当前为交易日且已过 15:00 → 当日。当前为交易日但尚未收盘 → 拒绝写入，不得改写到上一交易日。当前不是交易日 → 不落库，并提示最近交易日。

用户显式给出日历日期：

- 查看：按该日期调用 `get`。不是交易日或没有数据时按下方「无数据显示」，不要改写成最近交易日。
- 写入：目标日必须是已收盘交易日。显式指定非交易日则不落库，并提示最近交易日；显式指定当日且尚未收盘则拒绝正式记录。历史已收盘交易日可直接写入或修订。

## 请求分流（默认意图）

分流分两步，且一经确定**不因读库失败而改写**：

1. **操作意图**：只读还是写入（是否加载行为说明、是否取数）。
2. **字段范围**：完整复盘，还是点名类别 / 字段（只处理范围内）。

| 用户说法（示例） | 操作意图 | 字段范围 | 改判条件 |
| --- | --- | --- | --- |
| 查看、看看、显示、读一下、库里有什么 | 只读：`get` + 下方展示规则 | 按用户点名；未点名则展示已保存总体复盘 | 同时要求补齐 / 修正 / 保存 → 写入 |
| 整理、补充、提取、修正、补齐、保存、写入 | 写入：加载 `references/Skill行为说明.md` 等 | 点名则点名；未限定字段 → 完整复盘 | — |
| 统计、汇总（**未限定**字段，如「统计今日大盘」「汇总市场数据」） | **写入**（自动整理） | **完整复盘** | 用户明确「只看已保存 / 不要联网 / 不要写入」→ 只读 |
| 统计 / 汇总 + **点名字段**（如「统计上涨和下跌家数」「汇总两融」） | **写入**（自动整理） | **仅点名范围** | 同上只读改判；不要扩大到八类 |

「统计今日大盘」= 写入 + 完整复盘，不是新闻摘要任务。新闻或收盘快讯最多作旁证，**不能代替**范围内交付，也不能在首轮用其结束请求。

完整复盘，或明确请求全部涨跌停时，**必须**按行为说明执行「涨跌停四类采集执行流程」并对四类分别采集、尽量 `save-events`；用户无需再次点名个股。未给出四类各自的**采集结果与保存结果**前不得结束首轮。点名子类（如只补炸板）、单条修正或梯队扩展**不**扩大为四类全量，仅处理点名范围及必要依赖（见行为说明「流程适用范围」）。

## 读库失败

`get` 或任意写入 CLI 返回 `ok=false` 且 `error.code=DB_UNAVAILABLE`（或等价的「unable to open database file」）时：

- 明示：**数据库状态 = 未知 / 不可用**；给出 CLI 错误原文或 `message`。
- **不要**根据失败推断「库内缺项」「库内已有」「无复盘数据」；不得使用 `missing_fields`（本次未读到）。
- **不要**因此改写已判定的请求范围（只读仍只读；写入仍按原范围继续采集与校验）。
- 只读请求：说明无法展示账本后结束；可提示检查 `MARKETREVIEW_HOME` / `~/.marketreview` 权限或沙箱限制。
- 写入请求：继续范围内取数；采集结果与数据库状态分开说明；能写入则写入，仍失败则保留已核验候选并报告库不可用。

采集状态与数据库状态**可同时成立、非互斥**，细则见 `references/Skill行为说明.md`。

## 两个核心功能

### 1. 写入

- 理解用户请求范围（完整复盘、涨跌停事件、每日梯队扩展，或点名字段/类别）
- 通过网页、图片、API 或用户文字取得数据；核验、重试与换源见行为说明
- **涨跌停**：存个股事件，不存汇总数；完整复盘走四类采集，点名子类不扩范围（见行为说明）；写入前确定可信 `streak_height`；ST 过滤是义务不是停采条件
- 完成业务校验后调用 CLI 写入
- 事件扩展只能关联已保存的涨跌停事件；`limit_up_reasons` 只允许 `direction=up`
- 三只指数（上证、深证成指、创业板指）可通过内置腾讯日 K 自动补齐
- 首轮交付：对照请求范围报告已确认项；涨跌停须按适用类分别报告**采集结果与保存结果**；**仅当存在未完成项或写入失败时**列出未完成项并明确「未完成」；范围内均已核验写入成功时可报告该范围已完成

### 2. 读取展示

- 调用 `get` 始终读取原子字段、事件、统计摘要、缺失字段和 `ladder`
- 按下方规则格式化；只看总体复盘时可忽略 `ladder`
- 读取时不自动取数、不修改数据库、不追问缺失项

## 展示规则

系统**不保存、不推断**涨跌停名单是否采全。因此 `summary` 中由事件派生的指标（有效涨停、炸板、收盘跌停、炸板率、涨跌停比、连板等）一律按「**库内已保存事件的派生统计**」展示，**不得**暗示「全市场已核验」或「该方向已采全」。

`review` 为 `null` 且 `events` 为空时，显示「无复盘数据」，到此结束。不要把这种情况渲染成各指标为 `0`。表述为**库内无记录**，不要写成「已确认全市场无涨跌停」——后者只属于本轮写入路径已核验零事件后的说明。

有事件但没有总体复盘行时，原子字段留空，仍展示 `summary` 中由事件得到的涨跌停与连板指标，并标明「库内事件派生（完整性未知）」。

`review` 中的 `null` 单元格留空，不显示 `—`、`0` 或其他占位符。已确认的数值为 `0` 时必须显示 `0`（仅适用于 `review` 原子字段本身为 0，不适用于「某方向库内无事件」被读成 0 却当成全市场结论）。

`events` 为空时：`summary` 里由事件派生的数量在数值上为 `0`，展示时必须标明「库内无涨跌停事件」，**不得**单独说成「有效涨停 0、炸板 0」并暗示已核验零事件。

`events` **非空**时：仍只是库内子集统计。某方向在库内没有对应事件时，该方向派生为 `0` 只能表述为「库内该方向无事件」，**不得**写成「收盘跌停 0 / 炸板率 0% / 涨跌停比 1:0」并暗示全市场该方向已核验为空或已采全。本轮写入若只保存了可核验子集或部分方向，须在说明中写「部分采集 / 按方向未完整」，与只读展示口径一致。

本轮写入路径已核验并确认无触板事件时，在完成说明中写「涨跌停：已核验零事件」，与「未采集」「库内无事件」「部分采集」区分。

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

## 云端配置（接入 Supabase 时）

用户已确认：日常是整理完数据后写入 Supabase。历史账本还在本地，正式迁移前 `CLOUD_DEFAULT_ENABLED` 仍关闭，所以当前进程默认还是 SQLite，可不填云端凭证。现在打开开关会把新的一天写进还没接上旧数据的云端。选用 `backend=supabase` 时如果缺 URL 或 Secret，报错停止，不打开 SQLite。显式 `--backend sqlite` 不请求云端。`--db` 不能把默认后端改成 SQLite。

首次使用云端能力（`sync` 或 `backend=supabase`）且本机缺少配置文件时，CLI 会**自动**从 Skill 目录复制模板到 `~/.marketreview/`（已有文件绝不覆盖），并报错提示需填写的字段。Agent 不得代写真实密钥进仓库或对话。也可手工执行：

```bash
mkdir -p ~/.marketreview
[ -e ~/.marketreview/config ] || cp "<skill-dir>/config/marketreview.config.example" ~/.marketreview/config
[ -e ~/.marketreview/supabase.secret ] || cp "<skill-dir>/config/supabase.secret.example" ~/.marketreview/supabase.secret
chmod 600 ~/.marketreview/supabase.secret
```

- `~/.marketreview/config`：`backend`、`supabase_url`、`supabase_publishable_key`（Publishable / 旧 anon，低权限）
- `~/.marketreview/supabase.secret`：Secret Key 单行（`sb_secret_...` / 旧 service_role），权限 600；不要写进 `config`
- 选用 `supabase` 后端时缺 URL 或 Secret 则报错停止，不回退 SQLite；显式 `--backend sqlite` 不要求云端凭证
- 仅当 `supabase.secret` 不存在时可回退旧 `supabase.config`；文件存在但无效则报错
- 填写说明以模板文件注释和 README「云端配置模板」为准
- 两机同步独立于日常 backend，不改默认配置；需要已填写 URL/Secret：

```bash
python3 "<skill-dir>/scripts/cli.py" sync push [--source <sqlite>]
python3 "<skill-dir>/scripts/cli.py" sync pull [--target <sqlite>]
python3 "<skill-dir>/scripts/cli.py" sync new-identity --source <sqlite>
```

  复制出的库仍带着原账本身份。在它独立写入前必须执行 `sync new-identity`；基线损坏时不会改身份，也不会当成首次接入。只移动路径不需要新身份。

  冲突选择：`--keep-cloud` / `--adopt-local`；本地删除：`--restore-cloud` / `--delete-on-cloud`。组引用形如 `review:2026-08-21` 或 `event:2026-08-21:sh:600519`。不要用日常 save 命令循环搬运数据。
- 云端库正式备份（管理连接 / 数据库密码，非 Secret）：`python3 "<skill-dir>/scripts/pg_backup.py" backup|restore-blank|verify`；产物在 `~/.marketreview/backups/supabase/`，须含 schema 与 public wrappers。没有定时任务。每个有写入的交易日结束后手动导出；长假前、迁移前、schema 升级前再导出一份。保留最近 30 份和每月最后一份。迁移前加 `--keep-long-term`，修剪时不删除。导出失败不覆盖上一份有效备份。两机原库的一致性备份另行长期登记，不放进这个修剪目录。日常目标是整理后写入 Supabase；历史账本迁移完成前不要改默认后端。
- 方向替换超时后执行 `verify-pending`；不得在本机待核验未关闭时换机续写

## 边界

- 写入和显示相互独立
- 有效涨停、炸板、连板等派生值由 `get` 返回的 `summary` 统计，不单独入库
- 每日梯队、竞价占比和开盘涨幅由 `get` 返回的 `ladder` 生成，不单独入库
- 来源、网页链接、图片和采集方式不入库
- 多 Agent 共享 `~/.marketreview/market_review.sqlite3`；SQLite 已启用 WAL、busy timeout 和 foreign keys
- 云端凭证只存在于本机 `~/.marketreview/` 配置与 Secret 文件，不进仓库、发布包、日志或测试夹具
