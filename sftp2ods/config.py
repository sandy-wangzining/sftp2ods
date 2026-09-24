# -*- coding: utf-8 -*-
"""配置：读取、占位符替换、校验（含未知键告警）、目标表/下载目录解析。

作业文件（jobs/*.json）的块结构：
    job / description / secrets / maxcompute / profiles /
    sftp / source / parse / target / missing / notify
逐字段说明见 README「配置参考」与 jobs/_template_full.example.json。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import parse as parse_mod
from .dates import DEFAULT_TZ, load_zone, parse_grace
from .utils import ConfigError, as_bool, redact, require_identifier

ALLOWED_SFTP_AUTH_TYPES = ("password", "key")
ALLOWED_LAYOUTS = ("flat", "date_dir")

JOB_KEYS = {
    "job",
    "description",
    "secrets",
    "maxcompute",
    "profiles",
    "sftp",
    "source",
    "parse",
    "target",
    "missing",
    "notify",
}
SFTP_KEYS = {"host", "port", "username", "auth", "connect_timeout", "io_timeout", "retry_times", "retry_delay"}
SFTP_AUTH_KEYS = {"type", "password", "key_file", "passphrase"}
SOURCE_KEYS = {"root", "layout", "file_regex", "date_dir_regex", "download_dir"}
TARGET_KEYS = {"project", "table", "comment", "stored_as", "lifecycle_days", "allow_empty", "profile"}
MISSING_KEYS = {"check", "timezone", "grace"}
NOTIFY_KEYS = {"webhook", "enabled"}

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
# 校验阶段产生的告警挂在 job 上，由 collect_warnings 取走并清空（模块级缓存会串到下一次运行）
_WARNINGS_KEY = "__warnings__"


# =============================================================================
# 读取与占位符
# =============================================================================


def load_json_file(path: Path, desc: str) -> dict:
    """读一个 JSON 文件，失败时给出带路径的明确报错。

    用 utf-8-sig 读：Windows 上很容易存成「UTF-8 with BOM」，按 utf-8 读会在首字符处
    报 JSONDecodeError，而错误信息完全看不出是 BOM 导致。
    """
    if not path.is_file():
        raise SystemExit(f"找不到{desc}：{path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{desc}不是 UTF-8 编码（{path}）：{exc}；请用 UTF-8（可带 BOM）保存后重试")
    except OSError as exc:
        raise SystemExit(f"读取{desc}失败（{path}）：{exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{desc}不是合法 JSON（{path}）：{exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{desc}顶层必须是 JSON 对象：{path}")
    return data


def _show(value) -> str:
    """报错回显配置内容前先脱敏（整块配置里可能带密钥，异常文本会进日志）。"""
    return redact(repr(value))


def _as_secrets(value, where: str) -> dict:
    """secrets 必须是键值对；写成列表/字符串/数字时给出人话报错。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是对象（键值对），实际 {type(value).__name__}：{_show(value)}")
    return dict(value)


def job_tz_of(job: dict) -> ZoneInfo:
    """作业时区：missing.timezone（默认 Asia/Shanghai），只影响 ${today} 等占位符与缺文件核对。"""
    return load_zone(str((job.get("missing") or {}).get("timezone") or DEFAULT_TZ))


def build_context(config: dict, bizdate, job_secrets: dict | None = None, tz: ZoneInfo | None = None) -> dict:
    """运行时上下文：secrets（作业内优先）+ 日期。"""
    secrets = _as_secrets(config.get("secrets"), "--config 文件里的 secrets")
    secrets.update(_as_secrets(job_secrets, "作业配置的 secrets"))
    today = datetime.now(tz or ZoneInfo(DEFAULT_TZ)).date()
    return {
        "secrets": secrets,
        "bizdate": bizdate.strftime("%Y%m%d"),
        "bizdate_iso": bizdate.isoformat(),
        "today": today.strftime("%Y%m%d"),
        "today_iso": today.isoformat(),
    }


def resolve_placeholder(name: str, context: dict):
    """把 ${a.b} 解析成 context 里的值；找不到直接报错（不静默留空）。"""
    node = context
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            if name.startswith("secrets."):
                hint = f"请在作业文件的 secrets（或 --config 文件）里补上「{name.split('.', 1)[1]}」"
            else:
                hint = "可选：secrets.<键名> / bizdate / bizdate_iso / today / today_iso"
            raise SystemExit(f"配置里引用了不存在的占位符：${{{name}}}；{hint}")
        node = node[part]
    return node


def deep_substitute(value, context: dict):
    """递归替换配置里的占位符（字符串里可混写，如 'Bearer ${secrets.x}'）。

    找不到的占位符直接报错（不静默留空，避免密钥没配却悄悄连不上）。
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            # 以 // / # 开头的键是"注释"约定（JSON 没有注释）：原样保留、不参与占位符替换，
            # 这样模板/作业里可以放心写 "${secrets.xxx} 长这样" 这类说明
            if isinstance(key, str) and key.startswith(("//", "#")):
                result[key] = item
                continue
            new_key = deep_substitute(key, context) if isinstance(key, str) else key
            result[str(new_key)] = deep_substitute(item, context)
        return result
    if isinstance(value, list):
        return [deep_substitute(item, context) for item in value]
    if not isinstance(value, str):
        return value

    match = _PLACEHOLDER_RE.fullmatch(value)
    if match:
        return resolve_placeholder(match.group(1), context)

    if value.count("${") != len(_PLACEHOLDER_RE.findall(value)):
        raise SystemExit(
            f"配置里有未闭合或写法不对的占位符：{value[:120]!r}（应形如 ${{secrets.键名}}，${{ 与 }} 必须成对）"
        )

    def _replace(m: re.Match) -> str:
        return str(resolve_placeholder(m.group(1), context))

    return _PLACEHOLDER_RE.sub(_replace, value)


def render_job(job_raw: dict, config: dict, bizdate) -> dict:
    """作业配置 → 替换占位符（secrets/日期）后的运行时配置。

    作业文件里的 secrets 原样保留（密钥本身不参与占位符替换），只用于 ${secrets.x}。
    """
    context = build_context(config, bizdate, job_raw.get("secrets"), tz=job_tz_of(job_raw))
    job = {key: value for key, value in job_raw.items() if key != "secrets"}
    rendered = deep_substitute(job, context)
    rendered["secrets"] = _as_secrets(job_raw.get("secrets"), "作业配置的 secrets")
    # --config 文件自己的 maxcompute/profiles 也参与替换（共享凭证常写成 ${secrets.ak}）
    for block in ("maxcompute", "profiles"):
        if isinstance(config.get(block), dict):
            config[block] = deep_substitute(config[block], context)
    return rendered


def check_block_types(job: dict) -> None:
    """作业文件里几个大块必须是对象，否则给中文报错（必须在任何取值之前调用）。"""
    for block in ("sftp", "source", "parse", "target", "missing", "notify"):
        value = job.get(block)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(f"作业配置的 {block} 必须是对象（键值对），实际 {type(value).__name__}：{_show(value)}")


def normalize_job(job: dict) -> dict:
    """把作业配置补齐成"带默认值"的完整形态（让用户配置尽量短）。"""
    job = dict(job)
    check_block_types(job)
    sftp = dict(job.get("sftp") or {})
    if sftp:
        sftp.setdefault("port", 22)
        sftp.setdefault("connect_timeout", 30)
        sftp.setdefault("io_timeout", 600)
        sftp.setdefault("retry_times", 3)
        sftp.setdefault("retry_delay", 10)
        auth = dict(sftp.get("auth") or {})
        if auth:
            auth.setdefault("type", "password")
            sftp["auth"] = auth
        job["sftp"] = sftp
    source = dict(job.get("source") or {})
    if source:
        source.setdefault("layout", "flat")
        job["source"] = source
    parse_cfg = dict(job.get("parse") or {})
    if parse_cfg:
        parse_cfg.setdefault("encoding", "utf-8-sig")
        parse_cfg.setdefault("delimiter", "auto")
        parse_cfg.setdefault("on_missing_header", "error")
        parse_cfg.setdefault("strict_columns", True)
        parse_cfg.setdefault("empty_as", "null")
        job["parse"] = parse_cfg
    target = dict(job.get("target") or {})
    if target:
        target.setdefault("allow_empty", True)
        job["target"] = target
    missing = dict(job.get("missing") or {})
    missing.setdefault("check", True)
    missing.setdefault("timezone", DEFAULT_TZ)
    job["missing"] = missing
    notify = dict(job.get("notify") or {})
    if notify:
        notify.setdefault("enabled", True)
        job["notify"] = notify
    return job


# =============================================================================
# 校验
# =============================================================================


def _check_unknown_keys(obj: dict, allowed: set, where: str, warnings: list) -> None:
    """逐个键比对白名单，命中就追加一条告警（JSON 没有注释，以 // 或 # 开头的键当注释放过）。"""
    for key in obj:
        if key.startswith("//") or key.startswith("#"):
            continue
        if key not in allowed:
            warnings.append(f"{where}.{key} 不是已知配置项（拼写错误？）——已忽略")


def collect_warnings(job: dict) -> list[str]:
    """收集未知配置项告警（拼写错误提醒），不阻断运行。"""
    warnings: list[str] = []
    warnings.extend(job.pop(_WARNINGS_KEY, None) or [])
    _check_unknown_keys(job, JOB_KEYS, "作业", warnings)
    _check_unknown_keys(job.get("sftp") or {}, SFTP_KEYS, "sftp", warnings)
    _check_unknown_keys(job.get("sftp", {}).get("auth") or {}, SFTP_AUTH_KEYS, "sftp.auth", warnings)
    _check_unknown_keys(job.get("source") or {}, SOURCE_KEYS, "source", warnings)
    _check_unknown_keys(job.get("target") or {}, TARGET_KEYS, "target", warnings)
    _check_unknown_keys(job.get("missing") or {}, MISSING_KEYS, "missing", warnings)
    _check_unknown_keys(job.get("notify") or {}, NOTIFY_KEYS, "notify", warnings)
    parse_cfg = job.get("parse") or {}
    _check_unknown_keys(parse_cfg, parse_mod.PARSE_KEYS, "parse", warnings)
    for index, col in enumerate(parse_cfg.get("columns") or []):
        if isinstance(col, dict):
            _check_unknown_keys(col, parse_mod.COLUMN_KEYS, f"parse.columns[{index}]", warnings)
    footer = parse_cfg.get("footer")
    if isinstance(footer, dict):
        _check_unknown_keys(footer, {"sum"}, "parse.footer", warnings)
    return warnings


def _require_number(value, where: str, *, minimum=None, exclusive_min=None, maximum=None, integer: bool = False) -> None:
    """可选数值配置项的范围校验：写错在配置阶段就报，别拖到连接时才抛裸异常。"""
    if value is None or value == "":
        return
    if isinstance(value, bool):
        raise ConfigError(f"{where} 必须是{'整数' if integer else '数字'}，实际 {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} 必须是{'整数' if integer else '数字'}，实际 {value!r}")
    if not math.isfinite(number):
        raise ConfigError(f"{where} 必须是有限数字（不能是 NaN/Infinity），实际 {value!r}")
    if integer and number != int(number):
        raise ConfigError(f"{where} 必须是整数，实际 {value!r}")
    if minimum is not None and number < minimum:
        raise ConfigError(f"{where} 不能小于 {minimum:g}，实际 {value!r}")
    if exclusive_min is not None and number <= exclusive_min:
        raise ConfigError(f"{where} 必须大于 {exclusive_min:g}，实际 {value!r}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{where} 不能大于 {maximum:g}，实际 {value!r}")


def _compile_regex(value, where: str, need_date: bool, hint: str = "从文件名提取业务日期") -> None:
    """校验正则：能编译；需要日期捕获组时必须带命名组 date。"""
    text = str(value or "")
    if not text:
        raise ConfigError(f"作业配置缺少 {where}")
    try:
        pattern = re.compile(text)
    except re.error as exc:
        raise ConfigError(f"{where} 不是合法正则：{exc}")
    if need_date and "date" not in pattern.groupindex:
        raise ConfigError(f"{where} 必须带日期命名捕获组 (?P<date>...)：{text!r}（{hint}）")


def validate_job(job: dict) -> None:
    """校验作业配置的必填项与枚举值（错误信息带字段路径，方便直接改）。"""
    if not isinstance(job, dict):
        raise ConfigError(f"作业配置必须是对象（键值对），实际 {type(job).__name__}：{_show(job)}")
    check_block_types(job)

    # ---- sftp ----
    sftp = job.get("sftp") or {}
    if not sftp:
        raise ConfigError("作业配置缺少 sftp 块（host / username / auth）")
    if not sftp.get("host"):
        raise ConfigError("作业配置缺少 sftp.host（SFTP 主机）")
    if not sftp.get("username"):
        raise ConfigError("作业配置缺少 sftp.username（SFTP 登录名）")
    _require_number(sftp.get("port"), "sftp.port", minimum=1, maximum=65535, integer=True)
    _require_number(sftp.get("connect_timeout"), "sftp.connect_timeout", exclusive_min=0)
    _require_number(sftp.get("io_timeout"), "sftp.io_timeout", exclusive_min=0)
    _require_number(sftp.get("retry_times"), "sftp.retry_times", minimum=0, integer=True)
    _require_number(sftp.get("retry_delay"), "sftp.retry_delay", minimum=0)
    auth = sftp.get("auth")
    if auth is None or not isinstance(auth, dict):
        raise SystemExit('作业配置缺少 sftp.auth（形如 {"type": "password", "password": "xxx"} 或 {"type": "key", "key_file": "~/.ssh/xxx"}）')
    auth_type = str(auth.get("type") or "password").lower()
    if auth_type not in ALLOWED_SFTP_AUTH_TYPES:
        raise ConfigError(f"sftp.auth.type 不支持：{auth_type}（可用 {'/'.join(ALLOWED_SFTP_AUTH_TYPES)}）")
    if auth_type == "password" and not auth.get("password"):
        raise ConfigError('sftp.auth.type=password 必须给 sftp.auth.password')
    if auth_type == "key" and not auth.get("key_file"):
        raise ConfigError('sftp.auth.type=key 必须给 sftp.auth.key_file（私钥路径，如 ~/.ssh/clink_sftp）')

    # ---- source ----
    source = job.get("source") or {}
    if not source:
        raise ConfigError("作业配置缺少 source 块（root / layout / file_regex）")
    if not str(source.get("root") or "").strip():
        raise ConfigError("作业配置缺少 source.root（远端目录，如 /statements 或 settlements）")
    layout = str(source.get("layout") or "flat").lower()
    if layout not in ALLOWED_LAYOUTS:
        raise ConfigError(f"source.layout 不支持：{layout}（可用 {'/'.join(ALLOWED_LAYOUTS)}）")
    _compile_regex(source.get("file_regex"), "source.file_regex", need_date=(layout == "flat"))
    if layout == "date_dir":
        _compile_regex(
            source.get("date_dir_regex"),
            "source.date_dir_regex",
            need_date=True,
            hint="日期子目录名里要能提取业务日期，如 (?P<date>\\d{8})",
        )
    if source.get("download_dir") is not None and not isinstance(source.get("download_dir"), str):
        raise ConfigError(f"source.download_dir 必须是字符串路径，实际 {type(source.get('download_dir')).__name__}")

    # ---- parse ----
    parse_cfg = job.get("parse")
    if parse_cfg is None or not isinstance(parse_cfg, dict):
        raise SystemExit("作业配置缺少 parse 块（columns 列定义）")
    parse_mod.validate_parse_config(parse_cfg)

    # ---- target ----
    target = job.get("target") or {}
    if not target.get("table"):
        raise ConfigError("作业配置缺少 target.table（MaxCompute 目标表名）")
    if target.get("project"):
        require_identifier(target["project"], "target.project")
    require_identifier(target["table"], "target.table")
    if target.get("stored_as"):
        require_identifier(target["stored_as"], "target.stored_as")
    as_bool(target.get("allow_empty"), default=True, field="target.allow_empty")
    raw_lifecycle = target.get("lifecycle_days")
    if raw_lifecycle is not None and raw_lifecycle != "":
        if (
            isinstance(raw_lifecycle, bool)
            or not isinstance(raw_lifecycle, (int, float))
            or float(raw_lifecycle) != int(raw_lifecycle)
            or int(raw_lifecycle) <= 0
        ):
            raise ConfigError(f"target.lifecycle_days 必须是正整数（天），实际 {raw_lifecycle!r}")

    # ---- missing ----
    missing = job.get("missing") or {}
    if missing:
        as_bool(missing.get("check"), default=True, field="missing.check")
        load_zone(str(missing.get("timezone") or DEFAULT_TZ))  # 时区名不合法直接报错
        parse_grace(str(missing.get("grace") or ""))  # 格式不合法直接报错

    # ---- notify ----
    notify = job.get("notify") or {}
    if notify:
        as_bool(notify.get("enabled"), default=True, field="notify.enabled")
        if notify.get("webhook") is not None and not isinstance(notify.get("webhook"), str):
            raise ConfigError("notify.webhook 必须是字符串（飞书群机器人地址）")

    # ---- secrets / maxcompute ----
    if "secrets" in job and not isinstance(job["secrets"], dict):
        raise ConfigError("作业配置的 secrets 必须是对象（键值对）")
    for block in ("maxcompute", "profiles"):
        if block in job and not isinstance(job[block], dict):
            raise ConfigError(f"作业配置的 {block} 必须是对象")
    for key, value in (job.get("profiles") or {}).items():
        if not isinstance(value, dict):
            raise ConfigError(
                f"作业配置的 profiles.{key} 必须是对象（键值对），实际 {type(value).__name__}：{_show(value)}"
            )


# =============================================================================
# 目标表 / 下载目录
# =============================================================================


def _profile_source(source: dict, where: str) -> dict:
    """取一份配置里的 profiles 映射；写成非对象时给出人话报错。"""
    profiles = source.get("profiles")
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise ConfigError(
            f'{where} 的 profiles 必须是对象（形如 {{"default": {{...}}}}），'
            f"实际 {type(profiles).__name__}：{_show(profiles)}"
        )
    return profiles


def get_mc_profile_meta(config: dict, job: dict, args) -> dict:
    """取作业使用的 MaxCompute profile 元信息（project/endpoint/ak/sk）。

    查找顺序：作业文件的 profiles.<名> / maxcompute → --config 文件的 profiles.<名> / maxcompute。
    """
    name = str(getattr(args, "mc_profile", "") or (job.get("target") or {}).get("profile") or "default").strip()
    available: list[str] = []
    for source, where in ((job, "作业配置"), (config, "--config 文件")):
        profiles = _profile_source(source, where)
        available += [f"{key}" for key in profiles if key not in available]
        if name in profiles:
            entry = profiles[name]
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"{where} 的 profiles.{name} 必须是对象（键值对），实际 {type(entry).__name__}：{_show(entry)}"
                )
            return dict(entry)
        if name == "default" and source.get("maxcompute"):
            block = source["maxcompute"]
            if not isinstance(block, dict):
                raise ConfigError(
                    f"{where} 的 maxcompute 必须是对象（键值对），实际 {type(block).__name__}：{_show(block)}"
                )
            return dict(block)
    if name == "default":
        return {}
    raise ConfigError(
        f"找不到 MaxCompute profile「{redact(name)}」；已配置：{redact(str(available)) if available else '（无）'}"
    )


def resolve_target(job: dict, config: dict, args) -> tuple[str, str]:
    """解析目标表信息 → (project, table)。"""
    target = job.get("target") or {}
    profile = get_mc_profile_meta(config, job, args)
    project = str(target.get("project") or profile.get("project") or "")
    if not project:
        raise ConfigError(
            "没有目标项目：请在作业文件的 maxcompute.project（或 profiles.<名>.project）或 target.project 里指定"
        )
    table = str(target.get("table") or "")
    return project, table


def safe_job_name(job: dict, job_path: Path) -> str:
    """作业名（用于下载目录名）：过滤成安全字符；没配 job 时用文件名。"""
    raw = str(job.get("job") or job_path.stem or "job")
    return re.sub(r"[^0-9A-Za-z_\-]", "_", raw).strip("_") or "job"


def resolve_download_dir(job: dict, job_path: Path) -> Path:
    """下载目录：source.download_dir（相对路径按作业文件所在目录）或默认 <作业目录>/download/<作业名>。"""
    source = job.get("source") or {}
    raw = str(source.get("download_dir") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else Path(job_path).parent / path
    return Path(job_path).parent / "download" / safe_job_name(job, Path(job_path))


def build_context_doc() -> str:
    """给 --help / --check 用的占位符说明。"""
    return "${secrets.xxx} / ${bizdate} / ${bizdate_iso} / ${today} / ${today_iso}"


def build_job_summary(job: dict) -> list[str]:
    """体检/启动时打印的作业概要（调用方负责整体脱敏）。"""
    sftp = job.get("sftp") or {}
    source = job.get("source") or {}
    parse_cfg = job.get("parse") or {}
    target = job.get("target") or {}
    missing = job.get("missing") or {}
    columns = parse_cfg.get("columns") or []
    amount_count = sum(1 for col in columns if str(col.get("type") or "").lower().startswith("decimal"))
    layout_cn = "日期子目录" if str(source.get("layout") or "flat") == "date_dir" else "平铺"
    window = "无（默认全部日期）"
    if missing.get("check", True):
        window = f"{missing.get('timezone') or DEFAULT_TZ} 昨天"
        if missing.get("grace"):
            window += f"（{missing['grace']} 前再退一天）"
    return [
        f"  作业      : {job.get('job') or '(未命名)'}" + (f" —— {job['description']}" if job.get("description") else ""),
        f"  SFTP      : {sftp.get('username')}@{sftp.get('host')}:{sftp.get('port') or 22}"
        f"（认证 {((sftp.get('auth') or {}).get('type') or 'password')}）",
        f"  远端      : {source.get('root')}（{layout_cn}；{source.get('file_regex')}）",
        f"  解析      : {len(columns)} 列（其中金额/小数列 {amount_count} 个）"
        f"，encoding={parse_cfg.get('encoding') or 'utf-8-sig'}，delimiter={parse_cfg.get('delimiter') or 'auto'}",
        f"  目标      : pt=文件日期（每个日期一个分区），allow_empty={target.get('allow_empty', True)}",
        f"  缺文件检查: {'开' if missing.get('check', True) else '关'}（预期最新 = {window}）",
    ]
