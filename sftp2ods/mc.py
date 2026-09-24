# -*- coding: utf-8 -*-
"""MaxCompute：凭证、建表/结构校验、SQL 超时控制、分区覆盖写入、行数校验。

目标表形态：列展开宽表 + pt 分区（pt = 文件日期，普通分区表），与两个结算脚本一致。
写入是"先删再填"：删分区 → 建分区 → Tunnel 分批写 → 写后 count(*) 核对；
任何失败都会让调度看到非 0 退出码（旧分区可能已清空，重跑幂等自愈）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .parse import normalize_type
from .utils import log, progress_log, require_identifier, retry_call

try:
    from odps import ODPS
except ImportError:  # pragma: no cover
    ODPS = None

PARTITION_COLUMN = "pt"
SQL_TIMEOUT_SECONDS = 600
MAX_CELL_BYTES = 7_000_000  # MaxCompute 单列字符串上限 8MB，留余量提前报错
SQL_HEARTBEAT_SECONDS = 30
DEFAULT_ENDPOINT = "http://service.us-west-1.maxcompute.aliyun.com/api"


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
    ak_id, secret = _pick_aksk(profile or {})
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
        names = [cli_profile] if cli_profile else [cli_config.get("current")]
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


def connect_odps(config: dict, source_label: str, profile: dict, project: str, endpoint: str = "", cli_profile: str = ""):
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
    """提交 SQL 并等待完成：成功返回 / 失败抛错 / 超时主动 stop() 取消并抛 TimeoutError。"""
    instance = o.run_sql(sql)
    started = time.time()
    last_log = started
    while True:
        if instance.is_successful():
            return instance
        if instance.is_terminated():
            instance.wait_for_success(timeout=1)  # 触发一次，抛出带错误信息的异常
            return instance
        now = time.time()
        if timeout and timeout > 0 and now - started > timeout:
            try:
                instance.stop()
            except Exception:  # noqa: BLE001 - 取消失败不影响报错
                pass
            raise TimeoutError(f"{desc} 执行超过 {timeout} 秒，已主动停止")
        if timeout and timeout > 0 and now - last_log >= SQL_HEARTBEAT_SECONDS:
            log(f"    {desc} 还在执行（已等待 {int(now - started)} 秒，超时阈值 {timeout} 秒）...")
            last_log = now
        time.sleep(1)


# =============================================================================
# 目标表：DDL / 结构校验 / 自动建表
# =============================================================================


def build_table_ddl(
    project: str, table: str, columns, comment: str = "", stored_as: str = "", lifecycle_days: int | None = None
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
    if lifecycle_days:
        lines.append(f"lifecycle {int(lifecycle_days)}")
    table_comment = (comment or "SFTP 文件列展开").replace("'", "''")
    lines.append(f"tblproperties ('comment' = '{table_comment}')")
    return "\n".join(lines)


def _table_schema_of(table) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """取表的 (非分区列名→类型, 分区列名→类型)，名字按原样返回。"""
    schema = table.table_schema
    partition_names = [str(col.name) for col in schema.partitions]
    partitions = [(str(col.name), normalize_type(col.type)) for col in schema.partitions]
    columns = [(str(col.name), normalize_type(col.type)) for col in schema.columns if str(col.name) not in partition_names]
    return columns, partitions


def verify_table_schema(table, table_name: str, columns) -> None:
    """校验已存在的表结构与配置一致；不一致直接报错拒绝写入。

    检查顺序：不是视图 → 不是事务表 → 列名（忽略大小写，顺序必须一致）→ 列类型 → 分区只有 pt。
    顺序必须按位对齐：Tunnel 按位置写列，顺序错了就是"每列都写错位"而且行数校验照样通过。
    """
    if getattr(table, "is_virtual_view", False) or getattr(table, "is_materialized_view", False):
        raise SystemExit(f"{table_name} 是视图，不能作为写入目标")
    if getattr(table, "is_transactional", False):
        raise SystemExit(f"{table_name} 是事务表，本框架按「先删再填普通分区表」写入；请换表名或先 drop 后重建")
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
                f"{table_name}.{name} 类型是 {existing_type}，配置要求 {wanted_type}；"
                f"改配置或先 drop 重建该表"
            )
    if [name.lower() for name, _ in partitions] != [PARTITION_COLUMN.lower()]:
        raise SystemExit(
            f"{table_name} 的分区列是 {[name for name, _ in partitions]}，框架要求只有 [{PARTITION_COLUMN}]"
        )
    for name, type_text in partitions:
        if type_text != "string":
            raise SystemExit(f"{table_name}.{name} 分区类型是 {type_text}，框架要求 string")


def ensure_target_table(
    o, project: str, table_name: str, columns, comment: str = "", stored_as: str = "",
    lifecycle_days: int | None = None, timeout: int = SQL_TIMEOUT_SECONDS,
):
    """表不存在则按列配置建表；存在则校验结构，返回 table 对象。"""
    ddl = build_table_ddl(project, table_name, columns, comment, stored_as, lifecycle_days)
    run_sql_with_timeout(o, ddl, timeout=timeout, desc=f"建表 {table_name}")  # create if not exists
    table = o.get_table(table_name)
    verify_table_schema(table, table_name, columns)
    return table


# =============================================================================
# 分区覆盖写入 / 行数校验
# =============================================================================


def write_partition(
    table, table_name: str, partition_value: str, batches_factory, total: int | None = None, retries: int = 3
) -> int:
    """先删再填一个分区（delete → create → Tunnel 写入），整段失败自动重试。

    - batches_factory：一个"可重复调用"的函数，每次返回从头开始的「批次迭代器」；
      用工厂而不是列表，是为了支持失败重试时重新读一遍，同时内存里只留一个批次。
    - total：预期行数；写完后核对，不一致报错（调用方再与 count(*) 二次校验）。
    - 单格超过 MAX_CELL_BYTES 直接报错；检查必须在删分区之前——这类值永远写不进去，
      先删后失败等于白丢一天数据。
    """
    spec = f"{PARTITION_COLUMN}={partition_value}"
    # 每一次重试都是"先删分区、再重新写"：只要删成功过，这个分区就已经不在原位了。
    # 重试全部失败时，旧数据不会自己回来——必须让调度侧知道"这里可能缺数、要重跑"。
    deleted_once = False

    def _check_cell_sizes() -> None:
        """通读一遍所有行，找出超长单元格；行号从 1 数起，报错里能定位。"""
        index = 0
        for rows in batches_factory():
            for row in rows:
                index += 1
                for cell in row:
                    size = _cell_bytes(cell)
                    if size > MAX_CELL_BYTES:
                        raise SystemExit(
                            f"第 {index:,} 行有单元格大小 {size:,} 字节，超过单列上限（约 {MAX_CELL_BYTES:,} 字节）；"
                            f"请检查源文件里是否有超长字段"
                        )

    def _do() -> int:
        """完整的"校验 → 删 → 建 → 写"一趟，交给 retry_call 重试（每趟都从头读数据）。"""
        nonlocal deleted_once
        _check_cell_sizes()
        # 先置位再删：delete_partition 可能在服务端已经删掉分区后才抛异常，
        # 此时 deleted_once=False 会把"分区可能已丢数"的提示吞掉。
        deleted_once = True
        table.delete_partition(spec, if_exists=True)  # 先删：重跑/补数不会叠加
        table.create_partition(spec, if_not_exists=True)
        # 重试要重新读一遍数据，所以 writer 与 written 都在重试时重置。
        # reopen=True：不复用上一次失败留下的 Tunnel 上传会话——复用会把上次已上传的块
        # 与本轮全量一起提交，结果是分区里出现重复行。
        with table.open_writer(partition=spec, reopen=True) as writer:  # Tunnel 写入
            written = 0
            for rows in batches_factory():
                writer.write(rows)
                written += len(rows)
                progress_log(f"{table_name} pt={partition_value} 写入", written, "行")
        return written

    try:
        written = retry_call(
            _do, attempts=max(1, retries), base_delay=10, desc=f"{table_name} pt={partition_value} 写入"
        )
    except Exception as exc:  # noqa: BLE001 - 重试耗尽后统一改写错误信息
        # KeyboardInterrupt / SystemExit 不经 except Exception，Ctrl+C 的 130 出口不受影响
        if deleted_once:
            raise RuntimeError(
                f"{table_name} pt={partition_value} 覆盖写入失败，分区可能已被清空或只写入了一部分"
                f"（覆盖写是「先删再填」，旧数据不会自动恢复）。请重跑本作业把该分区补回；"
                f"重跑会从头覆盖，不会叠加。原始错误：{exc}"
            ) from exc
        raise
    if total is not None and written != total:
        raise RuntimeError(f"{table_name} 写入行数异常：计划 {total:,}，实际 {written:,}")
    return written


def _cell_bytes(cell) -> int:
    """一个值的 UTF-8 字节数（None 计 0；Decimal 等按字符串形态计）。"""
    if cell is None:
        return 0
    if isinstance(cell, str):
        return len(cell.encode("utf-8"))
    return len(str(cell).encode("utf-8"))


def count_partition(o, project: str, table_name: str, partition_value: str, timeout: int = SQL_TIMEOUT_SECONDS) -> int:
    """SELECT COUNT(*) 校验分区行数（用于写后核对）。"""
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    literal = str(partition_value).replace("'", "''")  # 拼 SQL 前转义
    sql = f"select count(*) as cnt from {project}.{table_name} where {PARTITION_COLUMN} = '{literal}'"
    instance = run_sql_with_timeout(o, sql, timeout=timeout, desc=f"校验 {table_name} 行数")
    with instance.open_reader() as reader:
        for row in reader:
            # reader 的行既可能是按列名取值（pyodps Record），也可能是元组（测试替身）
            try:
                return int(row["cnt"])
            except (TypeError, KeyError, IndexError):
                return int(row[0])
    return 0
