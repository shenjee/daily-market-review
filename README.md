# Daily Market Review

每日资本市场总体复盘 Skill，用于整理、保存和查看指定交易日的市场复盘数据。

## 功能

- 整理、补充和修正市场宽度、指数、成交额、市值、估值及两融等复盘字段
- 保存涨停、跌停、炸板和连板事件
- 保存事件扩展信息，并按连板高度生成每日梯队视图
- 读取已保存的数据并生成统计摘要
- 多个 Agent 在同一系统用户下可共享同一份用户数据

本 Skill 只提供数据整理与复盘，不提供买卖建议。

## 系统要求

- Python 3.11 或更高版本
- 支持本地 Skill 的 Agent

运行仅使用 Python 标准库，无需安装第三方 Python 依赖。

## 安装

本 Skill 提供两种安装方式，任选其一即可。安装完成后不需要另外克隆或保存源码仓库。

项目地址：[shenjee/daily-market-review](https://github.com/shenjee/daily-market-review)

同一系统用户下的不同 Agent 需要分别安装一份 Skill，但默认共享 `~/.marketreview/` 中的用户数据（见下文「用户数据」）。如果不同 Agent 设置了不同的 `MARKETREVIEW_HOME`，则不会共享。

### 方式一：让 Agent 安装（推荐）

把上面的 GitHub 链接发给 Agent，并说明要安装本 Skill。例如：

- 「请安装这个 Skill：https://github.com/shenjee/daily-market-review」
- 「帮我安装 daily-market-review 市场复盘 Skill」

支持从 GitHub 安装 Skill 的 Agent，会将其安装到该平台配置的 Skill 目录。安装完成后，可直接开始使用（见下文「使用」）。

### 方式二：手动安装

如果 Agent 不支持自动安装，或你希望自行管理安装位置，可以手动安装：

1. 打开项目的 [Releases 页面](https://github.com/shenjee/daily-market-review/releases)，下载最新版本包。文件名格式为 `daily-market-review-vX.Y.Z.zip`（例如 `daily-market-review-v0.3.4.zip`）。请勿下载 GitHub 自动生成的 `Source code (zip)`。
2. 解压，得到 `daily-market-review` 目录。
3. 将该目录放入所使用 Agent 的 Skill 目录。

各 Agent 的 Skill 目录位置请参考其官方文档。安装完成后，重启或刷新 Agent，使其加载新 Skill。

> **注意：** 请安装发布版本包，不要下载源码仓库。版本包仅包含运行所需文件，不含测试和开发内容。

## 使用

安装后，可直接向 Agent 提出请求，例如：

- “保存 2026-08-21 的市场复盘数据”
- “补充今天的涨停和连板名单”
- “把这张截图里的复盘数据提取并保存”
- “查看 2026-08-21 的市场复盘”
- “查看今天的每日梯队”

写入时，Skill 可以根据用户提供的文字、图片、网页或 API 数据完成整理和校验。读取已保存数据时不会自动联网取数，也不会修改数据库。

## 当前限制

- 只允许写入已经收盘的 A 股交易日。
- 内置交易日历仅覆盖 **2025–2026 年**；使用 2027 年及以后或 2025 年以前的日期前，须先更新 `assets/trading_calendar.json` 中的交易日历数据，或安装已包含对应年份日历的新版本。
- 打开已有数据库时，会通过 `CREATE TABLE IF NOT EXISTS` 自动补齐新增表，不改写已有用户数据。涉及已有列变化的升级仍会在版本说明中单独说明；升级前建议备份用户数据。

## 用户数据

默认数据文件位于：

```text
~/.marketreview/market_review.sqlite3
```

Skill 安装目录只存放程序和内置资源，用户数据独立保存在 `~/.marketreview/`。因此，同一系统用户下、且使用默认数据路径的多个 Agent，会共享同一份复盘数据；升级或移除 Skill 也不应删除该目录。若某 Agent 设置了不同的 `MARKETREVIEW_HOME`，则使用各自的数据目录。

如需更改数据目录，可在启动 Agent 前设置 `MARKETREVIEW_HOME`：

```bash
export MARKETREVIEW_HOME="/path/to/marketreview-data"
```

请定期备份该目录。不要让多个用户账户共用同一个数据目录。

### 云端配置模板（可选）

正式切换到 Supabase 之前，日常仍默认使用本地 SQLite，可不填写云端凭证。

需要接入云端时（`sync` 或 `backend=supabase`），若本机尚无配置文件，CLI 会自动从 Skill 安装目录复制模板到 `~/.marketreview/`（已有文件不覆盖），并提示自行填写。也可手工复制。填好的文件只留在本机，不要提交 Git、打进发布包，或贴到对话 / 日志 / Issue。

```bash
mkdir -p ~/.marketreview
[ -e ~/.marketreview/config ] || cp "<skill-dir>/config/marketreview.config.example" ~/.marketreview/config
[ -e ~/.marketreview/supabase.secret ] || cp "<skill-dir>/config/supabase.secret.example" ~/.marketreview/supabase.secret
chmod 600 ~/.marketreview/supabase.secret
```

将 `<skill-dir>` 换成本机 Skill 安装目录的绝对路径。源码开发时可把仓库根目录当作该路径。

| 项 | 本机位置 | 说明 |
| --- | --- | --- |
| 后端选择 | `~/.marketreview/config` 中 `backend` | 只能是 `sqlite` 或 `supabase`；切换前保持 `sqlite` |
| 项目 URL | 同上 `supabase_url` | 控制台 Project URL，不含密钥 |
| Publishable | 同上 `supabase_publishable_key` | `sb_publishable_...`（旧名 anon 亦可）；低权限，可写在 config |
| Secret | `~/.marketreview/supabase.secret` | `sb_secret_...`（旧名 service_role 亦可）；高权限，单独文件、单行、权限 600 |

日常写 RPC 使用 Secret。`backend=sqlite`（或显式 `--backend sqlite`）时不要求云端凭证。使用 `supabase` 时必须已填写 URL 与 Secret；缺配置、鉴权失败或断网会明确报错，**不会**回退本地 SQLite。兼容：仅当 `supabase.secret` **不存在**时，可读 `~/.marketreview/supabase.config` 里的 `SUPABASE_SECRET_KEY`；文件存在但内容无效时直接报错，不回退旧密钥。

### 两机同步（独立于日常 backend）

`sync push` / `sync pull` 不修改日常 `backend` 配置；需要本机已填写 URL 与 Secret。省略 `--source` / `--target` 时按现有本地路径规则解析日常 SQLite。

```bash
python3 "<skill-dir>/scripts/cli.py" sync push --source ~/.marketreview/market_review.sqlite3
python3 "<skill-dir>/scripts/cli.py" sync pull --target ~/.marketreview/market_review.sqlite3
```

冲突或本地删除待处理时，用组引用重复选择（可多次）：

- `--keep-cloud review:2026-08-21`
- `--adopt-local event:2026-08-21:sh:600519`
- `--restore-cloud` / `--delete-on-cloud`（本地删除待处理）

方向替换超时后的本机核验：`verify-pending`（过 8s 在途窗口且前像仍一致时才会受控重发）。

### 云端 PostgreSQL 备份（管理连接）

正式备份走数据库密码 + `pg_dump`（Session pooler；不用 transaction pooler，不用 Secret Key）。CLI：`scripts/pg_backup.py`（`backup` / `restore-blank` / `verify`）。成功包写入 `~/.marketreview/backups/supabase/<UTC>/`，含 `marketreview` schema、**`public.marketreview_*` wrappers**、迁移副本与校验清单；不进仓库。恢复须在空白库上核验通过才算有效。当前默认后端仍为 SQLite；切换门槛见设计文档 #10，勿提前改默认。

## 开发

从源码运行、测试和发布打包说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。
