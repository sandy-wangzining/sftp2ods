# -*- coding: utf-8 -*-
"""命令行入口：体检（--check）、试跑（--dry-run）、正式同步、交互式建配置（--init）。

一次运行的完整流程（run_sync）：
    1) 解析参数 → 列远端文件 → 缺文件核对（缺了直接失败 + 飞书告警）
    2) 逐日期：跳过已上传 → 下载（.part 校验大小）→ 解析数行数（先全量校验一遍）
    3) 写库：自动建表/校验结构 → 删分区 → Tunnel 分批写入 → count(*) 行数核对
    4) 台账记录每个文件（表/pt/大小/行数），校验通过才落账；失败中断后重跑幂等自愈
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import VERSION
from . import mc as mc_mod
from . import parse as parse_mod
from . import sftp as sftp_mod
from . import state as state_mod
from .config import (
    build_context_doc,
    build_job_summary,
    check_block_types,
    collect_warnings,
    get_mc_profile_meta,
    job_tz_of,
    load_json_file,
    normalize_job,
    render_job,
    resolve_download_dir,
    resolve_target,
    validate_job,
)
from .dates import (
    DEFAULT_TZ,
    env_bizdate,
    expected_latest,
    load_zone,
    norm_date,
    parse_day_arg,
    plan_dates,
)
from .notify import notify
from .utils import (
    ConfigError,
    FatalSourceError,
    RunLock,
    as_bool,
    collect_secret_values,
    log,
    redact_secrets,
    reset_log_once,
    setup_console,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.json"
MISSING_SHOW_LIMIT = 20  # 飞书/日志里最多列多少个缺失日期（防刷屏）


# =============================================================================
# 参数与工具
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义（help 文案就是用户文档的第一入口，改参数时同步改 README）。"""
    parser = argparse.ArgumentParser(
        prog="sftp2ods",
        description="通用 SFTP 按天文件（CSV/TSV）→ MaxCompute ODS（列展开宽表 + pt 分区，先删再填）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  sftp2ods --init                                     # 交互式生成一份作业配置（新手推荐）\n"
            "  sftp2ods --job jobs/demo.json --check               # 体检：配置 + SFTP 连通 + 目标表结构\n"
            "\n"
            "  # 试跑：只下载/解析数行数，不写库\n"
            "  sftp2ods --job jobs/demo.json --bizdate 20260920 --dry-run\n"
            "\n"
            "  # 正式同步：调度传业务日（只处理该日期的文件）；pt = 文件日期\n"
            "  sftp2ods --job jobs/demo.json --bizdate ${bizdate}\n"
            "\n"
            "  # 补数（闭区间）；不传 --bizdate 时默认处理远端全部未上传日期\n"
            "  sftp2ods --job jobs/demo.json --start-date 2026-09-01 --end-date 2026-09-10\n"
            "\n"
            f"占位符：{build_context_doc()}\n"
        ),
    )
    parser.add_argument("--job", default="", help="作业配置文件（jobs/*.json）")
    parser.add_argument("--init", action="store_true", help="交互式生成作业配置（生成后自己核对密钥，再 --check）")
    parser.add_argument("--init-out", default="", help="--init 的输出路径（默认 jobs/<作业名>.json）")
    parser.add_argument("--config", default="", help=f"可选的共享凭证文件（默认 {DEFAULT_CONFIG_PATH}，没有就不读）")
    parser.add_argument("--check", action="store_true", help="只体检：配置 + SFTP 连通 + 目标表结构（不下载）")
    parser.add_argument(
        "--bizdate", default="", help="只处理该日期的文件（yyyyMMdd 或 yyyy-MM-dd；默认读环境变量 bizdate）"
    )
    parser.add_argument("--start-date", default="", help="补数/调试：起始日期（含），可与 --end-date 单独使用")
    parser.add_argument("--end-date", default="", help="补数/调试：结束日期（含）")
    parser.add_argument("--force", action="store_true", help="忽略已上传台账，全部重写（缺文件核对仍然生效）")
    parser.add_argument("--dry-run", action="store_true", help="只下载并数行数，不写 MaxCompute")
    parser.add_argument("--no-notify", action="store_true", help="缺文件/空目录时只报错，不发飞书")
    parser.add_argument("--endpoint", default="", help="MaxCompute endpoint（覆盖作业里的配置）")
    parser.add_argument("--mc-profile", default="", help="作业 maxcompute/profiles 里的 profile 名（默认 default）")
    parser.add_argument("--cli-profile", default="", help="aliyun CLI profile 名（本机调试凭证兜底，默认 current）")
    parser.add_argument(
        "--sql-timeout",
        type=int,
        default=mc_mod.SQL_TIMEOUT_SECONDS,
        help=f"单条 MaxCompute SQL 最长等待秒数，默认 {mc_mod.SQL_TIMEOUT_SECONDS}；0 表示不限制",
    )
    parser.add_argument("--log-file", default="", help="日志同时写一份到该文件（追加，UTF-8）")
    parser.add_argument("--version", action="version", version=f"sftp2ods {VERSION}")
    return parser


def _check_cli_args(args) -> str:
    """命令行参数自身的校验（格式 / 互斥），返回错误文本（空串 = 通过）。

    在运行锁、读配置、连 SFTP 之前做：README 的退出码约定里 2 = 参数问题（没做过任何
    远端操作）。日期参数写错（如 --start-date 2026-9-1）以前会掉进运行期错误报 1，
    调度侧按码分流时会把"命令打错"当成"数据问题"。
    """
    if args.sql_timeout < 0:
        # 负数在 run_sql_with_timeout 里会被当成"0=不限制"，与用户直觉相反（想调小却等成无限）
        return "--sql-timeout 不能为负（0 表示不限制）"
    try:
        bizday = parse_day_arg(args.bizdate) if args.bizdate else None
        start = norm_date(args.start_date, "--start-date") if args.start_date else ""
        end = norm_date(args.end_date, "--end-date") if args.end_date else ""
    except SystemExit as exc:
        return str(exc)
    if bizday and (start or end):
        return "--bizdate 与 --start-date/--end-date 互斥，请二选一"
    if start and end and start > end:
        return f"--end-date（{end}）不能早于 --start-date（{start}）"
    return ""


def _open_log_file(path_text: str):
    """打开 --log-file 指定的日志文件（追加、UTF-8、父目录自动创建）；没指定返回 None。"""
    if not path_text:
        return None
    path = Path(path_text)
    if path.is_dir():
        raise SystemExit(f"--log-file 指向的是目录，需要给文件名：{path}（如 {path / 'run.log'}）")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"--log-file 打不开：{path}（{exc}）") from exc


def _detach_log_sink(handle) -> None:
    """摘掉日志文件并关闭（同一进程里 main 可能被调用多次，残留的 handle 会写坏日志）。"""
    if handle is None:
        return
    from .utils import remove_log_sink

    remove_log_sink(handle)


def _lock_path(job_path: Path) -> Path:
    """每个作业一把运行锁（不同作业可并行，同一作业不会重复跑）。

    优先放工具目录下 .run-locks/；工具目录不可写（如 pip 装在只读位置）时退回系统临时目录。
    锁名带路径哈希：jobs/a/api.json 与 jobs/b/api.json 同名不同作业，只按文件名会互相阻塞。
    """
    stem = job_path.stem or "job"
    digest = hashlib.sha1(str(job_path).encode("utf-8")).hexdigest()[:8]
    name = f"{stem}-{digest}"
    candidates = [ROOT / ".run-locks", Path(tempfile.gettempdir()) / "sftp2ods-locks"]
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            # 探测文件名必须唯一（mkstemp）：并发启动时别的进程先 unlink 会让探测抛 FileNotFoundError
            handle, probe = tempfile.mkstemp(prefix=".probe-", dir=str(base))
            os.close(handle)
            os.unlink(probe)
            return base / f"{name}.lock"
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / f"sftp2ods-{name}.lock"


def _redact_job(job: dict, text) -> str:
    """作业上下文下的脱敏：先按配置里的密钥值遮，再走形态规则兜底。"""
    return redact_secrets(collect_secret_values(job), str(text))


def _cred_source_label(config_path: Path, job_path: Path) -> str:
    """凭证来源说明（只写位置，不含密钥）。"""
    if config_path.is_file():
        return f"{job_path.name} / {config_path.name}"
    return job_path.name


def _notifier(job: dict, args):
    """构造飞书通知函数（webhook/enabled 都从作业配置取；--no-notify 优先）。"""
    notify_cfg = job.get("notify") or {}
    webhook = str(notify_cfg.get("webhook") or "")
    enabled = as_bool(notify_cfg.get("enabled"), default=True, field="notify.enabled") and not args.no_notify
    return lambda title, lines, footer: notify(webhook, title, lines, footer, enabled=enabled)


def _alert_footer(job: dict, job_path: Path, project: str, table_name: str, source) -> str:
    """告警卡片脚注：任务 / 目标表 / 远端位置。"""
    job_name = job.get("job") or job_path.stem
    root = source.root or "."
    return f"作业 {job_name} · 目标 {project}.{table_name} · 远端 {source.username}@{source.host}{root}"


def _lifecycle_days(target_cfg: dict) -> int | None:
    """target.lifecycle_days → int | None（配置阶段已校验，这里兜底防被单独调用）。"""
    raw = target_cfg.get("lifecycle_days")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) != int(raw) or int(raw) <= 0:
        raise SystemExit(f"target.lifecycle_days 必须是正整数（天），实际 {raw!r}")
    return int(raw)


def _local_candidates(download_dir: Path, item, *, allow_legacy: bool = False) -> list[Path]:
    """一个文件的本地候选路径：当前口径 + 旧脚本口径（迁移后复用旧目录里的文件）。"""
    paths = [download_dir / item.ledger_key]
    if allow_legacy and item.ledger_key != item.name:
        paths.append(download_dir / item.name)
    return paths


def _pick_local(download_dir: Path, item, *, allow_legacy: bool = False) -> Path | None:
    """本地已有的、大小一致的文件路径（没有返回 None）。

    allow_legacy 只给「判定已上传」用：旧脚本把文件平铺下载在 download_dir 下、台账键
    是文件名，只有台账明确记着"这个键属于这一天"时才敢认那个平铺文件。
    **下载与写库路径必须用默认值**：平铺目录里的同名文件可能属于别的日期
    （文件名不含日期时尤其如此），大小恰好相同就会被静默当成本日数据写进库。
    """
    for candidate in _local_candidates(download_dir, item, allow_legacy=allow_legacy):
        if state_mod.local_ready(candidate, item.size):
            return candidate
    return None


def _pick_latest_sample(files_by_date: dict):
    """取一个"最新日期下的第一个文件"当样本（向导/体检展示用）。"""
    for date in sorted(files_by_date, reverse=True):
        items = files_by_date[date]
        if items:
            return date, items[0]
    return None


# =============================================================================
# 体检
# =============================================================================


def run_check(job: dict, config: dict, args, job_path: Path, config_path: Path | None = None, bizdate: str = "") -> int:
    """体检：配置概要 + SFTP 真实列目录 + 目标表结构。新接一个源时先跑这个。"""
    log("== 作业概要 ==")
    for line in build_job_summary(job):
        log(_redact_job(job, line))

    log("")
    log("== SFTP 连通性（真实列目录） ==")
    source = sftp_mod.SftpSource(job.get("sftp") or {}, job.get("source") or {})
    try:
        files_by_date = source.list_files()
        sample = _pick_latest_sample(files_by_date)
        log(
            f"  ✅ 连接成功：远端共 {len(files_by_date)} 个业务日期，{sum(len(v) for v in files_by_date.values()):,} 个文件"
        )
        if sample:
            date, item = sample
            log(f"  最新样本：{date}/{item.name}（{item.size:,} 字节）")
        else:
            log("  ⚠️ 远端目录下没有匹配文件（检查 source.root / file_regex / 源方是否已产出）")
        missing_cfg = job.get("missing") or {}
        if as_bool(missing_cfg.get("check"), default=True, field="missing.check") and files_by_date:
            tz = load_zone(str(missing_cfg.get("timezone") or DEFAULT_TZ))
            expected = expected_latest(tz, str(missing_cfg.get("grace") or ""))
            # 带 --bizdate 时只核对那一天（与正式跑一致）；否则核对接入以来的完整性
            missing, _, r_start, r_end = plan_dates(sorted(files_by_date), bizdate, "", "", expected, True)
            if missing:
                shown = "、".join(missing[:MISSING_SHOW_LIMIT]) + (" 等" if len(missing) > MISSING_SHOW_LIMIT else "")
                log(f"  ⚠️ 缺文件核对未通过：{shown}（共 {len(missing)} 个；区间 {r_start} ~ {r_end}）")
            elif bizdate:
                log(f"  ✅ 业务日 {bizdate} 的远端文件存在")
            elif r_start and r_start <= r_end:
                log(f"  ✅ 缺文件核对通过（{r_start} ~ {r_end} 完整）")
            else:
                # 远端最早日期比预期最新还晚（首次接入/源方刚开账）：核对区间为空，不算"缺文件"
                log(f"  ✅ 缺文件核对：远端最早 {min(files_by_date)} 晚于预期最新 {expected}，无需核对")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"  ❌ {_redact_job(job, exc)}")
        return 1

    log("")
    log("== MaxCompute 目标表 ==")
    try:
        project, table_name = resolve_target(job, config, args)
        parse_spec = parse_mod.ParseSpec(job.get("parse") or {})
        profile = get_mc_profile_meta(config, job, args)
        o = mc_mod.connect_odps(
            config,
            _cred_source_label(config_path or DEFAULT_CONFIG_PATH, job_path),
            profile,
            project,
            endpoint=str(args.endpoint or ""),
            cli_profile=args.cli_profile,
        )
        if not o.exist_table(table_name):
            log(f"  ⭕ 表不存在（运行同步时自动创建）：{project}.{table_name}（{len(parse_spec.columns)} 列 + pt）")
        else:
            table = o.get_table(table_name)
            mc_mod.verify_table_schema(table, table_name, parse_spec.columns)
            log(f"  ✅ {table_name} 结构符合（{len(parse_spec.columns)} 列 + pt）")
    except SystemExit as exc:
        log(f"  ❌ {_redact_job(job, exc)}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"  ❌ 连接/校验失败：{_redact_job(job, exc)}")
        return 1

    log("")
    log("检查通过。")
    return 0


# =============================================================================
# 正式同步
# =============================================================================


def run_sync(job: dict, config: dict, args, job_path: Path, bizdate: str = "", config_path: Path | None = None) -> int:
    """正式同步：列目录 → 缺文件核对 → 下文件 → 解析 → 先删再填 pt → 行数校验。"""
    job_name = job.get("job") or job_path.stem
    source_cfg = job.get("source") or {}
    target_cfg = job.get("target") or {}
    parse_spec = parse_mod.ParseSpec(job.get("parse") or {})
    source = sftp_mod.SftpSource(job.get("sftp") or {}, source_cfg)
    project, table_name = resolve_target(job, config, args)
    notifier = _notifier(job, args)
    footer = _alert_footer(job, job_path, project, table_name, source)

    # ---- 参数 ----
    start = norm_date(args.start_date, "--start-date") if args.start_date else ""
    end = norm_date(args.end_date, "--end-date") if args.end_date else ""
    if bizdate and (start or end):
        raise ConfigError("--bizdate 与 --start-date/--end-date 互斥，请二选一")
    if start and end and start > end:
        raise ConfigError(f"--end-date（{end}）不能早于 --start-date（{start}）")

    # ---- 台账 ----
    download_dir = resolve_download_dir(job, job_path)
    ledger_path = download_dir / state_mod.STATE_FILE_NAME
    ledger = state_mod.load_state(ledger_path)

    # ---- ① 列远端文件 + 缺文件核对 ----
    log(f"列出远端文件（{source.username}@{source.host}:{source.port} {source.root or '.'}）...")
    try:
        files_by_date = source.list_files()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 连接/列目录失败统一按失败退出
        log(f"❌ 列远端文件失败：{_redact_job(job, exc)}")
        return 1

    if not files_by_date:
        log(f"❌ 远端目录（{source.root or '.'}）下没有任何匹配文件")
        notifier(
            f"{job_name}：远端目录为空",
            [f"远端目录 `{source.root or '.'}` 下没有任何匹配文件", f"匹配规则：`{source_cfg.get('file_regex')}`"],
            footer,
        )
        return 1

    missing_cfg = job.get("missing") or {}
    check_missing = as_bool(missing_cfg.get("check"), default=True, field="missing.check")
    tz = load_zone(str(missing_cfg.get("timezone") or DEFAULT_TZ))
    expected = expected_latest(tz, str(missing_cfg.get("grace") or "")) if check_missing else ""
    all_dates = sorted(files_by_date)
    missing, proc_dates, range_start, range_end = plan_dates(all_dates, bizdate, start, end, expected, check_missing)
    if missing:
        shown = "、".join(missing[:MISSING_SHOW_LIMIT]) + (" 等" if len(missing) > MISSING_SHOW_LIMIT else "")
        log(
            f"❌ 【{job_name}】缺少文件：{shown}（共 {len(missing)} 个）；"
            f"已有最新：{max(all_dates)}，预期到：{range_end}"
        )
        notifier(
            f"{job_name}：文件缺失",
            [
                f"**缺少日期**：{shown}（共 {len(missing)} 个）",
                f"**当前最大**：{max(all_dates)}",
                f"**预期到**：{range_end}",
            ],
            footer,
        )
        return 1
    if check_missing and range_start and range_end and range_start > range_end:
        # 核对区间为空 = 用户给的日期范围与远端完全没有交集（区间下界晚于数据上界）。
        # 此时缺文件核对会整个被跳过、也没有任何日期可处理，静默 rc=0 会让"补数日期打错"
        # 看起来像成功；对齐"宁可失败"的红线，这里明确失败。
        log(
            f"❌ 【{job_name}】日期范围与远端没有交集（核对区间为空：{range_start} 晚于 {range_end}）；"
            f"远端数据范围 {min(all_dates)} ~ {max(all_dates)}，请检查 --start-date/--end-date 是否写错"
        )
        return 1
    if check_missing and range_start and range_start <= range_end:
        log(
            f"远端共 {len(all_dates)} 个日期（核对区间 {range_start} ~ {range_end} 完整），本次处理 {len(proc_dates)} 个"
        )
    else:
        log(f"远端共 {len(all_dates)} 个日期，本次处理 {len(proc_dates)} 个")
    if not proc_dates:
        # 缺文件核对关掉时会出现"指定了业务日/区间但远端没有文件"：明确打一条，避免 rc=0 看着像做过事
        log("⚠️ 本次没有要处理的日期（业务日/区间内远端没有匹配文件）")

    # ---- ② 连 MaxCompute（dry-run 跳过，纯看数不碰库）----
    table = o = None
    if not args.dry_run:
        try:
            profile = get_mc_profile_meta(config, job, args)
            o = mc_mod.connect_odps(
                config,
                _cred_source_label(config_path or DEFAULT_CONFIG_PATH, job_path),
                profile,
                project,
                endpoint=str(args.endpoint or ""),
                cli_profile=args.cli_profile,
            )
            table = mc_mod.ensure_target_table(
                o,
                project,
                table_name,
                parse_spec.columns,
                comment=str(target_cfg.get("comment") or ""),
                stored_as=str(target_cfg.get("stored_as") or ""),
                lifecycle_days=_lifecycle_days(target_cfg),
                timeout=args.sql_timeout,
            )
            log(f"表就绪：{project}.{table_name}（{len(parse_spec.columns)} 列 + pt）")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"❌ 连接/建表失败：{_redact_job(job, exc)}")
            return 1

    # ---- ③ 逐日期：跳过已上传 → 下载 → 解析 → 写 pt → 校验 ----
    allow_empty = as_bool(target_cfg.get("allow_empty"), default=True, field="target.allow_empty")
    uploaded = skipped = total_rows = 0
    for date in proc_dates:
        items = files_by_date[date]

        def done(item, date=date):
            """台账 + 本地文件都齐（大小一致）→ 该文件已上传；远端文件更新过会自动重下重写。

            allow_legacy=True：允许回退到旧脚本的平铺文件——台账已经证明那个键属于这一天，
            跳过是安全的；下载阶段绝不会这么认（见 _pick_local）。
            """
            keys = (item.ledger_key,) if item.ledger_key == item.name else (item.ledger_key, item.name)
            return state_mod.record_of(ledger, keys, item.size, table_name, date, project=project) and _pick_local(
                download_dir, item, allow_legacy=True
            )

        if not args.force and all(done(item) for item in items):
            skipped += 1
            state_mod.log_skip(date, items)
            continue

        # 下载该日期下所有文件（本地已有且大小一致的不重下）
        local_paths: list[Path] = []
        for item in items:
            existing = _pick_local(download_dir, item)
            if existing is not None:
                local_paths.append(existing)
                continue
            log(f"下载 {date}/{item.name}（{item.size:,} 字节）...")
            try:
                local_paths.append(source.download(item, download_dir / item.ledger_key))
            except FatalSourceError as exc:
                log(f"❌ {_redact_job(job, exc)}")
                return 1
            except Exception as exc:  # noqa: BLE001
                log(f"❌ 下载 {item.name} 失败：{_redact_job(job, exc)}")
                return 1

        # 先完整解析一遍数行数（表头校验、合计行校验都在这趟完成；坏文件在写库前就拦住）
        file_rows: dict[str, int] = {}
        try:
            for path in local_paths:
                stats = {"skipped": 0}
                file_rows[path.name] = sum(1 for _ in parse_spec.iter_rows(path, stats))
                if stats["skipped"]:
                    log(f"  {path.name}：跳过 {stats['skipped']} 行（命中 skip_if_empty 的空键行）")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"❌ 解析失败：{_redact_job(job, exc)}")
            return 1
        rows = sum(file_rows.values())
        total_rows += rows
        log(f"{date}：{len(local_paths)} 个文件，{rows:,} 行 → pt={date}")
        if args.dry_run:
            continue
        if rows == 0 and not allow_empty:
            log(f"❌ pt={date} 解析出 0 行，且 target.allow_empty=false，未写库")
            return 1
        if rows == 0:
            # 写空分区前先看一眼现有分区：把一个本来有数据的分区覆盖成 0 行几乎总是异常
            # （源文件被截断成只剩表头、关键列整列变空被 skip_if_empty 滤光），而"先删再填"
            # 不可逆。零交易的正常场景下分区本来就不存在（查询得 0），不会误伤。
            # --force 是"我知道我在干什么"的显式开关，允许绕过这层保护。
            if not args.force:
                try:
                    existing = mc_mod.count_partition(o, project, table_name, date, timeout=args.sql_timeout)
                except Exception as exc:  # noqa: BLE001 - 查询失败按写库失败处理（宁可不写）
                    log(f"❌ pt={date} 写前查询现有分区行数失败：{_redact_job(job, exc)}")
                    return 1
                if existing:
                    log(
                        f"❌ pt={date} 本次解析出 0 行，但该分区现有 {existing:,} 行；为避免清空已有数据，"
                        f"本次未写库。确认源方确实把这天改成了零行后，可加 --force 重写该分区"
                    )
                    return 1
            log(f"  警告：pt={date} 解析出 0 行，将写入空分区（源文件不该为空时请检查解析配置）")

        try:
            mc_mod.write_partition(
                table, table_name, date, lambda paths=local_paths: parse_mod.iter_batches(paths, parse_spec), rows
            )
            verified = mc_mod.count_partition(o, project, table_name, date, timeout=args.sql_timeout)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - 写库/Tunnel 失败统一按失败退出（调度可告警）
            log(f"❌ 写库失败：{_redact_job(job, exc)}")
            return 1
        if verified != rows:
            log(f"❌ {date} 写后校验不一致：写入 {rows:,}，查询 {verified:,}")
            return 1
        # 写入并校验成功后才记台账（失败中断后重跑会重试这个日期）
        for item in items:
            ledger[item.ledger_key] = {
                "project": project,
                "table": table_name,
                "pt": date,
                "size": item.size,
                "rows": file_rows[item.name],
            }
        try:
            state_mod.save_state(ledger_path, ledger)
        except OSError as exc:
            # 数据本身已经写好并校验过；台账写不进去只是"下次会重传"，
            # 但既然本地状态已经异常，按失败退出让调度可见（重跑幂等）
            log(f"❌ 台账写入失败（数据已写入 MaxCompute）：{ledger_path}（{exc}）；请检查磁盘/权限，重跑即可")
            return 1
        uploaded += 1
        log(f"  已写入 {project}.{table_name} pt={date}，校验 {verified:,} 行")

    if args.dry_run:
        log(f"dry-run 完成：{len(proc_dates) - skipped} 个日期，共 {total_rows:,} 行（未写库）")
    else:
        log(f"完成：写库 {uploaded} 个日期，跳过 {skipped} 个")
    return 0


# =============================================================================
# 入口
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回退出码：0 成功 / 1 运行失败 / 2 参数问题 / 130 用户中断。"""
    setup_console()
    # 告警去重记录按"每次运行"清空：同一进程里 main 被调用多次时，上一轮的告警会吞掉这一轮同名的那条
    reset_log_once()
    args = build_parser().parse_args(argv)

    log_handle = _open_log_file(args.log_file)
    if log_handle is not None:
        from .utils import add_log_sink

        add_log_sink(log_handle)

    if args.init:  # 交互式建配置：不需要 --job
        from .init_wizard import run_init

        def _wizard_ask(prompt: str = "") -> str:
            """向导的提问也走 log：--log-file 里能看到整套问答（只记问题，不记回答——回答里是密钥）。"""
            log(prompt)
            return input()

        try:
            return run_init(args.init_out, ask=_wizard_ask, echo=log)
        finally:
            _detach_log_sink(log_handle)

    problem = _check_cli_args(args)
    if problem:
        log(f"❌ {problem}")
        _detach_log_sink(log_handle)
        return 2

    if not args.job:
        log("请用 --job 指定作业配置文件（第一次接新源可以先用 `sftp2ods --init` 生成）")
        _detach_log_sink(log_handle)
        return 2

    job_raw: dict = {}
    try:
        # 凭证来源：作业文件自带；--config/默认 config.json 只在存在时作为补充（可选）
        if args.config:
            config_path = Path(args.config)
            config = load_json_file(config_path, "凭证/密钥文件")
        else:
            config_path = DEFAULT_CONFIG_PATH
            config = load_json_file(config_path, "凭证/密钥文件") if config_path.is_file() else {}

        job_path = Path(args.job).resolve()
        job_raw = load_json_file(job_path, "作业配置文件")
        # 类型检查提到最前面：下面 job_tz_of 就要取 missing.timezone，而 missing 写成字符串时是裸 traceback
        check_block_types(job_raw)

        # 业务日：--bizdate > 环境变量（DataWorks）> 不设置（处理远端全部未上传日期）
        # 顺序要紧：先看显式 --bizdate，没有才读环境变量；环境变量畸形必须报错（静默回退会处理错日期）
        bizdate = ""
        if args.bizdate:
            base_day = parse_day_arg(args.bizdate)
            bizdate = base_day.strftime("%Y%m%d")
        else:
            from_env = env_bizdate(strict=not args.check)
            if from_env is not None:
                bizdate = from_env.strftime("%Y%m%d")
                base_day = from_env
            else:
                base_day = datetime.now(job_tz_of(job_raw)).date() - timedelta(days=1)

        job = render_job(job_raw, config, base_day)  # 替换 ${secrets.x}/${bizdate} 等占位符
        job = normalize_job(job)  # 补齐默认值，让配置尽量短
        validate_job(job)
        for warning in collect_warnings(job):  # 未知字段告警（拼写错误提示）
            log(f"⚠️ {warning}")

        if args.check:
            try:
                return run_check(job, config, args, job_path, config_path=config_path, bizdate=bizdate)
            except KeyboardInterrupt:
                log("已中断（体检未完成），退出")
                return 130

        try:
            with RunLock(_lock_path(job_path)):  # 同机同一作业互斥；不同作业可并行
                return run_sync(job, config, args, job_path, bizdate=bizdate, config_path=config_path)
        except SystemExit as exc:
            log(f"❌ {_redact_job(job, exc)}")
            return 1
        except KeyboardInterrupt:
            log("已中断（本次未完成；重跑同一命令即可，先删再填、幂等）")
            return 130
    except SystemExit as exc:
        # 准备阶段的配置错（bizdate 畸形、缺块、占位符写错、运行锁拿不到）统一按运行期错误的格式记一笔
        log(f"❌ {_redact_job(job_raw, exc)}")
        return 1
    except KeyboardInterrupt:
        log("已中断（尚未开始运行），退出")
        return 130
    finally:
        _detach_log_sink(log_handle)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
