# -*- coding: utf-8 -*-
"""配置：读取、占位符替换、校验（含未知键告警）、目标表/下载目录解析。

作业文件（jobs/*.json）的块结构：
    job / description / secrets / maxcompute / profiles /
    sftp / source / parse / target / missing / notify
逐字段说明见 README「配置参考」与 jobs/_template_full.example.json。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import parse as parse_mod
from .dates import DEFAULT_TZ, load_zone, parse_grace
from .utils import ConfigError, _is_sensitive_key, as_bool, redact, require_identifier

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
SFTP_KEYS = {
    "host",
    "port",
    "username",
    "auth",
    "connect_timeout",
    "io_timeout",
    "retry_times",
    "retry_delay",
    "host_key",
}
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
    missing = job.get("missing")
    if missing is not None and not isinstance(missing, dict):
        # render_job 会在 check_block_types 之前调到这里：missing 写成字符串时
        # 直接 .get 是 "'str' object has no attribute 'get'" 这种无上下文的裸异常
        raise ConfigError(f"作业配置的 missing 必须是对象（键值对），实际 {type(missing).__name__}：{_show(missing)}")
    return load_zone(str((missing or {}).get("timezone") or DEFAULT_TZ))


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


def _inline_scalar(value, name: str) -> str:
    """内联占位符（"a${secrets.x}b"）的解析结果转字符串。

    None/容器不能 str() 成 "None"/"['a']" 混进配置——secrets.x 为 null（模板注入失败）
    时静默转成 "None"，校验全过、运行期去连一个叫 "None" 的目录/主机，报错指不到配置上。
    """
    if value is None or isinstance(value, (list, dict, tuple, set)):
        raise ConfigError(
            f"占位符 ${{{name}}} 解析结果不是标量（{type(value).__name__}），"
            f"无法内联进字符串——检查对应的 secrets/占位符配置"
        )
    return str(value)


def deep_substitute(value, context: dict, *, literal_ok: bool = False):
    """递归替换配置里的占位符（字符串里可混写，如 'Bearer ${secrets.x}'）。

    找不到的占位符直接报错（不静默留空，避免密钥没配却悄悄连不上）。

    literal_ok=True 用于凭据字段（键名含 password/passphrase 等）的值：密码是自由文本，
    里面出现 "${" 只是密码的一个字符，能解析的占位符照常解析、畸形或未知的原样保留——
    不能因为密码长什么样就让整份作业加载失败。
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
            if isinstance(key, str) and not isinstance(new_key, str):
                # 整串占位符解析成非字符串（如 ${secrets.lst} 是列表）：str() 会把键静默
                # 变成 "['a', 'b']" 这种没人认得的字符串
                raise ConfigError(
                    f"配置的键 {redact(key)!r} 解析结果不是字符串（{type(new_key).__name__}），无法作为 JSON 键"
                )
            if str(new_key) in result:
                # 替换后键名撞车（"a" 与 "${secrets.b}" 都解析成同一个键）：静默覆盖会让
                # 前一个配置项凭空消失，必须报错
                raise ConfigError(f"占位符替换后键名冲突：{redact(str(new_key))!r}（源键 {redact(str(key))!r}）")
            result[str(new_key)] = deep_substitute(
                item, context, literal_ok=isinstance(key, str) and _is_sensitive_key(key)
            )
        return result
    if isinstance(value, list):
        return [deep_substitute(item, context, literal_ok=literal_ok) for item in value]
    if not isinstance(value, str):
        return value

    match = _PLACEHOLDER_RE.fullmatch(value)
    if match:
        try:
            resolved = resolve_placeholder(match.group(1), context)
        except SystemExit:
            # 凭据字段整串恰好长成 ${...}（密码本身就是这个字面量、或模板没改干净）：
            # 与下面的内联容错同口径，按字面量保留——不能同一段文本多一个字符就换个行为
            if literal_ok:
                return value
            raise
        if resolved is None:
            # 整串解析成 null（secrets.x 没配好）会原样进入运行时配置：与内联路径的
            # _inline_scalar 同口径拒绝；容器/数字是合法的整串结果，放行
            raise ConfigError(f"占位符 ${{{match.group(1)}}} 解析成了 null，请检查对应的 secrets 配置")
        return resolved

    if literal_ok:

        def _replace_tolerant(m: re.Match) -> str:
            try:
                resolved = resolve_placeholder(m.group(1), context)
            except SystemExit:
                return m.group(0)  # 未知键：按字面量保留
            return _inline_scalar(resolved, m.group(1))

        # 未闭合的 "${" 正则匹配不到，sub 会原样保留；成对但未知的占位符也按字面量留下
        return _PLACEHOLDER_RE.sub(_replace_tolerant, value)

    if value.count("${") != len(_PLACEHOLDER_RE.findall(value)):
        # 回显前过 redact：这段文本会进日志，而配置值本身可能就是密钥
        # （密码里带 "${" 这类字符时，原样回显等于把密码写进日志）
        raise SystemExit(
            f"配置里有未闭合或写法不对的占位符：{redact(value[:120])!r}（应形如 ${{secrets.键名}}，${{ 与 }} 必须成对）"
        )

    def _replace(m: re.Match) -> str:
        return _inline_scalar(resolve_placeholder(m.group(1), context), m.group(1))

    return _PLACEHOLDER_RE.sub(_replace, value)


def render_job(job_raw: dict, config: dict, bizdate) -> tuple[dict, dict]:
    """作业配置 → 替换占位符（secrets/日期）后的运行时配置；返回 (作业, 渲染后的 --config)。

    作业文件里的 secrets 原样保留（密钥本身不参与占位符替换），只用于 ${secrets.x}。
    --config 文件的 maxcompute/profiles 也参与替换（共享凭证常写成 ${secrets.ak}）：
    替换结果以**新副本**返回，不就地改写入参——同一进程里用同一个 config 跑多个作业时，
    就地改写会把上一个作业已替换的密钥/日期串到下一个作业。
    """
    context = build_context(config, bizdate, job_raw.get("secrets"), tz=job_tz_of(job_raw))
    job = {key: value for key, value in job_raw.items() if key != "secrets"}
    rendered = deep_substitute(job, context)
    rendered["secrets"] = _as_secrets(job_raw.get("secrets"), "作业配置的 secrets")
    rendered_config = dict(config)
    for block in ("maxcompute", "profiles"):
        if isinstance(config.get(block), dict):
            rendered_config[block] = deep_substitute(config[block], context)
    return rendered, rendered_config


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
    sftp = dict(_as_mapping(job.get("sftp")) or {})
    if sftp:
        # JSON null / 空串不能靠 setdefault：键已存在时不会补默认值，None 会一路传到连接层
        _fill_default(sftp, "port", 22)
        _fill_default(sftp, "connect_timeout", 30)
        _fill_default(sftp, "io_timeout", 600)
        _fill_default(sftp, "retry_times", 3)
        _fill_default(sftp, "retry_delay", 10)
        raw_auth = sftp.get("auth")
        # 类型守卫：dict("password") 会抛 "dictionary update sequence element #0 has length 1"
        # 这种无上下文的裸 ValueError（normalize_job 先于 validate_job 执行，先拦在这里）
        if raw_auth is not None and not isinstance(raw_auth, dict):
            raise ConfigError(
                f"作业配置的 sftp.auth 必须是对象（键值对），实际 {type(raw_auth).__name__}：{_show(raw_auth)}"
            )
        auth = dict(raw_auth or {})
        if auth:
            _fill_default(auth, "type", "password")
            sftp["auth"] = auth
        job["sftp"] = sftp
    source = dict(_as_mapping(job.get("source")) or {})
    if source:
        # 与 sftp 块同口径：JSON null / 空串也当未配置补默认值（"layout": null 不会静默留在 None）
        _fill_default(source, "layout", "flat")
        # 布局取值统一小写后再写回：validate_job 按 .lower() 校验，而 SftpSource 是按字面量
        # 比较的——只写 "DATE_DIR" 这类大小写变体时，校验能过、运行时却按 flat 列目录
        # （结果为空、报"远端目录下没有任何匹配文件"，与真正的配置错指不到一起）。
        source["layout"] = str(source.get("layout") or "flat").strip().lower()
        job["source"] = source
    parse_cfg = dict(_as_mapping(job.get("parse")) or {})
    if parse_cfg:
        # 与 sftp 块同口径：JSON null / 空串也当未配置补默认值
        # （setdefault 对已存在的 null 不生效，"strict_columns": null 会一路传成 None）
        _fill_default(parse_cfg, "encoding", "utf-8-sig")
        _fill_default(parse_cfg, "delimiter", "auto")
        _fill_default(parse_cfg, "on_missing_header", "error")
        _fill_default(parse_cfg, "strict_columns", True)
        _fill_default(parse_cfg, "empty_as", "null")
        job["parse"] = parse_cfg
    target = dict(_as_mapping(job.get("target")) or {})
    if target:
        _fill_default(target, "allow_empty", True)
        job["target"] = target
    missing = dict(_as_mapping(job.get("missing")) or {})
    _fill_default(missing, "check", True)
    _fill_default(missing, "timezone", DEFAULT_TZ)
    job["missing"] = missing
    notify = dict(_as_mapping(job.get("notify")) or {})
    if notify:
        _fill_default(notify, "enabled", True)
        job["notify"] = notify
    return job


# =============================================================================
# 校验
# =============================================================================


def _fill_default(block: dict, key: str, default) -> None:
    """缺省、JSON null、空串都当成未配置，写入默认值。"""
    if block.get(key) is None or block.get(key) == "":
        block[key] = default


def _as_mapping(value) -> dict:
    """未知键扫描只接受对象；字符串/数组退化成空，避免 .get 崩或按字符告警。"""
    return value if isinstance(value, dict) else {}


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
    # 校验阶段挂上来的告警：只读不 pop——同一份 job dict 反复收集要拿到同样的告警
    # （原来第一次就把键拿走、第二次返回空）。只有 list 形态才算内部通道；
    # 用户误写的同名键（字符串等）不参与 extend（原来 extend("oops") 会按字符拆成
    # 4 条假告警），并照常落入下面"不是已知配置项"的扫描
    pending = job.get(_WARNINGS_KEY)
    allowed_job_keys = JOB_KEYS | {_WARNINGS_KEY} if isinstance(pending, list) else JOB_KEYS
    if isinstance(pending, list):
        warnings.extend(str(item) for item in pending)
    _check_unknown_keys(job, allowed_job_keys, "作业", warnings)
    sftp = _as_mapping(job.get("sftp"))
    _check_unknown_keys(sftp, SFTP_KEYS, "sftp", warnings)
    _check_unknown_keys(_as_mapping(sftp.get("auth")), SFTP_AUTH_KEYS, "sftp.auth", warnings)
    _check_unknown_keys(_as_mapping(job.get("source")), SOURCE_KEYS, "source", warnings)
    _check_unknown_keys(_as_mapping(job.get("target")), TARGET_KEYS, "target", warnings)
    _check_unknown_keys(_as_mapping(job.get("missing")), MISSING_KEYS, "missing", warnings)
    _check_unknown_keys(_as_mapping(job.get("notify")), NOTIFY_KEYS, "notify", warnings)
    parse_cfg = _as_mapping(job.get("parse"))
    _check_unknown_keys(parse_cfg, parse_mod.PARSE_KEYS, "parse", warnings)
    columns = parse_cfg.get("columns")
    # 告警收集早于 validate_job：columns 写成非数组（5、true 这类手误）时不能裸 TypeError
    # 崩掉，交给校验阶段报"columns 必须是数组"（与 build_job_summary 的守卫同口径）
    for index, col in enumerate(columns if isinstance(columns, (list, tuple)) else []):
        if isinstance(col, dict):
            _check_unknown_keys(col, parse_mod.COLUMN_KEYS, f"parse.columns[{index}]", warnings)
    footer = parse_cfg.get("footer")
    if isinstance(footer, dict):
        _check_unknown_keys(footer, {"sum"}, "parse.footer", warnings)
    return warnings


def _require_number(
    value, where: str, *, minimum=None, exclusive_min=None, maximum=None, integer: bool = False
) -> None:
    """可选数值配置项的范围校验：写错在配置阶段就报，别拖到连接时才抛裸异常。"""
    if value is None or value == "":
        return
    if isinstance(value, bool):
        raise ConfigError(f"{where} 必须是{'整数' if integer else '数字'}，实际 {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 必须是{'整数' if integer else '数字'}，实际 {value!r}") from exc
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
    host = sftp.get("host")
    if not isinstance(host, str):
        raise ConfigError(f"sftp.host 必须是字符串（SFTP 主机），实际 {type(host).__name__}")
    if not host.strip():
        raise ConfigError("作业配置缺少 sftp.host（SFTP 主机）")
    username = sftp.get("username")
    if not isinstance(username, str):
        raise ConfigError(f"sftp.username 必须是字符串（SFTP 登录名），实际 {type(username).__name__}")
    if not username.strip():
        raise ConfigError("作业配置缺少 sftp.username（SFTP 登录名）")
    _require_number(sftp.get("port"), "sftp.port", minimum=1, maximum=65535, integer=True)
    _require_number(sftp.get("connect_timeout"), "sftp.connect_timeout", minimum=0)
    _require_number(sftp.get("io_timeout"), "sftp.io_timeout", minimum=0)
    _require_number(sftp.get("retry_times"), "sftp.retry_times", minimum=0, integer=True)
    _require_number(sftp.get("retry_delay"), "sftp.retry_delay", minimum=0)
    auth = sftp.get("auth")
    if auth is None or not isinstance(auth, dict):
        raise SystemExit(
            '作业配置缺少 sftp.auth（形如 {"type": "password", "password": "xxx"} 或 {"type": "key", "key_file": "~/.ssh/xxx"}）'
        )
    auth_type = str(auth.get("type") or "password").lower()
    if auth_type not in ALLOWED_SFTP_AUTH_TYPES:
        raise ConfigError(f"sftp.auth.type 不支持：{auth_type}（可用 {'/'.join(ALLOWED_SFTP_AUTH_TYPES)}）")
    if auth_type == "password":
        password = auth.get("password")
        if password is None or password == "":
            raise ConfigError("sftp.auth.type=password 必须给 sftp.auth.password")
        if not isinstance(password, str):
            # 只报类型名、不报取值：password 是密钥，明文不能进错误消息/日志
            raise ConfigError(f"sftp.auth.password 必须是字符串，实际 {type(password).__name__}")
    if auth_type == "key":
        key_file = auth.get("key_file")
        if not isinstance(key_file, str):
            raise ConfigError(f"sftp.auth.key_file 必须是字符串（私钥路径），实际 {type(key_file).__name__}")
        if not key_file.strip():
            raise ConfigError("sftp.auth.type=key 必须给 sftp.auth.key_file（私钥路径，如 ~/.ssh/clink_sftp）")
    passphrase = auth.get("passphrase")
    if passphrase is not None and not isinstance(passphrase, str):
        raise ConfigError(f"sftp.auth.passphrase 必须是字符串，实际 {type(passphrase).__name__}")
    host_key = str(sftp.get("host_key") or "").strip().lower()
    if host_key not in ("", "auto_accept"):
        raise ConfigError(
            f"sftp.host_key 不支持：{host_key!r}（不写 = 严格校验 ~/.ssh/known_hosts 里的主机指纹，"
            f'防中间人；"auto_accept" = 显式降级为不校验，与 StrictHostKeyChecking=no 同口径）'
        )

    # ---- source ----
    source = job.get("source") or {}
    if not source:
        raise ConfigError("作业配置缺少 source 块（root / layout / file_regex）")
    root = source.get("root")
    if not isinstance(root, str):
        # str() 兜底会把 ["/a", "/b"] 这种容器拼成非空字符串放行，运行期才在 SFTP 层
        # 崩成 TypeError/列错目录；与 download_dir 同口径在配置阶段显式拒掉
        raise ConfigError(
            f"source.root 必须是字符串目录路径，实际 {type(root).__name__}（如 /statements 或 settlements）"
        )
    if not root.strip():
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
            # NaN/Infinity 先挡掉：json.load 默认接受这些字面量，而 float(raw) != int(raw)
            # 会对 NaN 直接抛 ValueError；浮点相等比较也不可靠，改判 is_integer()
            or (
                isinstance(raw_lifecycle, float)
                and (not math.isfinite(raw_lifecycle) or not raw_lifecycle.is_integer())
            )
            or raw_lifecycle <= 0
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
    # 显式 JSON null 按"未配置"处理（与 check_block_types / _as_secrets / _profile_source
    # 同一口径）：把不用的块写成 null 是常态，运行期本来就容忍，校验不该反过来拦下
    if "secrets" in job and job["secrets"] is not None and not isinstance(job["secrets"], dict):
        raise ConfigError("作业配置的 secrets 必须是对象（键值对）")
    for block in ("maxcompute", "profiles"):
        if block in job and job[block] is not None and not isinstance(job[block], dict):
            raise ConfigError(f"作业配置的 {block} 必须是对象")
    for key, value in (job.get("profiles") or {}).items():
        if str(key).startswith(("//", "#")):
            # 注释键约定（同 _check_unknown_keys / deep_substitute）：不能按"必须是对象"报错，
            # 否则用户在 profiles 里写一行注释就让整个作业跑不起来
            continue
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
    # target 写成字符串/数组（漏了大括号）时不能裸 AttributeError：与摘要/告警收集同口径
    name = str(getattr(args, "mc_profile", "") or _as_mapping(job.get("target")).get("profile") or "default").strip()
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
    target = _as_mapping(job.get("target"))
    profile = get_mc_profile_meta(config, job, args)
    project = str(target.get("project") or profile.get("project") or "")
    if not project:
        raise ConfigError(
            "没有目标项目：请在作业文件的 maxcompute.project（或 profiles.<名>.project）或 target.project 里指定"
        )
    table = str(target.get("table") or "")
    return project, table


def safe_job_name(job: dict, job_path: Path) -> str:
    """作业名（用于下载目录名）：过滤成安全字符；没配 job 时用文件名。

    过滤后为空（全中文/全符号的作业名，本项目里很常见）时用名字哈希做后缀，
    不能一律退化成 "job"：多个这样的作业会共用同一个下载目录，互相扫到对方的
    文件（旧文件被当成本次数据、重名文件被去重跳过），错误数据写进目标表
    还看不出来。
    """
    raw = str(job.get("job") or job_path.stem or "job")
    cleaned = re.sub(r"[^0-9A-Za-z_\-]", "_", raw)
    if cleaned and cleaned == raw and raw == raw.lower():
        # 只含合法字符且不含大写字母：原样保留（再 strip 会把 "recon_" 改成 "recon-<hash>"，
        # 注释承诺的"已有下载目录不受影响"就不成立）。判定不能用 raw.islower()：它对
        # 没有大小写字符的名字（"20240101"、"___"）返回 False，会把这类名字也错误地
        # 补哈希——下载目录改名后旧文件不复用，每次重下、目录持续堆积。
        # 含大写字母的名字必须补哈希：
        # Windows/macOS 默认文件系统大小写不敏感，"Recon" 与 "recon" 会落到同一个
        # 下载目录、互相扫到对方的文件——名字层面的安全过滤不能漏掉这一维度
        return raw
    # 过滤后与原名不一致（含全中文/全符号名）时补名字哈希，两个原因：
    # ① 全中文名过滤后为空，退化成常量会共用下载目录；
    # ② 部分 ASCII 的名字会"折叠"——"对账A" 与 "A" 都归一到 "A"，两个作业互扫对方文件。
    # 名字本身就是 [0-9A-Za-z_-] 时保持原名，已有下载目录不受影响。
    digest = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{cleaned or 'job'}-{digest}"


def resolve_download_dir(job: dict, job_path: Path) -> Path:
    """下载目录：source.download_dir（相对路径按作业文件所在目录）或默认 <作业目录>/download/<作业名>。"""
    # source 写成字符串（漏大括号）时要给中文配置错而不是裸 AttributeError：
    # check_block_types 是标准的类型守卫；_as_mapping 会静默退化成空、把问题拖到后面
    check_block_types(job)
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
    # 概要打印早于 check_block_types/validate_job：块写成字符串时 "str".get 是裸 AttributeError，
    # 统一走 _as_mapping 退化成空（与未知键扫描同口径）
    sftp = _as_mapping(job.get("sftp"))
    source = _as_mapping(job.get("source"))
    parse_cfg = _as_mapping(job.get("parse"))
    target = _as_mapping(job.get("target"))
    missing = _as_mapping(job.get("missing"))
    columns = parse_cfg.get("columns") or []
    if not isinstance(columns, (list, tuple)):
        # "columns": 5 这类非可迭代值会让下面的 for 抛裸 TypeError（概要打印早于校验）
        columns = []
    # 列元素可能是非对象（配置校验会拦，但概要打印是更早的路径）：加类型守卫，别崩在 AttributeError
    amount_count = sum(
        1 for col in columns if isinstance(col, dict) and str(col.get("type") or "").lower().startswith("decimal")
    )
    layout_cn = "日期子目录" if str(source.get("layout") or "flat").strip().lower() == "date_dir" else "平铺"
    window = "无（默认全部日期）"
    if as_bool(missing.get("check"), default=True, field="missing.check"):
        window = f"{missing.get('timezone') or DEFAULT_TZ} 昨天"
        if missing.get("grace"):
            window += f"（{missing['grace']} 前再退一天）"
    return [
        f"  作业      : {job.get('job') or '(未命名)'}"
        + (f" —— {job['description']}" if job.get("description") else ""),
        f"  SFTP      : {sftp.get('username')}@{sftp.get('host')}:{sftp.get('port') or 22}"
        f"（认证 {(_as_mapping(sftp.get('auth')).get('type') or 'password')}）",
        f"  远端      : {source.get('root')}（{layout_cn}；{source.get('file_regex')}）",
        f"  解析      : {len(columns)} 列（其中金额/小数列 {amount_count} 个）"
        f"，encoding={parse_cfg.get('encoding') or 'utf-8-sig'}，delimiter={parse_cfg.get('delimiter') or 'auto'}",
        f"  目标      : pt=文件日期（每个日期一个分区），"
        f"allow_empty={as_bool(target.get('allow_empty'), default=True, field='target.allow_empty')}",
        f"  缺文件核对: {'开' if as_bool(missing.get('check'), default=True, field='missing.check') else '关'}（预期最新 = {window}）",
    ]
