# Supabase 环境实测记录

## 2026-10-02 合同第 5 项：约一百条批量写入与限流

未改 `CLOUD_DEFAULT_ENABLED`，未连接生产项目。隔离 PostgreSQL 17.11（`/tmp/dmr-pg-rpc-v1`，端口 55432）上 `test_hundred_event_batch_updates_identity_and_keeps_details` 通过：一次 `marketreview_save_events` 写入 100 条；按唯一键把 `000001` 改名后 `created_at` 仍是原批次、`updated_at` 是新批次；原明细和板块还在；同一批重复身份返回 `DUPLICATE_IDENTITY`，条数和明细不变。测完已停止该实例。

写入遇到 HTTP 429 记为 `REMOTE_RESULT_UNKNOWN`，不能当成已回滚，也不能立刻换一份新请求重试。读取遇到 429 仍是 `REMOTE_UNAVAILABLE`。回归：`tests/test_storage.py` 的 HTTP 错误用例。全套 `python3 -m unittest discover -s tests -p 'test_*.py'` 在该隔离实例上 209 项通过。

## 2026-10-02 合同第 5 项：#11 独立副本共用身份

未改 `CLOUD_DEFAULT_ENABLED`，未读生产 SQLite，未改云端。#8/#9/#11 仍不关闭。

同一状态目录里，第二份仍带着原 `ledger_id` 的库在任何 RPC 之前停止（`IDENTITY_MISMATCH`，不按首次接入重建）。只改路径仍用原身份。换一份状态目录（另一台机器只拿走数据库）同样停止。`sync new-identity --source` 在基线可读时换成新身份并清掉安装绑定；基线损坏则身份不变。回归：`tests/test_sync.py` 32 项通过。

把状态目录和数据库一起复制到另一台机器，本地仍看不到「两份同时活着」。这条不靠云端协议补，本轮没有改冻结合同。

## 2026-10-02 合同第 5 项：#11 同步完整快照 >1000

Docker 守护进程未启动。用隔离数据目录 `/tmp/dmr-pg-rpc-v1`、PostgreSQL **17.11**、端口 55432 跑 `tests/test_pg_rpc_v1.py`（`psql`，无 psycopg）。`test_sync_snapshot_over_1000_is_one_complete_jsonb` 通过：一次 `marketreview_sync_snapshot` 返回 `complete=true`、1201 条事件、代码从 `000001` 到 `001201`、两条原因按 `position` 为「先」「后」，计数与数组长度一致。同文件其余 RPC 测试一并通过。测完已停止该临时实例。这不是线上 Data API 列表容量，也不代替云端再测一遍。

## 2026-10-02 合同第 5 项：同一数据集 SQLite / Supabase 对照

同一黄金夹具 `tests/fixtures/golden_2026_08_21.json`，加上前一交易日 `2026-08-20` 的一条涨停。SQLite 走 `MarketReviewRepository`，云端形状走隔离 PG17.11 的 `marketreview_get_day` 和 `SupabaseRepository`。两边的 `review`、事件、明细、`summary`、`missing_fields`、`ladder` 一致。测试：`test_sqlite_and_supabase_get_summary_ladder_match`。未连接生产项目。

## 2026-10-02 合同第 5 项：#9 长期迁移前清单

未改 `CLOUD_DEFAULT_ENABLED`，未打开生产 SQLite，未改写已复核的一致性备份和云端备份目录。

手动策略写进 `SKILL.md`，并与 `prune_backups` 一致：有写入的交易日结束、长假前、迁移前、schema 升级前手动导出；最近 30 份加每月最后一份；`--keep-long-term` 为 `migration-snapshot`，修剪不删除；失败不覆盖。没有定时任务。

长期登记：`~/.marketreview/acceptance-evidence/20261002T093737Z_step5_migration_register/`（`migration_register.json` 的 SHA-256 `e54cf0ac54d277045a34d01be8ba0e544d7ac13af6be4a9f5980172c725fbf9d`）。

| 对象 | 结果 |
| --- | --- |
| 云端 `20261001T124829Z`、`20261001T133739Z` | 已是 `migration-snapshot`，revision 21，`BACKUP_OK` 在 |
| M3 原库一致性副本 | 五表 31/3048/1175/1026/590；五张同步表都不存在 |
| M1 原库一致性副本 | 五表 1/89/0/0/0；五张同步表都不存在 |

两份原库副本没有账本身份、基线、覆盖标记、授权或操作结果，因为源库里就没有这些表。清单写明「尚未建立同步身份」，不把缺表当成已确认不存在的基线。以后的一致性备份会把同步表普查写进 `inventory.json`。回归：`tests/test_acceptance_migration_register.py`。

## 2026-10-02 合同第 5 项：#8 持续集成

新增 `.github/workflows/tests.yml`。推送和拉取请求时在 Ubuntu 上安装 PostgreSQL 工具，用 `initdb --auth=trust` 在临时目录启动隔离实例，端口 55432，然后执行 `python3 -m unittest discover -s tests -p 'test_*.py'`。工作流文件里没有 Secret、数据库密码、`secrets.` 或 `~/.marketreview`。`tests/test_ci_workflow.py` 锁住这些约束。

这条流水线还没有在 GitHub 上跑过，因为本轮没有推送。本地 PostgreSQL 上的事务、权限、同步幂等和 1201 条快照测试此前已经通过。

## 2026-10-01 合同第 4 项：M3 准备 + JWT + M1 第二份备份 + 真双机

本机：MacBook Pro **Mac15,6 / Apple M3 Pro**；项目 `nyscgdxrctwchbzclszt`（ACTIVE_HEALTHY）。**未**改 `CLOUD_DEFAULT_ENABLED`；**未**导入正式生产账本。

证据目录：
- 准备／JWT：`~/.marketreview/acceptance-evidence/20261001T140926Z_step4_mac_prep/`
- 真双机：`~/.marketreview/acceptance-evidence/20261002T000200Z_step4_dual_physical/`
- 暂停／恢复：`~/.marketreview/acceptance-evidence/20261002T073344Z_step4_pause_resume/`
- 缺口补齐（开发侧已齐）：`~/.marketreview/acceptance-evidence/20261002T080000Z_step4_gap_fill/`

### 独立审查结论（2026-10-02 初审）

**第 4 项当时暂不整项通过。** 未发现新的产品代码缺陷；缺口为验收证据／场景。详见 `/private/tmp/daily-market-review-step4-audit-20261002.md`。

初审已核验一致：M3 原库一致性备份；JWT 链；暂停／恢复；M3 侧同步原始 reports。

初审当时仍阻塞（现已由开发侧补齐，见下「缺口补齐交付」）：
1. M1 原 SQLite 一致性备份
2. M1 原始报告与第二份备份校验原输出
3. 双机完整数据比较
4. 真双机未知结果恢复

### 已交付（开发侧；审查已部分互核）

| 子项 | 结果 | 审查状态 |
| --- | --- | --- |
| M3 原 SQLite 一致性备份 | online backup API → `sqlite_consistency/`；五表 31/3048/1175/1026/590 | 已独立核验通过 |
| #9 第二份备份打包 | `cloud_backup_for_m1/20261001T133739Z.tar.gz` + `VERIFY_ON_M1.sh` | 传输包正确 |
| M1 第二份备份校验 | Air 原输出 12/12 OK，revision=21 | 开发侧已绑原始输出；待复审 |
| 真实 Auth JWT | 403/42501 vs 401/PGRST301 | 已独立核验通过 |
| 真物理双机（重叠／独有／冲突／显式删除／全量 pull） | 哨兵 `2099-10-01`…`04`；另有完整五表比较与未知结果演练 | 开发侧证据已齐；待复审 |
| 暂停／恢复 live | INACTIVE→540；恢复后 revision=29 | 已独立核验通过 |

### 缺口补齐交付（开发侧；待独立复审）

证据：`…/20261002T080000Z_step4_gap_fill/`。

| 缺口 | 证据 | 结果 |
| --- | --- | --- |
| M1 原库一致性备份 | `sqlite_consistency_m1/`（Air；1/89/0/0/0；`2026-09-23`） | CHECKSUMS 与 integrity 本机复核 OK |
| M1 双机原始报告 | `from_m1_dual_physical/` + 并入 `…/20261002T000200Z…/reports/m1_*.json` | Air meta；缺可选 `m1_pull.json` |
| M1 第二份备份原输出 | `reports/m1_second_backup_verify_raw.txt` | 12/12 OK，revision=21 |
| 双机完整五表比较 | `snapshots/m3|m1_after_final_pull.json` + `compare_snapshots.json` | groups/tables 哈希一致 |
| 未知结果恢复 | `unknown_result_m3.json`（29→30）+ `unknown_result_m1.json`（30→31） | 同 operation_id 恢复；稳定 push 不重复推进；哨兵已清 |
| 哨兵清理 | `cleanup_unknown_sentinel.json` | `2099-10-05/06` 清空；`2099-09-*` 样例仍在；revision=35 |

**第 4 项独立复审通过（2026-10-02），[#3](https://github.com/shenjee/daily-market-review/issues/3) 已勾选。** 初审四项缺口关闭。复审记录：`/private/tmp/daily-market-review-step4-reaudit-20261002.md`。

下一步：合同第 5 项 — 按原 #8/#9/#11 验收项逐条收口；这些 issues 尚未整体关闭，#10 未通过。

---

## 2026-10-01 合同第 3 项：PG17 客户端重导 + 隔离 PG17 恢复

本机新装 Homebrew `postgresql@17`（**17.11**）。优先路径：用 **PG17 客户端**对同一云端内容（revision=21 备份样例）经 Session pooler 重导，再恢复到本机隔离 PG17。**不是**用 18 产物试灌 17。

| 步骤 | 结果 |
| --- | --- |
| `pg_dump` 17.11 → Session pooler | **成功**；产物 `~/.marketreview/backups/supabase/20261001T133739Z/`（含合同副本 + CHECKSUMS，`revision=21`，五表样例同第 2 项） |
| 本机 `postgresql@17` 空白库 `restore-blank` + `verify` | **`revision=21` 通过** |
| 业务抽查 | reviews 2 日 pe／数量一致；18 wrappers；写回滚后 revision 回 21；anon 拒 probe |
| 清理 | 临时库已 DROP；已 `brew services stop postgresql@17` |

状态：**开发侧第 3 项已交付，待独立审查复核。** #8/#9 整体、#10 仍未通过。旁注：`20261001T133739Z.pg17_client.txt` 记录客户端版本（包外）。

---

## 2026-10-01 二次审查后：#8 P1 修复 + 第 2 项已勾选

- **第 2 项**：已通过并在 GitHub #3 勾选（`20261001T124829Z` 合同副本／校验绑定／独立 PG18 恢复）。**#9 整体仍未关**。
- **第 1 项**：完整容量证据二次审查通过；但验收脚本 P1（`--trade-date` 可删真实交易日）已修：
  - `assert_capacity_day`：仅 `2099-08-01`
  - CLI 入口、`_insert_events`、`_delete_events` 触库前均拒绝其他日期
  - SQL 硬编码 `CAPACITY_DAY`，不嵌入调用方日期参数
  - 单测 `tests/test_acceptance_capacity.py`：真实日／备份样例日拒绝且 `_psql`／密码未调用
- **#8/#9 整体、#10 仍未通过**；第 3 项见文首专节。

---

## 2026-10-01 审查缺口补齐（第 1／2 项证据）

独立审查结论：第 2 项 PG18 恢复行为通过，但第 1、2 项因证据／交付包缺口暂不整项勾选。本轮只补缺口，**未**改 `CLOUD_DEFAULT_ENABLED`，**未**发现新代码缺陷需修业务逻辑（仅补备份合同副本与容量验收证据）。

### 第 1 项 #8：完整脱敏证据 + 可复现脚本

- 脚本：`scripts/acceptance_capacity_data_api.py`（管理 SQL 写入 `2099-08-01`×1201；Data API + 适配器读取；不碰 `2099-09-*` 备份样例；默认清理容量哨兵）
- 完整证据目录：`~/.marketreview/acceptance-evidence/20261001T130257Z_issue8_capacity/`
  - `expected_keys.json`：1201 条完整预期业务键
  - `raw_list_events.json`：完整 RPC 响应（含全部 events；当日全量 list 另含备份样例共 1204）
  - `raw_business_keys.json` / `adapter_business_keys.json`：完整键列表
  - `adapter_list_events.json` / `adapter_list_events_range.json` / `adapter_get_day.json`：完整适配器输出
  - `summary.json` + `CHECKSUMS.json`
- 核验：容量子集 1201 键与预期完全一致；排序／唯一／无 1000 截断；清理后区间读 0；备份样例仍在（`2099-09-*` events=3）

### 第 2 项 #9：包内合同副本 + 绑定校验和

- 代码：`pg_backup.create_backup` 现复制 `contracts/supabase_rpc_v1.json` 入包；`manifest.contract` 记录路径；`CHECKSUMS` / `manifest.files` 绑定哈希；`_verify_checksums` 强制要求合同副本且与 18 wrappers 名集合一致
- 新交付包：`~/.marketreview/backups/supabase/20261001T124829Z/`（取代 `20261001T102555Z` 作为当前验收对象）
  - 含 `contracts/supabase_rpc_v1.json`（17590 bytes）
  - 业务五表样例与 revision=21 不变；`columns`=84；18 wrappers
- 本机 PG18：`restore-blank` + `verify` → **`revision=21` 通过**（临时库已 DROP）
- 旧包 `20261001T102555Z` 无合同副本，当前 verify 会拒绝（需用新包）

**仍不勾选整项**：待审查用新证据／新包独立复核。#8/#9 整体、#10、第 3 项 PG17 仍挂。

---

## 2026-10-01 #9 新工具云端完整备份（合同第 2 项）

本机：同日 #8 之后；项目 `nyscgdxrctwchbzclszt`；Session pooler；客户端 PostgreSQL **18.6**；凭证旧布局。**未**改 `CLOUD_DEFAULT_ENABLED`；**未**导入正式生产账本。

状态：**开发侧新备份已交付**（含可核验业务五表 + `catalog.columns` + **包内合同副本**）；本机 PG18 空白恢复 / `verify` 通过。当前验收对象：`20261001T124829Z`。**#9 整体仍未关**（PG17 回灌、M1 第二份拷贝、真双机待做）。旧包 `20261001T082039Z` 无 `columns`；`20261001T102555Z` 无合同副本——均不能作为当前完整交付口径。

### 隔离样例（dump 前写入；保留在云端供第 3 项同内容重导）

经真实 Data API + `SupabaseRepository`（非空表冒充）：

| 表 | 行数 | 可核验样例 |
| --- | --- | --- |
| reviews | 2 | `2099-09-01` pe_sh=12.5 / pe_sz=11.0 / advancing=123；`2099-09-02` pe_sh=9.9 |
| events | 3 | `(2099-09-01,sh,600519,up)` streak=2；`(…,sz,000001,down)`；`(2099-09-02,bj,830799,up)` 3000bp |
| details | 3 | note=`backup-sample-#9`；is_leader true/false/null |
| sectors | 4 | 白酒/消费、银行、专精特新（含 position 顺序） |
| reasons | 3 | 业绩预增/板块联动、北交所样例 |

写后 `revision=21`。

### 产品化 dump（最新 `scripts/pg_backup.py`）

| 项 | 结果 |
| --- | --- |
| 路径 | Session pooler `aws-0-ap-southeast-1.pooler.supabase.com:5432` / `postgres.<ref>` |
| 产物（当前） | `~/.marketreview/backups/supabase/20261001T124829Z/`（`--keep-long-term`） |
| 包内 | `marketreview.sql` + **`public_functions.sql`（18 wrappers）** + `roles.sql` + `migrations/`×3 + **`contracts/supabase_rpc_v1.json`** + snapshot/catalog/functions + `BACKUP_OK` + `CHECKSUMS` |
| catalog | **含 `columns`（84 列）**；constraints 已过滤 PG18 `contype='n'` |
| row_counts | reviews=2，events=3，details=3，sectors=4，reasons=3，history=71，sync_commit_result=6，`revision=21` |
| 合同 | 包内副本 + manifest/CHECKSUMS 绑定；校验时与 18 wrappers 名集合一致 |

### 本机空白库恢复（实测）

- `20261001T124829Z`：`restore-blank` → `verify` → **`revision=21` 通过**（临时库已 DROP）
- 五行业务值 / 列表顺序 / null / bool 与样例一致；18 wrappers 可调；权限与写回滚此前已由独立审查对同内容包复核通过
- 测后已停止本机 `postgresql@18`

### #9 仍挂（本轮不关）

- 第 3 项：PG17 客户端对**同一云端内容**重导再恢复（本包为 18 客户端产物，回灌 17 只算兼容探测）
- M1 第二份备份拷贝与校验和；真物理双机
- 独立审查对**含合同副本**新包的完整交付复核

---

## 2026-10-01 #8 线上容量（真实 Data API ≥1201）

本机：同日 #4/#9/#11 所用 Mac / 项目 `nyscgdxrctwchbzclszt`（ap-southeast-1，PG **17.6**，ACTIVE_HEALTHY）。凭证仍走旧布局 `~/.marketreview/supabase.config`。**未**改 `CLOUD_DEFAULT_ENABLED`；**未**触碰日常 `market_review.sqlite3`。

状态：**开发侧交付完成（含完整键／响应证据与可复现脚本），待独立审查复核后才能勾 #8 总通过。** 本机 PG SQL 1201 单测不能代替本条。**#9/#10 仍挂。**

### 构造（管理连接，非验收读路径）

- 哨兵交易日：`2099-08-01`（不碰 `2099-09-*` 备份样例）
- `INSERT … generate_series(1, 1201)` → `marketreview.daily_price_limit_event`
- 业务键：`(2099-08-01, sh, 000001…001201, up)`；`closed_at_limit=1`，`limit_rate_bp=1000`，`streak_height=1`
- 可复现：`python3 scripts/acceptance_capacity_data_api.py`

### 读取（真实 Data API + 产品适配器）

路径：`UrllibRpcTransport` → `SupabaseRepository.list_price_limit_events` / `read_day`。

| 检查 | 结果 |
| --- | --- |
| 原始 RPC `complete` | `true` |
| 容量日事件 | **1201**（全量 list 另含备份样例时为 1204；区间／get_day 恰 1201） |
| 完整业务键 | 与 `expected_keys.json` 完全一致；排序／唯一通过 |
| 相对 PostgREST 默认 1000 行上限 | **未截断** |
| 墙钟（最近一次） | raw ~3.2 s；adapter list ~1.8 s；range ~1.7 s；get_day ~2.7 s |

完整脱敏证据：`~/.marketreview/acceptance-evidence/20261001T130257Z_issue8_capacity/`（含完整 RPC／适配器输出与 1201 键，不只样例）。

### 清理

- 仅删除 `2099-08-01` 容量哨兵；备份样例 `2099-09-*` 保留
- 区间复读容量日 events=0

### 边界

- 写入用管理 SQL；**验收读取走真实 Data API**。
- 开发交付 ≠ 审查通过。

---

## 2026-10-01 #9 备份产品化 + 真实业务数据恢复 / 迁移演练（旧包；已被新包取代验收口径）

本机：同日 #4/#11 所用 Mac / 项目 `nyscgdxrctwchbzclszt`（ap-southeast-1，PG **17.6**，ACTIVE_HEALTHY）。凭证仍走旧布局 `~/.marketreview/supabase.config`；Session pooler 备份；本机客户端 PostgreSQL **18.6**。**未**改 `CLOUD_DEFAULT_ENABLED`（仍为 False）；**未**把日常 `market_review.sqlite3` 设为云端默认库。

状态：**旧包 `20261001T082039Z` 缺 `catalog.columns`；`20261001T102555Z` 缺合同副本；当前以 `20261001T124829Z` 为准。** **#9 整体仍未关**（PG17 / M1 / 真双机）。**#10 未通过**。

复核补充：独立审查复跑备份校验 / PG18 空白恢复 / 权限与写回滚 / 原操作证据恢复 / 临时 SQLite pull，原样恢复库无异常；问题在 `verify_restore` 漏检。补丁后：`anon`/`authenticated` 对全部业务与基础设施表的 SELECT/INSERT/UPDATE/DELETE 及合同内全部 public wrappers 的 EXECUTE 均须拒绝；catalog 增加 `attnotnull` 列清单（旧备份无 `columns` 时要求重新导出）。负向单测覆盖「给 anon 开事件表读」「DROP updated_at NOT NULL」两种漂移。日常读取 `_optional_number` 将 jsonb 整值还原为 Python `float`；CONTRIBUTING 发布树补齐备份运行依赖。

### 代码缺口（同日修复）

- Session pooler 用户 `postgres.<project_ref>`：原 `_validate_ident` 拒点号 → 新增 `_validate_user`。
- `verify_restore`：dump/restore 经 float8 文本往返后 jsonb 浮点可差 1 ULP → 快照比对改用 `same_json_value` 浮点容差。
- catalog：PG 18 把 NOT NULL 记为 `pg_constraint.contype='n'`，与云端 17 dump 的约束清单不一致 → `_catalog` 忽略 `contype='n'`，**并另存 `attnotnull` 列清单**（独立审查 P2：仅过滤约束名会漏检 DROP NOT NULL）。
- `_assert_role_denied`：独立审查 P2 后改为核验全部 `ALL_TABLES` 的 SELECT/INSERT/UPDATE/DELETE，以及合同内全部 `public.marketreview_*` 的 EXECUTE（不再只查 probe + 复盘表）。
- `_BIN_CANDIDATES` 补 `postgresql@18`。

### 产品化备份（实测）

数据：从日常 SQLite 抽取真实交易日子集（复盘 3 日 + 每日期限 12 只事件代码）经 `sync push` 写入云端（`revision` 12→13；约 39 组 / ~4 s）。

| 步骤 | 结果 |
| --- | --- |
| `scripts/pg_backup.py backup` ← Session pooler | **成功**（~18 s）；产物 `~/.marketreview/backups/supabase/20261001T082039Z/`（`--keep-long-term`） |
| 包内对象 | `marketreview.sql` + **`public_functions.sql`（18 个 `marketreview_*` wrappers）** + `roles.sql`（含 postgres stub）+ migrations + snapshot/catalog/functions + `BACKUP_OK` + CHECKSUMS |
| 行数（摘录） | reviews=3，events=36，details=18，history=53，sync_commit_result=4，`revision=13` |

相对 #4 仅 `--schema=marketreview` 的验收 dump：**本轮正式产品化路径已含 public wrappers**。

### 本机空白库恢复（实测）

- Homebrew `postgresql@18` 空白库 `marketreview_backup_restore2`；`restore-blank` → `verify` → **`revision=13` 通过**。
- 核验：业务五行与基础设施表行数一致；18 个 public wrappers 可调用；`anon`/`authenticated` 拒绝 probe / 读表；事务内 `save_review`（哨兵 `2099-03-01`）后 `ROLLBACK`，行数与 revision 回到 13。
- 测后已 `DROP` 恢复库并停止本机 PG 18。

### 双账本迁移演练（同 Mac；真实日期）

隔离账本：`~/.marketreview/backup-drill-acceptance/20261001T0815Z/machine_{a,b}.sqlite3`。

| 步骤 | 结果 |
| --- | --- |
| 机 A 已上传（见上） | `revision=13` |
| 机 B push（独有日 `2026-09-22` + 重叠 `2026-09-28` 冲突 pe_sh null↔99） | `partial`，`revision=14`；冲突组列出双方选项 |
| 机 B `--adopt-local review:2026-09-28` | `partial`，`revision=15` |
| 机 A / B `sync pull` | 均 `completed`，`revision=15` |
| 核对 | 两边 `read_local_groups` **`same_json_value` 完全一致**（48 组；重叠日 pe_sh=99.0） |

清理：MCP 删除演练交易日业务行；事后 reviews=0、events=0、details=0；`revision=15`（历史/sync_commit_result 验收痕迹保留）。临时 SQLite / 备份目录在本机 `~/.marketreview/`，未入库。

### 版本边界（#9）

- **18 客户端 dump 17 服务端 → 恢复到本机 18**：**已实测通过**（产品化路径）。
- **同一 dump 回灌云端 17**：**未验证**；官方不把「新工具 dump → 恢复进旧大版本」列为支持路径。若需要回灌，须另测或改用 17 客户端重导。

### #9 整体仍挂 / #10 仍挂（本轮不关闭）

**#9 整体（Mac 侧之外）**

- M1：产品化备份目录第二份拷贝到同路径
- 真物理双机各配日常环境的迁移演练（同机双账本不替代）

**#10**

- 前置：#8 总验收（若仍有未勾项）、#9 整体、#11（同机 live 已齐）
- 真实暂停 → 恢复
- 真实 Auth `authenticated` JWT（非伪造）
- M1 Agent 路径冒烟
- 迁移前正式 SQLite 登记备份 → 全量接入 → 两边 `backend=supabase`
- 最后才允许 `CLOUD_DEFAULT_ENABLED=True` / 默认切 supabase

---

## 2026-10-01 #11 两机同步 Live 冒烟（同 Mac 双账本 × 真实 Supabase）

本机：同日 #4 所用 Mac / 项目 `nyscgdxrctwchbzclszt`（ap-southeast-1，ACTIVE_HEALTHY）。凭证仍走旧布局 `~/.marketreview/supabase.config`。用两个隔离 SQLite 账本模拟两机（`~/.marketreview/sync-live-acceptance/20261001T153857Z/machine_{a,b}.sqlite3`），共享生产闸门目录 `~/.marketreview/supabase-state/`（顺序执行，无并发）。哨兵交易日 `2099-02-01` / `02` / `03`；**未**触碰日常 `market_review.sqlite3`，**未**改 `CLOUD_DEFAULT_ENABLED`。

状态：**#11 上传合并 + 全量下载 Mac live 冒烟通过**；过程中发现并修复「PG jsonb 整值 float → JSON int」导致 push 收尾证据比对失败的缺陷。真物理双机仍可挂 #10；**#9 备份产品化见上方同日专节（已通过 Mac 侧）**。

### 发现与修复（代码，同日）

首次 `sync push`（机 A）云端已提交（`revision` 9→10，业务行写入成功），但本机报 `status=unknown`：`本机源快照与云端提交结果不一致，未推进基线`，`pending-write.json` 保持 `open`。根因：`marketreview.review_json` / `jsonb_build_object` 把整值 `double precision`（如 `pe_sh=11.0`）编成 JSON 整数 `11`；本机源快照仍为 Python `float`；`same_json_value` 刻意区分 int/float，证据比对失败。快照读路径本已有 `_review_from_mapping` 归一化，**commit `result_payload` 未走同一恢复**。

修复：`normalize_public_group(s)` + `same_evidence` / `_finish_push_result` 在比对与云端证据恢复前恢复 float；单测 `test_pg_jsonb_whole_float_as_int_still_confirms_push`。重跑机 A push → `resumed=true` / `completed`，闸门关闭。

### 演练步骤与结果（实测）

| 步骤 | 墙钟 | 结果 |
| --- | --- | --- |
| 机 A 首次 push（3 组：两复盘+一事件） | ~4.2 s | 云端成功；本机 unknown（修复前） |
| 机 A 修复后 resume push | ~1.4 s | `completed`，`revision=10`，`resumed=true` |
| 机 B push（独有日 + 重叠冲突） | ~2.5 s | `partial`：提交 `review:2099-02-02`→`revision=11`；冲突 `review:2099-02-03`（本地 pe=9 / 云端 pe=1）；A 独有组列待下载 |
| 机 B `--adopt-local review:2099-02-03` | ~2.2 s | `partial`：冲突组提交→`revision=12`；仍列 A 侧待下载 |
| 机 A `sync pull` | ~1.0 s | `completed`，`revision=12`，4 组 |
| 机 B `sync pull` | ~1.0 s | `completed`，`revision=12`，4 组 |

核对：两机 `read_local_groups` **完全一致**（`same_json_value`）；重叠日 pe_sh=9.0（采用 B）；含事件 `600519`。`pending` 最终 `closed`。

清理：MCP 删除 `trade_date LIKE '2099-%'` 业务行；事后 reviews=0、events=0；`revision=12`；`sync_commit_result=3`；`group_change_history=14`（验收痕迹保留，无业务残留）。临时 SQLite 与 pull 备份留在本机 `~/.marketreview/` 下，未入库。

### 边界说明

- 本轮是**同机双账本**对真实 Data API 的协议冒烟，不是两台物理电脑各配一套日常环境。
- 共享 `supabase-state` 时，一账本 open pending 会挡住另一账本（`IDENTITY_MISMATCH`），符合闸门设计；须先收尾再换账本。
- #9 带业务数据的产品化 dump/restore 与同机迁移演练见上方专节（**Mac 侧可通过**）；M1 第二份备份、真物理双机与 #10 默认后端切换仍挂账。

---

## 2026-10-01 当前 Mac 在线验收（#4）

本机：MacBook Pro（`Mac15,6`，Apple M3 Pro，18 GB）。默认路由 `en0`，网关 `172.20.10.1`（典型个人热点段）。项目 `nyscgdxrctwchbzclszt`，地域 **ap-southeast-1**，状态 **ACTIVE_HEALTHY**，PostgreSQL 17.6。凭证仍由本机旧布局 `~/.marketreview/supabase.config` 加载（尚无新布局 `config` / `supabase.secret`）；密钥未输出、未写入仓库。客户端超时配置：探针与写测使用 30 s；仓库 `UrllibRpcTransport` 默认亦为 30 s。

状态：**Mac 核心通过**（鉴权拒绝、成功读写延迟、Data API 语句超时上限 **8s**、Session pooler dump、本机 PG 18 空白库 restore）；**环境尾巴挂 #10**（暂停/恢复、真实 authenticated JWT、M1）。未改 `CLOUD_DEFAULT_ENABLED`，未导入正式数据，未切换默认后端。#7 在途等待窗口已按本记录 8s 上限对齐。#11 同日另见上方 live 冒烟节。

### 1. 鉴权拒绝矩阵（实测）

Data API `POST /rest/v1/rpc/marketreview_probe`，请求体 `{"p_request":{"schema_version":1}}`：

| 身份 | HTTP | 错误体（脱敏） | 耗时 |
| --- | --- | --- | --- |
| Publishable 作 `apikey` | 401 | `{"code":"42501",...,"message":"permission denied for function marketreview_probe"}` | ~1753 ms |
| Publishable + `Authorization: Bearer <publishable>` | 401 | 同上 `42501` | ~632 ms |
| 无效 `sb_secret_…` | 401 | `{"message":"Invalid API key","hint":"Double check your API key."}` | ~379 ms |
| 空 `apikey` | 401 | `{"message":"No API key found in request",...}` | ~464 ms |
| Publishable + 伪造 `role=authenticated` JWT | 401 | `{"code":"PGRST301",...,"message":"No suitable key or wrong key type"}` | ~499 ms |
| 有效 Secret Key | 200 | 成功信封（见下） | 见延迟节 |

库内权限（MCP `execute_sql`，**实测**）：`anon` / `authenticated` 对 `public.marketreview_probe(jsonb)` 的 `EXECUTE` 均为 `false`；`service_role` 为 `true`。

**未验证**：真实 Auth 登录后的 `authenticated` 用户 JWT 经 Data API 访问（本机无可用用户会话；伪造 JWT 仅证明网关验签拒绝，不替代真实会话）。

### 2. 成功读 / 临时写延迟（实测）

**只读 `marketreview_probe`（Secret）**：warmup 成功后连续 12 次，超时 0 次。

- 样本 ms：471.35, 1043.55, 1106.19, 1138.61, 1145.78, 1192.78, 1286.49, 1412.38, 1491.21, 1585.68, 1695.03, 3098.48
- min / p50 / p95 / max / mean：471.35 / 1239.63 / 2326.58 / 3098.48 / 1388.96
- 返回：`format_version=1`、`schema_version=1`、`complete=true`、`ledger_key=main`（写测前 `revision=0`）

**临时写**（哨兵日 `2099-01-02`，非正式数据）：`save_review` / `save_events` / `get_day` 成功；5 次覆盖写样本超时 0 次。

- 写样本 ms：471.19, 634.49, 736.77, 1131.96, 1533.83 → min / p50 / p95 / max / mean：471.19 / 736.77 / 1453.46 / 1533.83 / 901.65
- 清理：`delete_price_limit_events` + `delete_review` 后 `get_day` 确认 review/events/details 为空
- 事后库内计数（MCP）：reviews=0、events=0、details=0；`ledger.revision=9`（写测推进，无业务行残留）

### 3. 服务端语句超时上限（实测 + 配置对照）

**配置（MCP 读 `pg_settings` / `pg_roles`）**：

- 库级 `statement_timeout` = `120000` ms（2 min）
- `anon.rolconfig`：`statement_timeout=3s`
- `authenticated.rolconfig`：`statement_timeout=8s`
- `authenticator.rolconfig`：`statement_timeout=8s`、`lock_timeout=8s`
- `service_role.rolconfig`：空

**Data API + Secret Key 有效上限（实测，关键）**：临时函数 `marketreview_acceptance_sleep`（测后已 `DROP`）经同一 Secret 路径调用，响应内 `statement_timeout` 显示为 **`8s`**（与 `authenticator` 会话设置一致，而非库级 2 min）。

| `seconds` | 结果 | 墙钟 ms | 错误 |
| --- | --- | --- | --- |
| 1 | 成功 | ~2645 | — |
| 7 | 成功 | ~8035 | — |
| 9 | 失败 | ~11331 | HTTP 500，`code=57014`，`canceling statement due to statement timeout` |
| 12 | 失败 | ~8833 | 同上 |

结论（供 #7 在途等待窗口）：**日常 Data API（Secret）语句超时上限按 8 秒计**；超时表现为 SQLSTATE `57014`，不是客户端 30 s 先到。库级 2 min 不代表该 HTTP 路径。Publishable 调用该临时函数亦为 `42501`（与探针一致）。客户端常量 `DATA_API_STATEMENT_TIMEOUT_SECONDS = 8.0`；前像仍一致且已过该窗口后，`verify-pending` 可按设计 close → reopen → 重发。

### 4. 管理 / 备份连接（实测；暂停见 §5）

本机密码文件：`~/.marketreview/db.password`（mode 600，单行；未写入仓库、未回显）。

| 路径 | 结果 | 备注 |
| --- | --- | --- |
| Session pooler `aws-0-ap-southeast-1.pooler.supabase.com:5432`，用户 `postgres.<project_ref>` | **认证成功**（~765 ms；`postgres\|17.6`） | 备份应走此模式 |
| 同主机 transaction `…:6543` | 认证亦可成功（对比用） | **不用**于正式 dump（设计禁止 transaction pooler） |
| Session `aws-1-ap-southeast-1…` | 失败 `ENOTFOUND` tenant/user | 非本项目 pooler 节点 |
| 直连 `db.<ref>.supabase.co` | 本轮 DNS 解析失败（无 A/AAAA） | 此前热点网络下曾见仅 IPv6 TCP；直连仍不稳定/不可用 |
| 本机曾用 `pg_dump` 16.15 | **失败** | `server version mismatch`（客户端大版本不得低于服务端） |

**本机客户端已改为 PostgreSQL 18.6**（Homebrew `postgresql@18`；PATH 中 `pg_dump`/`psql`/`pg_restore` 均为 18.6）。云端仍为 17.6。

| 操作 | 结果 |
| --- | --- |
| `pg_dump` 18.6 → Session pooler → `--schema=marketreview` 自定义格式（`-Fc`） | **成功**（~14.7 s，约 160 KB；TOC 187 条；含 ledger / probe / daily_market_review） |
| 同路径 `--schema-only` 纯 SQL | **成功**（~11.7 s，约 143 KB；含 probe/ledger） |
| 产物目录（仓库外） | `~/.marketreview/backups/supabase/_acceptance_20261001T031716Z/`（含 `.sha256`） |
| 本机空白库 `pg_restore` | **成功**（见下） |

**本机空白库恢复演练（实测，2026-10-01 同日收尾）**：

- 启动 Homebrew `postgresql@18`（数据目录 `/opt/homebrew/var/postgresql@18`；本机超级用户 `jishen`）。
- SHA-256 校验 `marketreview.dump` 通过后，新建空白库 `marketreview_acceptance_restore`。
- 为云端 dump 的 ACL / `DEFAULT PRIVILEGES FOR ROLE postgres` 准备本地 **NOLOGIN** 角色 stub：`postgres`、`anon`、`authenticated`、`service_role`（无密码）。无 `postgres` stub 时 restore 会在末尾 DEFAULT ACL 失败。
- `pg_restore --no-owner --exit-on-error`（**保留 ACL**）→ exit 0。
- 核验：schema `marketreview`；9 表齐全；业务五表行数 0；`schema_meta.schema_version=1`；`ledger.revision=9`；`group_change_history=9`（与写测推进一致）；关键函数 13 个均在；函数总数 68；`marketreview.probe` 返回 `complete=true` / `revision=9`。
- 读/写/回滚：事务内 `save_review`（哨兵日 `2099-01-03`）成功且 `revision→10`，`ROLLBACK` 后行数与 revision 均回到 9。
- 授权：`anon`/`authenticated` 无 schema USAGE，SELECT / `probe` EXECUTE 拒绝；`service_role` 可 USAGE + `probe`。
- 边界：该验收 dump 为 `--schema=marketreview`，**不含** `public.marketreview_*` 包装函数（预期；正式产品化备份须另含 public wrappers，见设计 §7 / `pg_backup.py`）。
- 清理：已 `DROP DATABASE marketreview_acceptance_restore`，并停止本机 PG 18 服务。角色 stub 留在本机集群（无密钥），便于日后 restore。
- 工具缺口（验收当时手工绕过）：`ROLES_SQL` 原未创建 `postgres` stub；已在 `pg_backup.py` 吸收。#4 环境项（真实 authenticated JWT / 暂停 / M1）挂 #10；#9 完整业务恢复见文首专节。

**版本边界（官方依据，已写入本决策）**：

- 用 **18 的 `pg_dump` 导出 17 服务端：官方支持**（dump 程序可读回至 9.2 的服务端）。
- 该备份用于恢复到 **本地 PostgreSQL 18**：符合官方「新工具导出旧库 → 恢复进新库」升级路径；**本轮已实测通过**。
- 该备份用于 **直接恢复回云端 17**：**未验证**，且官方升级文档不把「新工具 dump → 恢复进旧大版本」列为支持路径。若将来要以 dump 回灌 Supabase 17，须另做 17 目标恢复实测，或改用 17 客户端重导。

### 5. 暂停 / 恢复

项目本次始终为 **ACTIVE_HEALTHY**，**未遇到**真实暂停 → 标 **待验证**（不伪造）。

### 6. 其它边界

- M1 Agent 环境：仍 **待验证**（须在 #10 前补齐）。
- 本机配置迁移到新模板布局：可选，不影响本次探针（旧布局回退仍可用）。

---

## 2026-09-30 当前 Mac 审查实测

### 迁移部署后（同日晚间）

状态：**探针已通；#4 其余验收项仍未完成。**（后续见上方 2026-10-01 节。）

- 通过 Supabase MCP `apply_migration` 按序应用：`marketreview_v1_schema`、`marketreview_v1_rpc`、`marketreview_v1_sync`（对应仓库 `sql/migrations/0001`–`0003`）。
- 项目 `nyscgdxrctwchbzclszt`，控制台地域 **ap-southeast-1**，状态 ACTIVE_HEALTHY；引擎 PostgreSQL 17.6。
- 库内确认存在 `marketreview.probe` 与 `public.marketreview_probe`；`schema_version=1`，`revision=0`；业务表空。
- 本机 `UrllibRpcTransport` + Secret Key `apikey`，只读 `marketreview_probe`（`{"schema_version": 1}`，超时 5 秒）**成功**：耗时 2657.83 ms，返回 `format_version=1`、`schema_version=1`、`complete=true`、`revision=0`、`ledger_key=main`。项目 URL 与设计一致。凭证未输出、未写入仓库。
- 该次为成功读样本 1 条，尚不足以计算 p50/p95；未做写入、鉴权矩阵、超时上限、暂停/恢复、管理/备份连接实测。

### 迁移部署前（同日）

状态：**当时探针检查失败（HTTP 404）。**

- 原因事后确认：云端尚未部署 RPC（Functions 为空），不是项目本身不可用。
- 首次失败耗时 1327.55 ms，客户端 `REMOTE_UNAVAILABLE`；已排除 `SSLCertVerificationError`。失败耗时不作成功延迟样本。

仍待实测（截至 09-30 晚）：当前 Mac 完整型号与网络环境、有效/无效 Secret Key 与 anon/authenticated 拒绝、成功读写多样本延迟与超时次数、服务端语句超时上限、真实暂停/恢复、管理连接、备份连接及恢复演练。M1 验证仍须在 #10 前补齐。

本地隔离 PostgreSQL 的函数、权限与事务测试结果不替代上述在线验收。
