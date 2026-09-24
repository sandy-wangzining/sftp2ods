# 变更记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)；格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [1.0.0] - 2026-09-24

首个版本：从 `clink_settlement_sync` / `waffo_settlement_sync` 两个独立脚本抽象成
配置驱动的通用 SFTP → MaxCompute ODS 同步工具。

### 功能

- 作业配置驱动（`jobs/*.json`）：SFTP 连接与认证（密码/私钥）、远端文件规则
  （`flat` 平铺 / `date_dir` 日期子目录）、列定义（`string`/`bigint`/`double`/`decimal(p,s)`）、
  目标表、缺文件核对、飞书告警。
- 解析：按列头名映射（忽略大小写/空白），必需列缺失报错、可选列缺失告警按空入库；
  行-列数严格校验；空值默认存 NULL（`parse.empty_as: empty` 可让空串原样存 `''`，
  兼容旧 clink 表口径）；金额解析失败报错；可选的"合计行（首列为空）不写库 +
  金额 = 数据行之和"校验；自动探测分隔符（Tab/逗号/分号）；显式编码（含 GBK）。
- 写入：`pt` = 文件日期（一个日期一个分区，一天多文件合并）；自动建表/结构校验
  （列名、顺序、类型、分区）；先删再填；Tunnel 分批写入；写后 `count(*)` 复核；
  写库前先查单格大小（超 7MB 提前失败）。
- 可靠性：SFTP 操作重试（认证失败快速失败）、下载 `.part` + 大小核对、缺文件核对
  （内部断档 + 最新缺失，可配时区与"每日生成时刻"宽限）、空远端告警、运行锁、
  Ctrl+C 不写库、密钥脱敏（形态 + 配置值）、SQL 超时保护、退出码 0/1/2/130。
- 台账（下载目录 `.uploaded.json`）：表/pt/大小 命中即跳过；`--force` 强制重写；
  与两个旧脚本的台账字段兼容，`source.download_dir` 指到旧目录即可复用已下载文件。
- `--check` 体检（配置 + 真实列目录 + 目标表结构）、`--dry-run` 试跑、`--init` 交互式向导
  （可直接连 SFTP 拉样本文件生成列定义）、`--start-date/--end-date` 补数。
- CLI 入口 `sftp2ods`（console script）与 `python -m sftp2ods` 两种用法。

### 示例

- `jobs/clink_settlement.example.json`：Clink 结算文件（等价旧脚本）。
- `jobs/waffo_settlement.example.json`：Waffo 结算明细（等价旧脚本）。
- `jobs/_template_full.example.json` / `jobs/_template_simple.example.json`。

### 工程

- 239 个离线单元测试（假 SFTP + 假 MaxCompute，不访问网络/数仓）。
- GitHub Actions：ubuntu/windows/macos × Python 3.9 ~ 3.14 + `ruff check`。
