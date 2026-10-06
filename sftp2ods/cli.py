# -*- coding: utf-8 -*-
"""命令行入口：体检（--check）、试跑（--dry-run）、正式同步、交互式建配置（--init）。

一次运行的完整流程（run_sync）：
    1) 解析参数 → 列远端文件 → 缺文件核对（缺了飞书告警 + 跳过缺失日期，照常同步已有文件；
       但显式点名单日（--bizdate/环境变量 bizdate）整天无文件时直接失败，防静默缺数）
    2) 逐日期：跳过已上传 → 下载（.part 校验大小）→ 解析数行数（先全量校验一遍）
    3) 写库：自动建表/校验结构 → 删分区 → Tunnel 分批写入 → count(*) 行数核对
    4) 台账记录每个文件（表/pt/大小/行数），校验通过才落账；失败中断后重跑幂等自愈
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import math
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
    redact,
    redact_secrets,
    reset_log_once,
    setup_console,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.json"
MISSING_SHOW_LIMIT = 20  # 飞书/日志里最多列多少个缺失日期（防刷屏）

_GETPASS_ERRORS = (EOFError, OSError)
_GetPassWarning = getattr(getpass, "GetPassWarning", None)
if isinstance(_GetPassWarning, type) and issubclass(_GetPassWarning, BaseException):
    _GETPASS_ERRORS = (*_GETPASS_ERRORS, _GetPassWarning)


def prompt_secret(prompt: str = "") -> str:
    """密钥输入：优先 getpass 不回显；没有 tty 时退回 input() 并明确告警会明文回显。"""
    if prompt:
        log(prompt)
    try:
        return getpass.getpass("")
    except _GETPASS_ERRORS:
        log("警告：无法隐藏输入，接下来的内容会明文回显在终端上")
        try:
            return input()
        except (EOFError, ValueError, RuntimeError) as exc:
            # stdin 关闭/无输入源：统一翻译成 EOFError（= 取消），别让裸 ValueError 冒泡——
            # 向导顶层若按 ValueError 判"取消"，会把向导内部无关的 ValueError 也一起吞掉
            raise EOFError("标准输入不可用") from exc


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
        "--bizdate",
        default=None,
        # 默认必须是 None 而不是 ""：空串无法区分"没传 --bizdate"与"显式传了空值"
        # （调度脚本 `--bizdate "$pt"` 且 $pt 未定义）——后者要报错，不能静默回退处理全部日期
        help="只处理该日期的文件（yyyyMMdd 或 yyyy-MM-dd；默认读环境变量 bizdate）",
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
        bizday = parse_day_arg(args.bizdate) if args.bizdate is not None else None
        start = norm_date(args.start_date, "--start-date") if args.start_date else ""
        end = norm_date(args.end_date, "--end-date") if args.end_date else ""
    except SystemExit as exc:
        # SystemExit(0)/空消息时 str(exc) 是空串，会被 main 当成"校验通过"继续跑——
        # 这里给一句兜底文案，保证"捕获到异常"一定对应"有错误信息"
        return str(exc) or f"参数校验失败（{type(exc).__name__}，无消息）"
    if bizday and (start or end):
        return "--bizdate 与 --start-date/--end-date 互斥，请二选一"
    if start and end and start > end:
        return f"--end-date（{end}）不能早于 --start-date（{start}）"
    return ""


def _open_log_file(path_text: str):
    """打开 --log-file 指定的日志文件（追加、UTF-8、父目录自动创建）；没指定返回 None。"""
    if not path_text:
        return None
    # 展开 ~：调度平台把参数写成 "~/logs/x.log"（带引号时 shell 不展开）时，
    # 字面量 "~" 会在 CWD 下建目录、日志落错位置（排障时以为进程没跑）
    path = Path(path_text).expanduser()
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

    锁目录优先级：环境变量 SFTP2ODS_LOCK_DIR > 工具目录下 .run-locks/ > 系统临时目录
    （工具目录不可写，如 pip 装在只读位置时）。用 SFTP2ODS_LOCK_DIR 可以把锁钉在与运行者
    身份/环境无关的同一目录上——否则"root 能写工具目录、普通用户退回 TMPDIR"这类差异会让
    同一作业的两个实例锁在不同文件上，互斥静默失效；指到共享存储（如 NFS）时多机也能互斥
    （文件系统不支持锁会告警并降级）。
    锁名带路径哈希：jobs/a/api.json 与 jobs/b/api.json 同名不同作业，只按文件名会互相阻塞。
    """
    stem = job_path.stem or "job"
    # sha256 截 16 位十六进制：sha1 只取 8 位（32 位）时不同作业有可观的碰撞概率，
    # 撞了会互相阻塞（解锁时还可能删错对方的锁）。
    # 摘要先 resolve（绝对化/展开 ~/消解 .. 与软链接）：同一作业用相对/绝对路径两种写法
    # 原来会落到两把锁上、互斥静默失效（两个进程同删同写一个分区）
    digest = hashlib.sha256(os.fsencode(str(Path(job_path).expanduser().resolve()).encode("utf-8"))).hexdigest()[:16]
    name = f"{stem}-{digest}"
    override = os.environ.get("SFTP2ODS_LOCK_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 显式指定的目录不可用要立刻失败：静默换目录等于互斥失效，正是这个开关要防的事
            raise SystemExit(f"SFTP2ODS_LOCK_DIR 指定的锁目录不可用（{exc}）：{base}") from exc
        return base / f"{name}.lock"
    candidates = [ROOT / ".run-locks", Path(tempfile.gettempdir()) / "sftp2ods-locks"]
    for index, base in enumerate(candidates):
        try:
            base.mkdir(parents=True, exist_ok=True)
            # 探测文件名必须唯一（mkstemp）：并发启动时别的进程先 unlink 会让探测抛 FileNotFoundError
            handle, probe = tempfile.mkstemp(prefix=".probe-", dir=str(base))
            os.close(handle)
        except OSError:
            continue
        try:
            os.unlink(probe)
        except OSError:
            # 探测文件删不掉（少见）不该把整个目录判成不可用——否则会静默换目录，
            # 同一作业的两个实例锁在不同路径上，互斥失效
            pass
        if index > 0:
            # 退回目录按用户/环境解析（TMPDIR、macOS /var/folders、systemd PrivateTmp）：
            # 不同身份/环境跑同一作业可能拿到不同目录、互斥静默失效——至少把事实说出来
            log(
                f"  提示：工具目录不可写，运行锁放在 {base}；"
                f"若存在多用户/多环境混跑，请用 SFTP2ODS_LOCK_DIR 固定同一锁目录"
            )
        return base / f"{name}.lock"
    fallback = Path(tempfile.gettempdir()) / f"sftp2ods-{name}.lock"
    log(
        f"  警告：工具目录与系统临时目录都不可写，运行锁临时退回 {fallback}；"
        f"请用 SFTP2ODS_LOCK_DIR 指定一个可写的固定锁目录，否则并发保护可能失效"
    )
    return fallback


def _redact_job(job: dict, text) -> str:
    """作业上下文下的脱敏：先按配置里的密钥值遮，再走形态规则兜底。"""
    return redact_secrets(collect_secret_values(job), str(text))


def _redact_configs(text, *nodes) -> str:
    """多份配置一起做值级脱敏（作业文件 + --config 文件）。

    凭证常只写在 --config 的 secrets/maxcompute 里、作业文件只写 ${secrets.xxx}，
    按单份配置收密钥值会漏遮另一份里回显出来的明文。
    """
    secrets = [value for node in nodes for value in collect_secret_values(node)]
    return redact_secrets(secrets, str(text))


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
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        # NaN/Infinity 先挡掉：json.load 默认接受这些字面量，而 float(raw) != int(raw)
        # 会对 NaN 直接抛 ValueError（裸 traceback）；浮点相等比较也不可靠，改判 is_integer()
        or (isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()))
        or raw <= 0
    ):
        raise SystemExit(f"target.lifecycle_days 必须是正整数（天），实际 {raw!r}")
    return int(raw)


def _local_candidates(download_dir: Path, item, *, allow_legacy: bool = False) -> list[Path]:
    """一个文件的本地候选路径：当前口径 + 旧脚本口径（迁移后复用旧目录里的文件）。"""
    paths = [download_dir / item.ledger_key]
    if allow_legacy and item.ledger_key != item.name:
        paths.append(download_dir / item.name)
    return paths


def _pick_local(download_dir: Path, item, *, allow_legacy: bool = False, md5: str = "") -> Path | None:
    """本地已有的、大小一致的文件路径（没有返回 None）。

    allow_legacy 只给「判定已上传」用：旧脚本把文件平铺下载在 download_dir 下、台账键
    是文件名，只有台账明确记着"这个键属于这一天"时才敢认那个平铺文件。
    **下载与写库路径必须用默认值**：平铺目录里的同名文件可能属于别的日期
    （文件名不含日期时尤其如此），大小恰好相同就会被静默当成本日数据写进库。

    md5 给定时（台账里有）同时校验内容：本地文件被误改/损坏但大小没变时，
    跳过判断会误判"已上传"、分区永远缺这份数据——md5 兜住这类静默损坏。
    """
    for candidate in _local_candidates(download_dir, item, allow_legacy=allow_legacy):
        if state_mod.local_ready(candidate, item.size, md5=md5):
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
            log(f"  最新样本：{date}/{item.name}（{item.size_text}）")
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

    # 每个远端文件拼出的本地落地路径必须落在下载目录内：_safe_remote_name 已挡 "/"、"\\"、".."，
    # 但 Windows 上 "Z:xxx" 这类盘符相对名仍会让 `download_dir / 键` 跳出目录（远端是 POSIX，
    # 文件名带 ":" 合法，不能一律禁掉）。越界直接失败，绝不把远端内容写到下载目录之外。
    try:
        for date_files in files_by_date.values():
            for item in date_files:
                sftp_mod.local_path_within(download_dir, item.ledger_key, item.name)
    except FatalSourceError as exc:
        log(f"❌ {_redact_job(job, exc)}")
        return 1

    missing_cfg = job.get("missing") or {}
    check_missing = as_bool(missing_cfg.get("check"), default=True, field="missing.check")
    tz = load_zone(str(missing_cfg.get("timezone") or DEFAULT_TZ))
    expected = expected_latest(tz, str(missing_cfg.get("grace") or "")) if check_missing else ""
    all_dates = sorted(files_by_date)
    missing, proc_dates, range_start, range_end = plan_dates(all_dates, bizdate, start, end, expected, check_missing)

    # 显式点名单日（--bizdate 或调度环境变量 bizdate）却整天一个文件都没有 → 必须失败。
    # 与下面「缺文件只告警」不同：那些场景（不传业务日=处理远端全部日期、补数区间内缺某天）
    # 缺的是"众多日期中的某几天"，跳过它们照常同步已有文件是 1.2.0 的有意设计；
    # 而这里是"调用方点名要的那一天整天没有"，继续 rc=0 会让调度以为成功、pt=<bizdate>
    # 分区却根本不存在——飞书告警一旦被忽略就是静默缺数，与本家族"宁可失败不可静默丢数"冲突。
    # --force 是"我知道这天可能没数、别拦我"的显式放行开关（与写空分区保护一致）。
    if bizdate and check_missing and bizdate in missing and not args.force:
        log(
            f"❌ 【{job_name}】业务日 {bizdate} 在远端没有任何匹配文件：pt={bizdate} 分区不会产生"
            f"（远端数据范围 {min(all_dates)} ~ {max(all_dates)}）。"
            f"若源方当天确实不产数（如停产出），请用 missing.check=false 跳过核对，或加 --force 明确继续"
        )
        notifier(
            f"{job_name}：业务日 {bizdate} 远端无文件",
            [
                f"**业务日**：{bizdate}",
                f"**远端数据范围**：{min(all_dates)} ~ {max(all_dates)}",
                "该业务日在远端没有任何匹配文件，本次以失败退出（未写任何分区）。",
                "若源方当天确实不产数，请用 `missing.check: false` 或 `--force` 表达继续意图。",
            ],
            footer,
        )
        return 1

    # 下面两条都是"补数区间无产出就失败"，顺序要紧：先判「核对区间为空（下界晚于上界）」，它是
    # 1.0.1 起就有的分支，且与新分支可能同时成立（如 --end-date 早于远端最早：区间无交集且无可处理
    # 日期）——先命中它，报错文案与 1.0.1 保持一致。把它提到「缺文件」告警之前也是行为中性的：
    # range_start > range_end 时 find_missing 恒返回空（missing == []），那条告警本来就不会触发。
    if check_missing and range_start and range_end and range_start > range_end:
        # 核对区间为空 = 用户给的日期范围与远端完全没有交集（区间下界晚于数据上界）。
        # 此时缺文件核对会整个被跳过、也没有任何日期可处理，静默 rc=0 会让"补数日期打错"
        # 看起来像成功；对齐"宁可失败"的红线，这里明确失败。
        log(
            f"❌ 【{job_name}】日期范围与远端没有交集（核对区间为空：{range_start} 晚于 {range_end}）；"
            f"远端数据范围 {min(all_dates)} ~ {max(all_dates)}，请检查 --start-date/--end-date 是否写错"
        )
        return 1
    if check_missing and (start or end) and not proc_dates and not args.force:
        # 用户显式点了补数区间（--start-date/--end-date），而远端这段区间一个可处理文件都没有：
        # 返回 0 会让人以为"补数成功"，比失败更危险（本家族红线：宁可失败不可静默丢数）。
        # 与 1.4.0「显式单日缺文件 → 失败」同口径，--force 是"我知道这段可能没数"的显式放行开关。
        # proc_dates 只看"远端有没有文件"，与台账无关——区间内文件都已上传时它是非空、不会误判。
        log(
            f"❌ 【{job_name}】补数区间在远端没有任何匹配文件"
            f"（--start-date {start or '-'}，--end-date {end or '-'}）；"
            f"远端数据范围 {min(all_dates)} ~ {max(all_dates)}，本区间不会有任何产出。"
            f"若源方确实不产数、确认要空跑，请加 --force 明确继续"
        )
        return 1

    if missing:
        # 缺文件只告警、不失败：源方产数是人/上游系统排期（节假日、结算方停产出是常态），
        # 缺几天不等于任务失败——跳过缺失日期、照常同步已有的文件（缺失日期后面补产
        # 出后，下次运行会自动下载写分区）。真正的异常（远端目录一个文件都没有、
        # 日期范围与远端无交集）仍按失败处理。
        shown = "、".join(missing[:MISSING_SHOW_LIMIT]) + (" 等" if len(missing) > MISSING_SHOW_LIMIT else "")
        log(
            f"⚠️ 【{job_name}】缺少文件：{shown}（共 {len(missing)} 个）；"
            f"已有最新：{max(all_dates)}，预期到：{range_end}；本次跳过缺失日期、继续处理已有文件"
        )
        notifier(
            f"{job_name}：文件缺失（已跳过继续）",
            [
                f"**缺少日期**：{shown}（共 {len(missing)} 个）",
                f"**当前最大**：{max(all_dates)}",
                f"**预期到**：{range_end}",
                "本次已跳过缺失日期、照常同步已有文件，下游任务不受影响；",
                "缺失日期补产出后下次运行会自动同步，无需人工干预。",
            ],
            footer,
        )
    if check_missing and range_start and range_start <= range_end:
        state = "完整" if not missing else "有缺失（上面已告警并跳过）"
        log(
            f"远端共 {len(all_dates)} 个日期（核对区间 {range_start} ~ {range_end} {state}），本次处理 {len(proc_dates)} 个"
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

    # 源表头新增列：忽略其值、照常入库，但发飞书提醒（人工决定是否加列入库/改配置）。
    # 每批新列名最多提醒一次；重复文件名不再重复通知。
    drift_columns: set[str] = set()

    def note_extra_headers(file_name: str, extras: list[str]) -> None:
        new_cols = [name for name in extras if name not in drift_columns]
        if not new_cols:
            return
        drift_columns.update(extras)
        log(f"  ⚠️ {file_name} 表头出现未配置的新增列：{'、'.join(new_cols)}（本次忽略其值、照常入库）")
        notifier(
            f"{job_name}：源文件出现新增列",
            [
                f"**新增列**：{'、'.join(f'`{name}`' for name in new_cols)}",
                f"**文件**：{file_name}",
                "本次已忽略新增列、其余数据照常入库。如需入库，请手动处理：",
                "① ODS 表加列（`ALTER TABLE ... ADD COLUMNS ...`）；",
                '② 作业 `parse.columns` 补列定义（历史文件可能没有该列时加 `"required": false`）；',
                "③ 补跑受影响日期（`--force` 或 `--start-date` / `--end-date`）。",
            ],
            footer,
        )

    for date in proc_dates:
        items = files_by_date[date]

        def done(item, date=date):
            """台账 + 本地文件都齐（大小一致）→ 该文件已上传；远端文件更新过会自动重下重写。

            allow_legacy=True：允许回退到旧脚本的平铺文件——台账已经证明那个键属于这一天，
            跳过是安全的；下载阶段绝不会这么认（见 _pick_local）。
            台账里有 md5 时同时校验内容：本地文件被误改/损坏但大小没变也能发现。
            """
            keys = (item.ledger_key,) if item.ledger_key == item.name else (item.ledger_key, item.name)
            record = state_mod.record_of(ledger, keys, item.size, table_name, date, project=project)
            return bool(record) and _pick_local(
                download_dir, item, allow_legacy=True, md5=str((record or {}).get("md5") or "")
            )

        if not args.force and all(done(item) for item in items):
            skipped += 1
            state_mod.log_skip(date, items)
            continue

        # 下载该日期下所有文件（本地已有且大小一致的不重下）
        local_paths: list[Path] = []
        for item in items:
            # 复用本地文件时要带上台账里的 md5（与 done() 同口径）：内容被改坏/损坏但大小
            # 没变时原来会按"仅比大小"复用它——损坏数据被重新解析上传、台账 md5 还被覆盖，
            # 而且永远不再从远端重下
            keys = (item.ledger_key,) if item.ledger_key == item.name else (item.ledger_key, item.name)
            record = state_mod.record_of(ledger, keys, item.size, table_name, date, project=project)
            existing = _pick_local(download_dir, item, md5=str((record or {}).get("md5") or ""))
            if existing is not None:
                local_paths.append(existing)
                continue
            log(f"下载 {date}/{item.name}（{item.size_text}）...")
            try:
                local_paths.append(source.download(item, download_dir / item.ledger_key))
            except FatalSourceError as exc:
                log(f"❌ {_redact_job(job, exc)}")
                return 1
            except (ConfigError, OSError, RuntimeError) as exc:
                # 只接预期的下载/连接类错误（FatalSourceError 与重试耗尽都是 RuntimeError 子类）；
                # TypeError/AttributeError 这类代码缺陷继续上抛，不能被"下载失败"掩盖成数据问题
                log(f"❌ 下载 {item.name} 失败：{_redact_job(job, exc)}")
                return 1

        # 先完整解析一遍数行数（表头校验、合计行校验都在这趟完成；坏文件在写库前就拦住）。
        # 行数按 local_paths 的顺序存列表：用 path.name 当键有两个坑——台账键与远端文件名
        # 不同口径时下面按 item.name 取值会 KeyError；同一天两个文件重名时会互相覆盖、行数偏小
        # 源文件总大小不超过 REUSE_ROWS_MAX_BYTES 时把行缓存下来给写库复用，避免再扫一遍。
        file_rows: list[int] = []
        prepared_rows: list[list] | None = None
        try:
            reuse = parse_mod.source_bytes(local_paths) <= parse_mod.REUSE_ROWS_MAX_BYTES
            collected: list[list] = []
            for path in local_paths:
                stats = {"skipped": 0}
                if reuse:
                    rows_list = list(parse_spec.iter_rows(path, stats))
                    file_rows.append(len(rows_list))
                    collected.extend(rows_list)
                else:
                    file_rows.append(sum(1 for _ in parse_spec.iter_rows(path, stats)))
                if stats["skipped"]:
                    log(f"  {path.name}：跳过 {stats['skipped']} 行（命中 skip_if_empty 的空键行）")
                extras = stats.get("extra_headers") or []
                if extras:
                    note_extra_headers(path.name, extras)
            if reuse:
                prepared_rows = collected
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"❌ 解析失败：{_redact_job(job, exc)}")
            return 1
        rows = sum(file_rows)
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
                o,
                table,
                project,
                table_name,
                date,
                lambda paths=local_paths, rows=prepared_rows: parse_mod.iter_batches(
                    paths, parse_spec, prepared_rows=rows
                ),
                rows,
                timeout=args.sql_timeout,
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
        # 台账带 md5：下次运行时跳过判断会校验本地文件内容（防误改/静默损坏），
        # 旧台账没有 md5 字段时按只比大小兼容处理（见 state.local_ready）
        for index, (item, path) in enumerate(zip(items, local_paths)):
            ledger[item.ledger_key] = {
                "project": project,
                "table": table_name,
                "pt": date,
                "size": item.size,
                "rows": file_rows[index],
                "md5": state_mod.md5_of(path),
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

    try:
        log_handle = _open_log_file(args.log_file)
    except SystemExit as exc:
        # --log-file 指向目录/打不开属于"参数问题"：按 README 的退出码约定报 2（还没做过
        # 任何远端操作），不能混进 1（运行失败）让调度侧按"数据问题"处理
        log(f"❌ {exc}")
        return 2
    if log_handle is not None:
        from .utils import add_log_sink

        add_log_sink(log_handle)

    if args.init:  # 交互式建配置：不需要 --job
        from .init_wizard import run_init

        def _wizard_ask(prompt: str = "") -> str:
            """向导的提问也走 log：--log-file 里能看到整套问答（只记问题，不记回答——回答里是密钥）。"""
            log(prompt)
            try:
                return input()
            except (EOFError, ValueError, RuntimeError) as exc:
                # stdin 关闭/无输入源：统一翻译成 EOFError（= 取消），与 prompt_secret 同口径
                raise EOFError("标准输入不可用") from exc

        def _wizard_ask_secret(prompt: str = "") -> str:
            """密钥类提问：问题同样走 log（留痕），回答走 getpass 不回显。

            不回显是为了密钥不进终端 scrollback，也不被 `script` / 录屏抄走——原来走
            input() 时密钥明文回显在终端上。无 tty 等读不到隐藏输入的场景退回 input()。
            """
            return prompt_secret(prompt)

        try:
            return run_init(args.init_out, ask=_wizard_ask, echo=log, ask_secret=_wizard_ask_secret)
        except KeyboardInterrupt:
            log("已中断（配置未生成完），退出")
            return 130
        except SystemExit as exc:
            # 与其它分支同口径：向导里的配置错/文件错记一笔再返回，别把裸 traceback 抛给调度
            log(f"❌ {redact(str(exc))}")
            return 1
        except Exception as exc:  # noqa: BLE001 - 向导内部未预期异常
            # 向导不该把裸 traceback 抛给调度；也避免把它误报成"已取消"（用户取消走 EOFError）
            log(f"❌ 向导内部错误：{type(exc).__name__}: {redact(str(exc))}")
            return 1
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
    config: dict = {}  # 供统一异常出口做值级脱敏（读到 --config 后就有内容）
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
        if args.bizdate is not None:
            base_day = parse_day_arg(args.bizdate)
            bizdate = base_day.strftime("%Y%m%d")
        elif args.start_date or args.end_date:
            # 显式补数区间优先于环境变量 bizdate：调度环境里 bizdate 总是存在，不忽略它的话
            # 合法的补数命令会被 run_sync 的"单日 vs 区间"互斥拦下（退出码还会错成 1）
            base_day = datetime.now(job_tz_of(job_raw)).date() - timedelta(days=1)
        else:
            from_env = env_bizdate(strict=not args.check)
            if from_env is not None:
                bizdate = from_env.strftime("%Y%m%d")
                base_day = from_env
            else:
                base_day = datetime.now(job_tz_of(job_raw)).date() - timedelta(days=1)

        # 替换 ${secrets.x}/${bizdate} 等占位符；--config 的 maxcompute/profiles 用返回的副本
        # （render_job 不改写入参，同一进程重复调用时不会串上一个作业已替换的密钥/日期）
        job, config = render_job(job_raw, config, base_day)
        job = normalize_job(job)  # 补齐默认值，让配置尽量短
        validate_job(job)
        for warning in collect_warnings(job):  # 未知字段告警（拼写错误提示）
            # 此时 job 已经过 render_job，明文密钥就在 job 里：与其它所有来自 job 的输出
            # 同口径做值级脱敏，防止告警文本里带上字段值（拼错的键名旁边常跟着取值）
            log(f"⚠️ {_redact_job(job, warning)}")

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
        # 准备阶段的配置错（bizdate 畸形、缺块、占位符写错、运行锁拿不到）统一按运行期错误的格式记一笔。
        # 脱敏按**作业文件 + --config** 两份配置一起收密钥值：凭证只写在 --config 的 secrets/
        # maxcompute 里时，只按 job_raw 收值会把报错里回显的明文漏出去（api2ods 同口径）
        if job_raw or config:
            log(f"❌ {_redact_configs(exc, job_raw, config)}")
        else:
            log(f"❌ {redact(str(exc))}")
        return 1
    except KeyboardInterrupt:
        log("已中断（尚未开始运行），退出")
        return 130
    finally:
        _detach_log_sink(log_handle)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
