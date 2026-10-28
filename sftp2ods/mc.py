# -*- coding: utf-8 -*-
"""MaxCompute：凭证、建表/结构校验、SQL 超时控制、分区覆盖写入、行数校验。

目标表形态：列展开宽表 + pt 分区（pt = 文件日期，普通分区表），与两个结算脚本一致。
写入是"先删再填"：删分区 → 建分区 → Tunnel 分批写 → 写后 count(*) 核对；
任何失败都会让调度看到非 0 退出码（旧分区可能已清空，重跑幂等自愈）。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from . import _common
from .parse import Column, _cell_bytes, normalize_type
from .utils import ConfigError, log, progress_log, redact, require_identifier, retry_call

try:
    from odps import ODPS
except ImportError:  # pragma: no cover
    ODPS = None

PARTITION_COLUMN = "pt"
SQL_TIMEOUT_SECONDS = 600
MAX_CELL_BYTES = 7_000_000  # MaxCompute 单列字符串上限 8MB，留余量提前报错
SQL_HEARTBEAT_SECONDS = 30
# https：作业没写 endpoint 时 AK/SK 签名与查询结果不能走明文 HTTP
DEFAULT_ENDPOINT = "https://service.us-west-1.maxcompute.aliyun.com/api"
_PARTITION_VALUE_RE = re.compile(r"\A\d{8}\Z", re.ASCII)  # pt 恒为 8 位业务日；re.ASCII 挡全角/阿拉伯-印度数字


# =============================================================================
# 凭证与连接
# =============================================================================


def _pick_aksk(item: dict) -> tuple[str, str]:
    """从配置里取 AK/SK：兼容 access_key_id/ak/_id 与 access_key_secret/ak_secret/sk 几种写法。"""
    ak_id = next((str(item[key]) for key in ("access_key_id", "ak_id", "ak") if item.get(key)), "")
    secret = next((str(item[key]) for key in ("access_key_secret", "ak_secret", "sk") if item.get(key)), "")
    return (ak_id, secret) if ak_id and secret else ("", "")


def load_mc_credentials(profile: dict, source_label: str = "作业文件", cli_profile: str = "") -> tuple[str, str, str]:
    """按优先级查找阿里云 AccessKey，返回 (ak_id, ak_secret, 来源说明)；只说明位置，不含密钥。

    优先级：作业/配置文件里的 profile → 环境变量 → 本机 aliyun CLI。
    """
    if profile is not None and not isinstance(profile, dict):
        # 非映射（maxcompute 误写成字符串/数组）时 _pick_aksk 会裸 AttributeError；
        # 显式报配置错，也避免静默回退到环境变量/本机 aliyun CLI（可能用另一个身份写库）
        # 错误信息只报类型、不回显内容：maxcompute 块里就是 AK/SK，截 80 个字符
        # 也可能把密钥原文打进运行日志
        raise ConfigError(
            f"{source_label}的 maxcompute 配置应是映射（含 access_key_id/access_key_secret），"
            f"实际是 {type(profile).__name__}；请检查该块的写法"
        )
    ak_id, secret = _pick_aksk(profile or {})
    if isinstance(profile, dict):
        has_id = any(profile.get(k) for k in ("access_key_id", "ak_id", "ak"))
        has_sk = any(profile.get(k) for k in ("access_key_secret", "ak_secret", "sk"))
        if has_id != has_sk:
            # 只填一半（典型：secret 键名拼错）必须报错，不能警告后回退：回退到环境变量/
            # 本机 aliyun CLI 会用另一个身份写库（审计/计费/归属全错），与「--mc-profile
            # 找不到不回退」「非映射报错」同口径
            missing = "access_key_secret（或 ak_secret/sk）" if has_id else "access_key_id（或 ak_id/ak）"
            raise ConfigError(
                f"{source_label}的 maxcompute 配置里 AK/SK 只填了一半、缺 {missing}；"
                f"补齐这对凭证，或整对留空以使用环境变量/本机 aliyun CLI"
            )
    if ak_id and secret:
        name = str((profile or {}).get("name") or "default")
        return ak_id, secret, f"{source_label}（{name}）"

    env_id, env_secret = os.environ.get("ALIYUN_ACCESS_KEY_ID"), os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
    if env_id and env_secret:
        return env_id, env_secret, "环境变量 ALIYUN_ACCESS_KEY_ID/SECRET"

    cli_config_file = Path.home() / ".aliyun" / "config.json"
    if cli_config_file.is_file():
        # 这个文件是"锦上添花"的凭证来源，坏了不该抛裸 traceback
        try:
            cli_config = json.loads(cli_config_file.read_text(encoding="utf-8"))
            if not isinstance(cli_config, dict):
                raise ValueError(f"顶层应是 JSON 对象，实际是 {type(cli_config).__name__}")
            raw_profiles = cli_config.get("profiles") or []
            if not isinstance(raw_profiles, list):
                raise ValueError(f"profiles 应是数组，实际是 {type(raw_profiles).__name__}")
            profiles = {p.get("name"): p for p in raw_profiles if isinstance(p, dict)}
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"本机 aliyun CLI 配置读不了（{cli_config_file}）：{exc}\n"
                f"  可以不修它——改用作业文件的 maxcompute.access_key_id/access_key_secret，"
                f"或设环境变量 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET"
            ) from exc
        if cli_profile:
            # 显式指定的 profile 必须自食其力：原来找不到/没 AK 会静默回退到其它 profile，
            # 等于用另一个身份写库（与"AK/SK 只填一半"同口径，宁可报错）
            item = profiles.get(cli_profile) or {}
            if not (item.get("access_key_id") and item.get("access_key_secret")):
                raise SystemExit(
                    f"指定的 aliyun CLI profile「{cli_profile}」不存在或没有 AK/SK；"
                    f"不会自动回退到其它 profile（避免用另一个身份写库），请检查 --cli-profile"
                )
            return item["access_key_id"], item["access_key_secret"], f"aliyun CLI profile [{cli_profile}]"
        current = cli_config.get("current")
        if current and current in profiles:
            item = profiles[current]
            if not (item.get("access_key_id") and item.get("access_key_secret")):
                # 与 --mc-profile 分支同口径：current 是用户显式选择的身份，它没有 AK/SK
                # （常见于 STS/RAM 角色模式）时静默换一个 profile 等于用另一个身份写库
                raise SystemExit(
                    f"aliyun CLI 当前 profile「{current}」没有 AK/SK（可能是 STS/RAM 角色模式）；"
                    f"不会自动改用其它 profile（避免用另一个身份写库）。请用 --cli-profile 指定"
                    f"带 AK/SK 的 profile，或在作业文件里填 maxcompute.access_key_id/access_key_secret"
                )
            return item["access_key_id"], item["access_key_secret"], f"aliyun CLI profile [{current}]"
        names = [current]
        names += [
            name
            for name, item in profiles.items()
            if item.get("mode") == "AK" and item.get("access_key_id") and item.get("access_key_secret")
        ]
        for name in names:
            item = profiles.get(name) or {}
            if item.get("access_key_id") and item.get("access_key_secret"):
                return item["access_key_id"], item["access_key_secret"], f"aliyun CLI profile [{name}]"

    raise SystemExit(
        "找不到阿里云 AccessKey。任选一种方式：\n"
        "  1) 在作业文件的 maxcompute 块里填 access_key_id/access_key_secret（推荐）\n"
        "  2) 设置环境变量 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET\n"
        "  3) 配置本机 aliyun CLI（aliyun configure）"
    )


def connect_odps(
    config: dict, source_label: str, profile: dict, project: str, endpoint: str = "", cli_profile: str = ""
):
    """建立 MaxCompute 连接；缺 pyodps / 缺凭证时给出明确报错。"""
    if ODPS is None:
        raise SystemExit("缺少 pyodps：pip install pyodps")
    ak_id, secret, source = load_mc_credentials(profile, source_label, cli_profile)
    log(f"MaxCompute 凭证来源：{source}")
    endpoint = endpoint or str((profile or {}).get("endpoint") or DEFAULT_ENDPOINT)
    return ODPS(ak_id, secret, project, endpoint=endpoint)


# =============================================================================
# SQL 执行（带超时：pyodps 默认不限时，云端卡住会一直干等）
# =============================================================================


def run_sql_with_timeout(o, sql: str, timeout: int = SQL_TIMEOUT_SECONDS, desc: str = "SQL"):
    """通过公共实现等待 SQL，保留项目自身的时钟、脱敏日志与超时配置。"""
    return _common.run_sql_with_timeout(
        o, sql, timeout, desc, clock=time, log=lambda message: log(redact(message)), heartbeat=SQL_HEARTBEAT_SECONDS
    )


# =============================================================================
# 目标表：DDL / 结构校验 / 自动建表
# =============================================================================


def build_table_ddl(
    project: str,
    table: str,
    columns,
    comment: str = "",
    stored_as: str = "",
    lifecycle_days: int | None = None,
    *,
    staging: bool = False,
) -> str:
    """宽表 DDL：列展开 + pt 分区。

    project/table/列名/stored_as 都是直接拼进 DDL 的**标识符**：这里再做一道防御性校验
    （config.validate_job 也会校验，但 mc.py 可能被单独调用），挡住注入与拼错。
    """
    project = require_identifier(project, "target.project")
    table = require_identifier(table, "target.table")
    if stored_as:
        stored_as = require_identifier(stored_as, "target.stored_as")
    lines = [f"create table if not exists {project}.{table} ("]
    body = []
    for col in columns:
        name = require_identifier(col.name, "parse.columns[].name")
        line = f"    {name} {normalize_type(col.type)}"
        if col.comment:
            line += f" comment '{col.ddl_comment()}'"
        body.append(line)
    lines.append(",\n".join(body))
    lines.append(")")
    lines.append(f"partitioned by ({PARTITION_COLUMN} string comment '业务日期 yyyyMMdd（= 文件日期）')")
    if stored_as:
        lines.append(f"stored as {stored_as}")
    # 反斜杠先转义（同 parse.Column.ddl_comment / _sql_spec）：以 "\\" 结尾的注释
    # 会吃掉收尾引号、破坏 DDL
    table_comment = (comment or "SFTP 文件列展开").replace("\\", "\\\\").replace("'", "''")
    stage_properties = ", 'lifecycle.deletemeta'='true'" if staging else ""
    lines.append(f"tblproperties ('comment' = '{table_comment}'{stage_properties})")
    # LIFECYCLE 放在 TBLPROPERTIES 之后：与 api2ods（其示例作业带 lifecycle_days，实际跑过）
    # 以及官方 PK 表语法 `... [TBLPROPERTIES (...)] [LIFECYCLE <days>]` 的顺序一致
    if lifecycle_days is not None:
        # 非正值（0 常被理解成"永不过期"，但 MaxCompute 的 LIFECYCLE 只接受正整数）
        # 显式报错，不能真值判断静默省略——那会让用户以为设了生命周期、实际按默认回收
        if not isinstance(lifecycle_days, int) or isinstance(lifecycle_days, bool) or lifecycle_days <= 0:
            raise ConfigError(f"lifecycle_days 必须是正整数，实际 {lifecycle_days!r}（不设生命周期请留空）")
        lines.append(f"lifecycle {int(lifecycle_days)}")
    return "\n".join(lines)


def _table_schema_of(table) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """取表的 (非分区列名→类型, 分区列名→类型)，名字按原样返回。"""
    schema = table.table_schema
    partition_names = [str(col.name) for col in schema.partitions]
    partitions = [(str(col.name), normalize_type(col.type)) for col in schema.partitions]
    columns = [
        (str(col.name), normalize_type(col.type)) for col in schema.columns if str(col.name) not in partition_names
    ]
    return columns, partitions


def verify_table_schema(table, table_name: str, columns) -> None:
    """校验已存在的表结构与配置一致；不一致直接报错拒绝写入。

    检查顺序：不是视图 → 不是事务表 → 列名（忽略大小写，顺序必须一致）→ 列类型 → 分区只有 pt。
    顺序必须按位对齐：Tunnel 按位置写列，顺序错了就是"每列都写错位"而且行数校验照样通过。
    """
    if getattr(table, "is_virtual_view", False) or getattr(table, "is_materialized_view", False):
        raise SystemExit(f"{table_name} 是视图，不能作为写入目标")
    if getattr(table, "is_transactional", False):
        raise SystemExit(f"{table_name} 是事务表，本框架按「普通分区表暂存后原子发布」写入；请换表名或先 drop 后重建")
    existing, partitions = _table_schema_of(table)
    wanted = [(col.name, normalize_type(col.type)) for col in columns]
    if [name.lower() for name, _ in existing] != [name.lower() for name, _ in wanted]:
        raise SystemExit(
            f"{table_name} 表结构与配置不一致，拒绝写入。\n"
            f"  配置列：{[name for name, _ in wanted]}\n"
            f"  实际列：{[name for name, _ in existing]}"
        )
    for (name, existing_type), (_, wanted_type) in zip(existing, wanted):
        if existing_type != wanted_type:
            raise SystemExit(
                f"{table_name}.{name} 类型是 {existing_type}，配置要求 {wanted_type}；改配置或先 drop 重建该表"
            )
    if [name.lower() for name, _ in partitions] != [PARTITION_COLUMN.lower()]:
        raise SystemExit(
            f"{table_name} 的分区列是 {[name for name, _ in partitions]}，框架要求只有 [{PARTITION_COLUMN}]"
        )
    for name, type_text in partitions:
        if type_text != "string":
            raise SystemExit(f"{table_name}.{name} 分区类型是 {type_text}，框架要求 string")


def ensure_target_table(
    o,
    project: str,
    table_name: str,
    columns,
    comment: str = "",
    stored_as: str = "",
    lifecycle_days: int | None = None,
    timeout: int = SQL_TIMEOUT_SECONDS,
):
    """表不存在则按列配置建表；存在则校验结构，返回 table 对象。"""
    ddl = build_table_ddl(project, table_name, columns, comment, stored_as, lifecycle_days)
    run_sql_with_timeout(o, ddl, timeout=timeout, desc=f"建表 {table_name}")  # create if not exists
    # 必须全限定取表：连接绑定的 project 与 target.project 不同（maxcompute.project=A、
    # target.project=B）时，o.get_table(table_name) 会拿到 A 的同名表——校验错对象、
    # Tunnel 也写进 A，而 drop/add/count 全在 B（B 被清空、A 被污染）
    table = o.get_table(f"{project}.{table_name}")
    verify_table_schema(table, table_name, columns)
    return table


def partition_exists(table, partition_value: str) -> bool:
    """目标分区是否存在：台账命中后用它确认数据真的还在（表被重建/分区被删时重写）。"""
    return bool(table.exist_partition(f"{PARTITION_COLUMN}={partition_value}"))


# =============================================================================
# 分区覆盖写入 / 行数校验
# =============================================================================


def _sql_spec(spec: str) -> str:
    """把 pyodps 风格分区串（pt=20260920）转成 DDL 里的带引号写法（pt='20260920'）。

    pyodps 的 partition.name 可能已带引号（pt='20260920'），统一先去掉再补引号，
    避免生成 pt=''20260920'' 这种非法写法；分区字段名同样过标识符白名单。

    边界：本工具的表结构被 verify_table_schema 强制为单级 pt 分区，spec 恒由内部按
    pt=<值> 构造，多级分区（pt=x,region=y）不可达（会被字段名白名单挡下）。
    """
    key, _, value = str(spec).partition("=")
    key = require_identifier(key.strip(), "分区字段名")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not value:
        raise ConfigError(f"分区值不能为空：{spec!r}")
    # 与 count/write 同一套白名单+转义：这里再兜一道，DDL 里分区值也只可能是 8 位业务日
    return f"{key}='{_partition_literal(value)}'"


def drop_partition(o, project: str, table_name: str, spec: str, timeout: int = SQL_TIMEOUT_SECONDS) -> None:
    """删除分区（DDL，走超时保护；分区不存在也不报错，与旧口径的 if_exists=True 一致）。

    走 DDL 而不是 pyodps 的 `table.delete_partition`（同步、不限时）：云端分区元数据操作
    卡住时同样会无限挂起、一直占着运行锁，把后续调度全顶掉。`IF EXISTS` 提供与
    `delete_partition(if_exists=True)` 一致的幂等语义。
    """
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    run_sql_with_timeout(
        o,
        f"alter table {project}.{table_name} drop if exists partition ({_sql_spec(spec)})",
        timeout=timeout,
        desc=f"删分区 {table_name}",
    )


def add_partition(o, project: str, table_name: str, spec: str, timeout: int = SQL_TIMEOUT_SECONDS) -> None:
    """新增分区（DDL，走超时保护；分区已存在不报错，与旧口径的 if_not_exists=True 一致）。"""
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    run_sql_with_timeout(
        o,
        f"alter table {project}.{table_name} add if not exists partition ({_sql_spec(spec)})",
        timeout=timeout,
        desc=f"建分区 {table_name}",
    )


def write_partition(
    o,
    table,
    project: str,
    table_name: str,
    partition_value: str,
    batches_factory,
    total: int | None = None,
    retries: int = 3,
    timeout: int = SQL_TIMEOUT_SECONDS,
) -> int:
    """先写独立暂存表并核对，再以单条 INSERT OVERWRITE 发布；正式分区不提前删除。

    batches_factory 必须可重复读取；写前大小校验保留。暂存写入失败使用新 Tunnel 会话重试，
    发布重试复用已核对的数据；发布响应丢失时幂等覆盖，失败残留由生命周期兜底回收。
    """
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    partition_value = _partition_literal(partition_value)
    spec = f"{PARTITION_COLUMN}={partition_value}"
    existing, _ = _table_schema_of(table)
    columns = [Column(header=name, name=name, type=kind, kind=kind.split("(", 1)[0]) for name, kind in existing]
    if not columns:
        raise ConfigError("SFTP 目标表必须包含配置的非分区列")
    verify_table_schema(table, table_name, columns)
    select_columns = ", ".join(require_identifier(col.name, "目标列名") for col in columns)

    def check_sizes() -> None:
        index = 0
        for rows in batches_factory():
            for row in rows:
                index += 1
                for cell in row:
                    size = _cell_bytes(cell)
                    if size > MAX_CELL_BYTES:
                        raise SystemExit(
                            f"第 {index:,} 行有单元格大小 {size:,} 字节，超过单列上限（约 {MAX_CELL_BYTES:,} 字节）；"
                            "请检查源文件里是否有超长字段"
                        )

    retry_call(check_sizes, attempts=max(1, retries), base_delay=10, desc="写前单格大小检查")
    stage_name = f"tmp_sftp2ods_{uuid.uuid4().hex}"
    stage_ddl = build_table_ddl(
        project, stage_name, columns, "SFTP 暂存数据，校验后发布", lifecycle_days=1, staging=True
    )
    verified = published = False
    written = 0
    try:
        run_sql_with_timeout(o, stage_ddl, timeout=timeout, desc=f"建暂存表 {stage_name}")
        stage = o.get_table(f"{project}.{stage_name}")
        verify_table_schema(stage, stage_name, columns)

        def publish() -> int:
            nonlocal verified, published, written
            if not verified:
                drop_partition(o, project, stage_name, spec, timeout=timeout)
                add_partition(o, project, stage_name, spec, timeout=timeout)
                written = 0
                with stage.open_writer(partition=spec, reopen=True) as writer:
                    for rows in batches_factory():
                        writer.write(rows)
                        written += len(rows)
                        progress_log(f"{stage_name} pt={partition_value} 暂存", written, "行")
                if total is not None and written != total:
                    raise RuntimeError(f"暂存写入行数异常：计划 {total:,}，实际 {written:,}")
                actual = count_partition(o, project, stage_name, partition_value, timeout=timeout)
                if actual != written:
                    raise RuntimeError(f"暂存校验行数不一致：写入 {written:,}，查询 {actual:,}")
                verified = True
            run_sql_with_timeout(
                o,
                f"insert overwrite table {project}.{table_name} partition ({_sql_spec(spec)}) "
                f"select {select_columns} from {project}.{stage_name} "
                f"where {PARTITION_COLUMN} = '{partition_value}'",
                timeout=timeout,
                desc=f"发布分区 {table_name} pt={partition_value}",
            )
            published = True
            return written

        try:
            return retry_call(
                publish,
                attempts=max(1, retries),
                base_delay=10,
                desc=f"{table_name} pt={partition_value} 暂存/发布",
            )
        except Exception as exc:
            raise RuntimeError(
                f"{table_name} pt={partition_value} 发布失败；未提前删除正式分区。"
                f"暂存表 {project}.{stage_name} 已保留（生命周期 1 天）；"
                f"发布可能已提交，请核对云端实例及正式分区后重跑：{exc}"
            ) from exc
    finally:
        if published:
            try:
                run_sql_with_timeout(
                    o, f"drop table if exists {project}.{stage_name}", timeout=timeout, desc="清理暂存表"
                )
            except Exception as exc:
                log(f"  警告：暂存表 {project}.{stage_name} 清理失败，将由生命周期回收：{redact(exc)}")
        else:
            log(f"  提示：暂存表 {project}.{stage_name}（如已创建）已保留；正式分区未提前删除")


def _partition_literal(partition_value) -> str:
    """拼进 SQL 的分区值：白名单 + 引号转义。

    MaxCompute 没有绑定参数，分区值只能拼进语句；白名单把"能拼什么"锁死
    （pt 恒为 8 位业务日），点号/引号/反斜杠/空格等注入面直接归零。
    """
    text = str(partition_value)
    if not _PARTITION_VALUE_RE.match(text):
        raise ConfigError(f"分区值必须是 8 位业务日 yyyyMMdd：{text!r}")
    return text.replace("'", "''")


def count_partition(o, project: str, table_name: str, partition_value: str, timeout: int = SQL_TIMEOUT_SECONDS) -> int:
    """SELECT COUNT(*) 校验分区行数（用于写后核对）。"""
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    literal = _partition_literal(partition_value)
    sql = f"select count(*) as cnt from {project}.{table_name} where {PARTITION_COLUMN} = '{literal}'"
    instance = run_sql_with_timeout(o, sql, timeout=timeout, desc=f"校验 {table_name} 行数")
    with instance.open_reader() as reader:
        for row in reader:
            # reader 的行既可能是按列名取值（pyodps Record），也可能是元组（测试替身）
            try:
                return int(row["cnt"])
            except (TypeError, KeyError, IndexError):
                return int(row[0])
    # count(*) 必然返回一行：走到这里说明结果集为空（SQL 没真正执行/reader 异常）。
    # 返回 0 会把"没读到结果"伪装成"分区 0 行"——写前保护会据此误判、写后校验会报出
    # "写入 N 查询 0"这类误导性的不一致，宁可明确报错
    raise RuntimeError(f"校验 SQL 未返回行，无法确认分区 {partition_value} 的行数")
