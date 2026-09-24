# sftp2ods

[![tests](https://github.com/sandy-wangzining/sftp2ods/actions/workflows/tests.yml/badge.svg)](https://github.com/sandy-wangzining/sftp2ods/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/sandy-wangzining/sftp2ods/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sandy-wangzining/sftp2ods/blob/main/LICENSE)

**通用 SFTP 按天文件（CSV/TSV）→ MaxCompute ODS 同步工具**：把渠道/供应商 SFTP 上
按天产出的报表**列展开**写进 MaxCompute 宽表（`pt` = 文件日期），接一个新源 ≈ 写一份配置。

- 配置驱动：SFTP 地址、认证、远端目录/文件名规则、列定义（含类型）、目标表都在一个 JSON 里
- 写入幂等：每个日期**先删再填** `pt` 分区；本地台账（文件大小）决定跳过，重复跑/补数不会叠加
- 自动建表：目标表不存在时按列定义建表（可选 comment/stored_as/lifecycle），存在则逐列校验结构
- 少见即用的健壮性：SFTP 连接/下载自动重试（认证失败快速失败）、下载先 `.part` 再核对大小、
  行数与列数校验、合计行金额核对、写后 `count(*)` 复核、密钥脱敏、运行锁、写库失败退出码非 0
- 两类远端布局：`flat`（目录下直接是文件）/ `date_dir`（`目录/{日期}/文件`）
- 缺文件核对：远端每天必须至少有一个匹配文件，缺了直接失败 + 飞书告警（不会静默少一天）
- 跨平台：Windows / macOS / Linux（Python 3.9+），只需 `paramiko` + `pyodps` + `requests`

仓库内 `jobs/` 有两个生产来源的完整示例：

| 示例 | 形态 |
|---|---|
| `jobs/clink_settlement.example.json` | 平铺目录、`settlement_report_YYYYMMDD.csv`、私钥认证、UTC 02:30 后出 T-1、末尾合计行校验 |
| `jobs/waffo_settlement.example.json` | 日期子目录 `settlements/{日期}/`、一天多个文件、密码认证、按列头名映射（缺列告警） |

> 这两个示例等价于 `clink_settlement_sync/`、`waffo_settlement_sync/` 两个独立脚本，
> 迁移说明见下方「从旧脚本迁移」。

## 安装

```bash
# 方式一：pip（建议放虚拟环境）
python3 -m venv venv && ./venv/bin/pip install sftp2ods    # 发布到 PyPI 后
./venv/bin/pip install .                                    # 或在源码目录本地安装
sftp2ods --version

# 方式二：pipx（全局命令行工具，隔离环境）
pipx install .

# 方式三：直接从 GitHub 安装
pipx install "git+https://github.com/sandy-wangzining/sftp2ods.git"

# 方式四：源码直接跑（不安装）
python -m sftp2ods --job jobs/xxx.json --check
```

Windows / macOS / Linux 通用；Windows 会自动带上 `tzdata` 依赖（时区数据）。

## 三分钟上手

```bash
# 1) 生成一份作业配置（交互式问答；也可以复制 jobs/_template_simple.example.json 手工改）
sftp2ods --init

# 2) 体检：配置 + 真实列一次远端目录 + 目标表结构（新接源第一步）
sftp2ods --job jobs/my_sftp.json --check

# 3) 试跑：只下载/解析数行数，不写库
sftp2ods --job jobs/my_sftp.json --bizdate 20260920 --dry-run

# 4) 正式同步：只处理业务日那天的文件，写进 pt=20260920
sftp2ods --job jobs/my_sftp.json --bizdate 20260920

# 常用补充：
sftp2ods --job jobs/my_sftp.json --bizdate ${bizdate}            # 调度（DataWorks 传业务日）
sftp2ods --job jobs/my_sftp.json                                 # 不传业务日：处理远端全部未上传日期
sftp2ods --job jobs/my_sftp.json --start-date 2026-09-01 --end-date 2026-09-10   # 补数（闭区间）
sftp2ods --job jobs/my_sftp.json --bizdate 20260920 --force      # 强制重写（忽略已上传台账）
```

> **日期模型**（和结算文件的现实一致）：
> - 每个远端文件提取出一个**文件日期**（平铺=文件名里的日期；日期子目录=目录名），它就是数据所属的业务日；
> - **`pt` 永远等于文件日期**，一个日期一个分区（一天多个文件合并进同一个 `pt`）；
> - `--bizdate` 只处理那一天；`--start-date/--end-date` 处理区间；都不传 = 远端全部（已上传的自动跳过）；
> - 调度传 `${bizdate}` 时，缺文件核对、处理范围、目标分区都是那一天，口径完全一致。

## 它是怎么工作的

```
SFTP（按天文件）
  │  ① 列远端目录 → 按 file_regex/目录规则得到 {文件日期: [文件, ...]}，大小一并记下
  │  ② 缺文件核对：核对区间内每天必须至少一个文件；缺了 → 飞书告警 + 失败退出（不写库）
  ▼
逐日期处理：
  │  ③ 台账命中（本地文件大小一致 + 台账记录表/pt 一致）→ 跳过；否则下载（.part → 核大小 → 改名）
  │  ④ 先完整解析一遍数行数：表头校验、列数校验、类型转换、合计行金额核对（坏文件在写库前倒下）
  ▼
MaxCompute：宽表 + pt 分区
  │  ⑤ 自动建表/校验结构 → delete_partition → Tunnel 分批写入（每批 500 行）→ count(*) 复核
  │  ⑥ 全部通过才写台账（表/pt/大小/行数）——失败中断后重跑会自动重试那个日期
  ▼
DWD 层：按业务口径加工/引用
```

## 配置参考

作业文件（`jobs/*.json`）**只有 `sftp`、`source`、`parse`、`target` 四个块必填**，
其余按需。字段默认值与逐行说明见 `jobs/_template_full.example.json`，最小示例见
`jobs/_template_simple.example.json`。密钥建议写在 `secrets` 块、用 `${secrets.键名}` 引用
（作业文件已在 `.gitignore` 里，不会进 git）。

### 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `job` | 否 | 作业名（日志 + 默认下载目录名） |
| `description` | 否 | 一句话描述（日志用） |
| `secrets` | 否 | 密钥键值对；配置里用 `${secrets.键名}` 引用 |
| `maxcompute` | 是* | `project` / `endpoint`（默认 us-west-1）/ `access_key_id`+`access_key_secret`（可简写 `ak`/`sk`） |
| `profiles` | 否 | 多套 MaxCompute 凭证，配合 `target.profile` 或 `--mc-profile` 切换 |
| `sftp` | 是 | SFTP 连接（见下） |
| `source` | 是 | 远端文件规则（见下） |
| `parse` | 是 | 列定义与解析规则（见下） |
| `target` | 是 | 目标表（见下） |
| `missing` | 否 | 缺文件核对（见下） |
| `notify` | 否 | 飞书告警（见下） |

> 占位符：`${secrets.键名}` / `${bizdate}` / `${bizdate_iso}` / `${today}` / `${today_iso}`。
> 以 `//` 或 `#` 开头的键当注释（不校验、不做占位符替换），模板里大量使用。

### sftp（连接与认证）

| 字段 | 默认 | 说明 |
|---|---|---|
| `host` / `port` / `username` | - / 22 / - | 连接信息 |
| `auth.type` | password | `password`（配 `password`）/ `key`（配 `key_file`，可选 `passphrase`，`~` 会展开） |
| `connect_timeout` / `io_timeout` | 30 / 600 | 连接/通道超时（秒） |
| `retry_times` / `retry_delay` | 3 / 10 | 列目录/下载失败后的重试次数与首次冷却（指数退避）；认证失败不重试 |

> 主机密钥不校验（等价 `StrictHostKeyChecking=no`），与两个结算脚本口径一致；
> 需要校验 known_hosts 的场景请提 Issue。

### source（远端文件规则）

| 字段 | 默认 | 说明 |
|---|---|---|
| `root` | 必填 | 远端目录（如 `/statements` 或 `settlements`；相对路径相对登录 Home） |
| `layout` | flat | `flat`：目录下直接是文件；`date_dir`：`目录/{日期}/文件`（一天可多个文件） |
| `file_regex` | 必填 | 文件名正则；`flat` 必须含 `(?P<date>\d{8})` 命名捕获组。**完整匹配**（等价 `^...$`） |
| `date_dir_regex` | date_dir 必填 | 日期子目录名正则，必须含 `(?P<date>...)`（如 `(?P<date>\d{8})`） |
| `download_dir` | `<作业目录>/download/<作业名>` | 文件留档 + 台账目录；相对路径按作业文件所在目录算 |

### parse（列定义与解析）

| 字段 | 默认 | 说明 |
|---|---|---|
| `encoding` | `utf-8-sig` | 源文件编码；不是 UTF-8 的源（如 `gbk`）显式指定，乱码会直接报错而不是静默替换 |
| `delimiter` | `auto` | `auto`（Tab > 逗号 > 分号的保守探测）或单个字符 |
| `columns` | 必填 | 列定义数组，见下表 |
| `on_missing_header` | `error` | `error`：缺任一列头报错；`warn`：缺的列告警并按空入库 |
| `strict_columns` | true | 每行列数必须等于表头列数（防列错位）；false 时允许行尾少列（按空补齐），多列仍报错 |
| `empty_as` | `null` | 空串的落库形态：`null`（空串存 NULL）/ `empty`（空串原样存 `''`，兼容既有表口径，见 clink 示例） |
| `skip_if_empty` | - | 这些列（**目标列名**）值为空的行跳过不入库（如 `["order_id"]`），跳过的行数会打日志 |
| `footer` | - | 开启合计行处理：首列为空的合计行不写库；`{"sum": ["金额列", ...]}` 会校验「合计 = 数据行之和」，对不上报错 |

`columns` 每项：

| 字段 | 必填 | 说明 |
|---|---|---|
| `header` | 是 | 源文件列头原文（匹配时忽略大小写与多余空白，所以列顺序变了也不怕） |
| `name` | 是 | 目标表列名（MaxCompute 标识符规则） |
| `type` | 是 | `string` / `bigint` / `double` / `decimal(p,s)` |
| `comment` | 否 | 建表时的列注释 |
| `required` | 否 | 覆盖 `on_missing_header`：`true` 强制必须有 / `false` 允许缺失 |

> 取值口径：空值一律存 `NULL`（不是空字符串、更不是 0）；金额/整数解析失败**直接报错**
> （宁可失败，不写错数）；`decimal` 允许千分位逗号（`1,234.50`）。

### target（目标表）

| 字段 | 默认 | 说明 |
|---|---|---|
| `project` / `table` | 项目取 `maxcompute.project`；表名必填 | 目标表 |
| `comment` | - | 建表注释 |
| `stored_as` / `lifecycle_days` | - | 存储格式 / 生命周期（仅新建时生效） |
| `allow_empty` | true | 解析出 0 行时是否允许写空分区。结算类文件常有"零交易的一天"，默认允许；写空会打警告 |
| `profile` | default | 使用 `profiles.<名>` 的凭证 |

`project`/`table`/列名/`stored_as` 会拼进 DDL 与校验 SQL，只允许字母/数字/下划线且不以数字开头
（写错给明确配置错，而不是建表失败或注入风险）。

### missing（缺文件核对）

| 字段 | 默认 | 说明 |
|---|---|---|
| `check` | true | 核对区间内每天必须至少一个匹配文件，缺了直接失败 + 告警（不写库） |
| `timezone` | Asia/Shanghai | "昨天"按哪个时区算（不传 `--bizdate` 时用于推断预期最新） |
| `grace` | - | `HH:MM`：源方每天几点前还没生成 T-1 文件（如 Clink 是 UTC 02:30）。此刻之前跑，预期最新再后退一天防误报 |

### notify（飞书告警）

| 字段 | 默认 | 说明 |
|---|---|---|
| `webhook` | - | 群机器人地址；`--no-notify` 可临时关闭 |
| `enabled` | true | 关掉后只报错不发通知 |

> webhook 是凭证：日志里不会出现它，`utils` 的脱敏规则也会把 `/hook/xxx` 后面的 id 遮掉。

## 从旧脚本迁移（clink / waffo 结算同步）

`sftp2ods` 从这两个脚本抽象而来，`jobs/` 下的两个 `.example.json` 与旧脚本**完全同表、同口径**：

| 旧脚本 | sftp2ods 作业 | 说明 |
|---|---|---|
| `clink_settlement_sync.py` | `jobs/clink_settlement.example.json` | 表 `ods_clink_settlement_details_di`、pt=文件日期、合计行校验、UTC 02:30 宽限 |
| `waffo_settlement_sync.py` | `jobs/waffo_settlement.example.json` | 表 `ods_waffo_settlement_details_di`、pt=文件日期、缺 Order Id 行跳过 |

迁移步骤（用户自行决定切换时机）：

1. 复制示例为真实作业（含密钥），改三处：`sftp.username/auth`、`maxcompute` 的 AK/SK、`notify.webhook`；
2. `sftp2ods --job jobs/xxx.json --check` 看远端文件与目标表结构；
3. `--bizdate <某天> --dry-run` 对数（行数应与旧脚本一致）；
4. 正式切调度：旧脚本命令换成 `sftp2ods --job jobs/xxx.json --bizdate "${bizdate}"`。

**想复用旧脚本已经下载的文件与台账**：把 `source.download_dir` 指到旧目录
（如 `../clink_settlement_sync/settlement`），台账字段完全兼容（`table/pt/size/rows`，
date_dir 布局还会回退按旧的文件名键查找），已上传的日期会直接跳过、不重复导入。

## 行为与保护（为什么可以放心跑调度）

1. 列目录 / 下载失败自动重试（指数退避，每次重试都是全新连接）；认证失败立即失败（重试无意义）；
2. 下载先落 `.part`、核对大小后才改名生效——半截文件永远不会以正式名进解析；
3. 解析阶段完整校验：表头必需列、重复列、空列名、行-列数一致、金额可解析、合计行位置与金额；
   任何一条不过 → 不写库，退出码 1；
4. 写库「先删再填」：删分区 → 建分区 → Tunnel 分批写入 → 写后 `count(*)` 复核；重跑幂等；
   写库失败会明确提示"分区可能已被清空、请重跑"，不会假装成功；
5. 单格超过约 7MB 在**删分区之前**就检查报错（MaxCompute 单列上限 8MB）；
6. 台账只用"表 + pt + 大小"命中：远端文件更新过（大小变了）会自动重下重写；
7. 每个作业一把运行锁（同名作业不同目录互不影响），同一作业不会并发跑；
8. 日志与异常里的 SFTP 密码、私钥口令、AK/SK、飞书 hook 一律脱敏；
9. 建表/删分区/校验 SQL 有超时保护（默认 600 秒，`--sql-timeout` 可调，0 = 不限制）；
10. Ctrl+C（130）不写库；写入阶段中断的分区可能不完整，重跑同一命令即可恢复。

### 退出码（调度侧判断成败）

| 码 | 含义 |
|---|---|
| 0 | 成功（含 `--dry-run`、`--check`） |
| 1 | 运行失败：配置错、连接/下载失败、解析失败、缺文件、写库失败、写后行数对不上 |
| 2 | 命令行参数问题（缺 `--job` 等），**没做过任何远端操作** |
| 130 | 用户中断（Ctrl+C） |

## 常见问题

| 现象 | 处理 |
|---|---|
| `远端目录下没有任何匹配文件` | 检查 `source.root`、`file_regex`、源方是否已产出；`--check` 会打印远端日期/样本 |
| `缺少必需列头：...` | 源文件列头变了：用 `--dry-run` 对比；确认列可缺时给该列 `"required": false` 或整体 `on_missing_header: "warn"` |
| `第 N 行列数 X ≠ 表头 Y` | 文件里有坏行；确认不是分隔符识别错（`parse.delimiter` 显式指定），或设 `strict_columns: false` 容忍行尾短列 |
| 金额列报 `不是合法的数字` | 源里是非数字（如 `--`）；确认是否该列先清洗，工具不静默转 0 |
| `合计行与数据行合计不一致` | 文件口径变了或文件被截断——工具拒绝写库是保护，先核对源文件 |
| 时间不对/每天晚一天 | 检查 `missing.timezone`/`grace`；`--bizdate` 只认传入的业务日，不猜 |
| `downloads` 目录越来越大 | 文件是留档（重跑/对账用）；可定期归档，删文件后重跑会重新下载（台账会因其大小一致而认为已上传？会——台账仍在。要彻底重导用 `--force`） |
| 补数时只导一部分日期 | `--start-date/--end-date` 是过滤远端已有文件；缺文件核对仍会拦（想跳过用 `missing.check: false`） |
| 同一个作业文件在两台机器上跑 | 运行锁只在单机生效；请用调度系统保证同一作业不并发 |

## 开发与测试

```bash
python -m unittest discover -s tests -v    # 220 个离线用例：不连 SFTP、不连数仓
pip install -e ".[dev]" && ruff check .    # 代码检查（配置在 pyproject.toml，当前 0 告警）
```

CI 在 ubuntu / windows / macos × Python 3.9 ~ 3.14 十八种组合上跑同一套用例
（见 `.github/workflows/tests.yml`）。想改代码或加数据源，先看 `CONTRIBUTING.md`
——里面写了这个工具的几条"设计红线"（先删再填、宁可失败不可静默丢数、密钥不进日志……）。

模块结构（都在 `sftp2ods/` 包内，每个文件头部有职责说明）：

| 模块 | 职责 |
|---|---|
| `cli.py` | 命令行入口 / 体检 / 同步主流程 |
| `init_wizard.py` | `--init` 交互式配置生成 |
| `config.py` | 配置加载/占位符/校验/目标解析 |
| `dates.py` | 日期与时区、缺文件核对、处理范围规划 |
| `sftp.py` | SFTP 连接、列目录、下载（.part + 大小核对） |
| `parse.py` | CSV/TSV 解析：列头映射、类型转换、合计行 |
| `mc.py` | MaxCompute：建表/结构校验 / 先删再填 / 行数核对 |
| `state.py` | 上传台账（.uploaded.json） |
| `notify.py` | 飞书群卡片告警 |
| `utils.py` | 日志、运行锁、脱敏、重试 |

## License

MIT（见 `LICENSE`）。
