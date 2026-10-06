# -*- coding: utf-8 -*-
"""交互式建配置向导（`sftp2ods --init`）。

目的：让不熟悉配置字段的人也能接新数据源——按提示回答问题、连一次 SFTP 看真实文件，
必要时自动拉一个样本文件生成列定义，最后得到一份能直接 `--check` 的作业 JSON。
（密钥直接写进文件，作业文件在 .gitignore 里。）

设计：所有提问都允许回车用默认值；answers 通过 ask 注入，便于单元测试。
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import parse as parse_mod
from .config import DEFAULT_TZ
from .sftp import SftpSource, local_path_within
from .utils import ConfigError

DEFAULT_ENDPOINT = "https://service.us-west-1.maxcompute.aliyun.com/api"
DEFAULT_FILE_REGEX_FLAT = "settlement_report_(?P<date>\\d{8})\\.csv"
DEFAULT_FILE_REGEX_DIR = "data_(?P<date>\\d{8})\\.csv"
DEFAULT_DIR_REGEX = "(?P<date>\\d{8})"
SAMPLE_CHOICES = (
    "1 = 先连 SFTP 拉一个最新文件（推荐，表头/类型自动带出来）",
    "2 = 读取本地 CSV 文件的表头",
    "3 = 暂时没有样本（生成占位列，之后自己在 json 里改）",
)
SAMPLE_IDS = ("1", "2", "3")


def _ask_bool(ask, echo, prompt: str, default: bool = True, retries: int = 3) -> bool:
    """y/n 问答：只在认出肯定/否定时返回，其它回答提示后重问。

    原来用 startswith("y") 判定：答"是/有/1"这类肯定说法会被静默当成"否"
    （合计行开关、缺文件核对被悄悄关掉），既不重问也不提示。
    """
    for _ in range(retries):
        answer = _ask(ask, f"{prompt}？(y/n)", "y" if default else "n").strip().lower()
        if answer in ("y", "yes", "1", "是", "有", "true"):
            return True
        if answer in ("n", "no", "0", "否", "没有", "false"):
            return False
        echo(f"   无法识别 {answer!r}：请输入 y 或 n")
    echo(f"   连续 {retries} 次无法识别，按默认（{'y' if default else 'n'}）处理")
    return default


def _ask(ask, prompt: str, default: str = "") -> str:
    """问一个问题；空输入用默认值。"""
    hint = f"（默认 {default}）" if default else ""
    answer = str(ask(f"{prompt}{hint}：") or "").strip()
    return answer or default


def _ask_secret(ask, prompt: str) -> str:
    """密钥类输入：只去掉尾部换行，不做 strip——首尾空白可能是凭据本身的一部分，
    getpass 不回显，被改写了用户当场察觉不到（与 api2ods 同款）。"""
    return str(ask(prompt) or "").rstrip("\r\n")


def _default_ask_secret(prompt: str = "") -> str:
    """密钥类输入：走 getpass 不回显（终端 scrollback / 录屏 / `script` 录制都拿不到明文）。

    环境不支持隐藏输入（无 tty 等）时退回普通 input——不能因为读不到密钥就让向导不可用；
    与 feishu2ods.host_key 同口径。
    """
    try:
        return getpass.getpass(prompt)
    except Exception:  # noqa: BLE001 - 没有 tty 等场景退回普通输入
        # 退回 input() 时输入会明文回显，而向导提示语里写着"输入不回显"——必须显式纠正预期
        print("（警告：当前环境无法隐藏输入，接下来输入的密钥会明文回显）", file=sys.stderr)
        try:
            return input(prompt)
        except (EOFError, ValueError, RuntimeError) as exc:
            # stdin 关闭/无输入源：统一翻译成 EOFError（= 取消），与 cli 的输入口径一致
            raise EOFError("标准输入不可用") from exc


def _ask_choice(ask, prompt: str, choices: tuple, default: str = "0", echo=print) -> str:
    """让用户从编号选项里选一个，返回选中的编号字符串。"""
    echo(prompt)
    for line in choices:
        echo(f"    {line}")
    return _ask(ask, "请选择编号", default)


def _ask_int(ask, prompt: str, default: int, echo=print, minimum: int | None = None, maximum: int | None = None) -> int:
    """问一个整数；回车用默认值，填了非数字/越界值就提示后重问。"""
    for _ in range(3):
        answer = str(_ask(ask, prompt, str(default))).strip()
        try:
            value = int(answer)
        except ValueError:
            echo(f"   {answer!r} 不是整数，请填数字（如 {default}）")
            continue
        if minimum is not None and value < minimum:
            echo(f"   {value} 必须不小于 {minimum}，请重新填写（如 {default}）")
            continue
        if maximum is not None and value > maximum:
            echo(f"   {value} 不能大于 {maximum}，请重新填写（如 {default}）")
            continue
        return value
    echo(f"   连续三次没填对，先按默认值 {default} 写进配置（之后可以在文件里改）。")
    return default


def _ask_nonempty(ask, echo, prompt: str, attempts: int = 3) -> str:
    """问一个必填项；连续 attempts 次为空返回空串（调用方决定是否取消）。"""
    for _ in range(attempts):
        value = _ask(ask, prompt)
        if value:
            return value
        echo("   这一项不能为空。")
    return ""


def slugify(header: str) -> str:
    """列头 → 合法 MaxCompute 列名（小写、非字母数字转下划线、数字开头加前缀）。"""
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(header or "").lower()).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text


def build_columns(headers: list[str], amount_indexes, int_indexes) -> list[dict]:
    """按"哪些列是金额/整数"生成列定义（其余 string）；列名去重。"""
    used: set[str] = set()
    columns = []
    for index, header in enumerate(headers):
        name = slugify(header) or f"col_{index + 1}"
        # 补后缀后必须再查一次重名：["Amount", "Amount", "Amount_1"] 只按基名计数会
        # 生成两个 amount_1（列重名，validate_parse_config 会直接拒绝这份配置）
        base, suffix = name, 1
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        if index in amount_indexes:
            type_text = "decimal(19,10)"
        elif index in int_indexes:
            type_text = "bigint"
        else:
            type_text = "string"
        columns.append({"header": header, "name": name, "type": type_text})
    return columns


def _match_columns(headers: list[str], raw: str, echo=print, names=None) -> set[int]:
    """把"列号或列名"的输入解析成列下标集合；认不出的项告警后忽略。

    names 给目标列名（如 order_id）：用户按提示填列名时，源列头（Order ID）与
    目标列名（order_id）都能匹配上。
    """
    result: set[int] = set()
    for token in re.split(r"[,，、]", raw or ""):
        token = token.strip()
        if not token:
            continue
        if token.isascii() and token.isdigit():
            # isdigit() 单独不够：上标数字（"²"）也过 isdigit 但 int() 抛 ValueError，
            # 而那个异常不在向导的兜底里，会直接 traceback 退出
            index = int(token) - 1
            if 0 <= index < len(headers):
                result.add(index)
            else:
                echo(f"   列号 {token} 超出范围（1~{len(headers)}），已忽略")
            continue
        lowered = token.lower()
        matches = [i for i, header in enumerate(headers) if str(header or "").strip().lower() == lowered]
        if names:
            matches += [i for i, name in enumerate(names) if str(name).lower() == lowered]
        if matches:
            result.add(matches[0])
        else:
            echo(f"   找不到列 {token!r}，已忽略（可以填列号）")
    return result


def _read_local_sample(ask, echo) -> list[str] | None:
    """读取本地 CSV 样本的表头（3 次机会）。"""
    for _ in range(3):
        path_text = _ask(ask, "   本地 CSV 文件路径")
        if not path_text:
            echo("   路径不能为空。")
            continue
        try:
            # expanduser 也要在 try 内：解析不了 ~user（本地无该用户 / HOME 未设置）时
            # 抛 RuntimeError("Could not determine home directory.")，在外面会冒到向导
            # 顶层被当真实缺陷重抛；读不存在的文件/无权限是 OSError，编码不对是
            # UnicodeDecodeError——都要走"提示后重试"，不能让整个向导直接终止
            # （与远端样本路径同口径）
            path = Path(path_text).expanduser()
            headers = parse_mod.read_header(path)
        except (ConfigError, OSError, UnicodeDecodeError, RuntimeError) as exc:
            echo(f"   读取失败：{exc}")
            continue
        if headers:
            return headers
    return None


def _read_remote_sample(ask, echo, sftp_cfg: dict, source_cfg: dict) -> list[str] | None:
    """连 SFTP 取一个当日文件读表头（同一天多个文件时按名称取第一个）；连接/下载失败给提示并返回 None。"""
    workdir = Path(tempfile.mkdtemp(prefix="sftp2ods-init-"))
    try:
        try:
            source = SftpSource(sftp_cfg, source_cfg)
            files_by_date = source.list_files()
        except (ConfigError, OSError, RuntimeError) as exc:
            # 只接预期的连接类错误（ConfigError 是 SystemExit 子类需单独列；FatalSourceError
            # 与重试耗尽都是 RuntimeError 子类）。TypeError/AttributeError 等代码缺陷继续上抛：
            # 全部降级成"连接失败"会生成一份列定义完全错误的配置却提示成功
            echo(f"   连接/列目录失败：{exc}")
            return None
        if not files_by_date:
            echo("   远端没有任何匹配文件（检查目录/正则），换个样本来源吧。")
            return None
        date = max(files_by_date)
        item = sorted(files_by_date[date], key=lambda it: it.name)[0]
        echo(f"   取样文件：{date}/{item.name}（{item.size_text}；同一天多个文件时按名称取第一个），下载中 ...")
        try:
            # 与主下载路径同一把锁：落地路径必须落在 base（向导的临时 workdir）之内，
            # 免得"包含性校验"在这里成了例外（远端是 POSIX，名字带 ":" 合法，不能靠禁冒号）。
            local = source.download(item, local_path_within(workdir, item.name, item.name))
        except (ConfigError, OSError, RuntimeError) as exc:
            # 与上面的连接块同口径：TypeError/AttributeError 等代码缺陷继续上抛，
            # 不能被"下载失败"掩盖成"换个样本来源"的提示
            echo(f"   下载失败：{exc}")
            return None
        try:
            return parse_mod.read_header(local)
        except (ConfigError, OSError, UnicodeDecodeError) as exc:
            # 与本地样本路径（_read_local_sample）同口径：坏样本给提示后返回 None，
            # 让 _collect_sample_headers 的三次重选机制生效，而不是终止整个向导
            echo(f"   读取表头失败：{exc}")
            return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _collect_sample_headers(ask, echo, sftp_cfg: dict, source_cfg: dict) -> list[str] | None:
    """按用户选择拿到表头列表；拿不到返回 None（由调用方决定占位/取消）。"""
    for _ in range(3):
        choice = _ask_choice(ask, "⑤ 表头列定义从哪里来？", SAMPLE_CHOICES, "1", echo)
        if choice not in SAMPLE_IDS:
            echo(f"   编号 {choice} 不是有效选项，按「连 SFTP 拉样本」继续。")
            choice = "1"
        if choice == "1":
            headers = _read_remote_sample(ask, echo, sftp_cfg, source_cfg)
        elif choice == "2":
            headers = _read_local_sample(ask, echo)
        else:
            return None
        if headers:
            echo(
                f"   读到 {len(headers)} 列：{'、'.join(str(h or '') for h in headers[:6])}{' 等' if len(headers) > 6 else ''}"
            )
            return headers
        echo("   没拿到表头，可以重选来源。")
    return None


def run_init(out_path: str = "", ask=input, echo=print, workdir: Path | None = None, ask_secret=None) -> int:
    """交互式生成作业配置，返回退出码（0 成功 / 1 取消）。

    默认输出到「当前目录/jobs/<作业名>.json」——无论是在源码目录还是 pip 安装后运行都合理。
    ask_secret(prompt) -> str：密钥类输入默认用 getpass（不回显）；单元测试可注入假实现（离线跑）。
    """
    root = Path(workdir) if workdir else Path.cwd()
    ask_secret = ask_secret or _default_ask_secret
    written_path: Path | None = None  # os.replace 成功后的落盘路径（外层中断分支据它区分"文件已写全/未生成"）
    try:
        echo("=== sftp2ods 配置向导（直接回车用默认值；随时 Ctrl+C 取消）===")
        echo("")

        # ---------------------------------------------------------- ① 作业
        job_name = _ask(ask, "① 作业名（英文/数字/下划线，用于文件名和日志）", "my_sftp_job")
        # 作业名直接当文件名用，含路径分隔符/.. 时会写到 jobs 目录之外；顺手统一去掉不安全字符
        safe_name = re.sub(r"[^0-9A-Za-z_\-]", "_", job_name).strip("_") or "my_sftp_job"
        if safe_name != job_name:
            echo(f"   提示：作业名里的特殊字符已替换为下划线，文件名用 {safe_name}")
        job_name = safe_name

        # ---------------------------------------------------------- ② SFTP
        echo("")
        echo("=== SFTP 连接 ===")
        host = _ask_nonempty(ask, echo, "② 主机名（如 sftp.example.com）")
        if not host:
            echo("❌ 主机名连续三次为空，已取消。")
            return 1
        port = _ask_int(ask, "   端口", 22, echo, minimum=1, maximum=65535)
        username = _ask_nonempty(ask, echo, "   登录名")
        if not username:
            echo("❌ 登录名连续三次为空，已取消。")
            return 1
        auth_choice = _ask_choice(ask, "③ SFTP 怎么认证？", ("1 = 密码", "2 = 私钥文件"), "1", echo)
        auth: dict = {}
        if auth_choice == "2":
            key_file = _ask(ask, "   私钥文件路径", "~/.ssh/id_rsa")
            passphrase = _ask_secret(ask_secret, "   私钥口令（没有就留空，输入不回显）")
            auth = {"type": "key", "key_file": key_file}
            if passphrase.strip():
                # 判空按去空白后的值（" " 与"没填"同义）；存储保持原值：口令首尾空白
                # 可能是凭据本体（与密码分支同一口径）
                auth["passphrase"] = passphrase
        else:
            if auth_choice != "1":
                echo(f"   编号 {auth_choice} 不是有效选项，按「密码」继续。")
            password = _ask_secret(ask_secret, "   密码（输入不回显）")
            # 空密码写进配置 = 一份必然连不上的作业、向导却报"已生成成功"：
            # 重问但限次（与 _ask_nonempty 同为 3 次）——不能无上限循环：
            # 输入源持续给空白行（管道/自动应答）时会一直刷屏不退出
            for _ in range(2):
                if password.strip():
                    break
                echo("   密码不能为空（认证类型选了密码）：请重新输入，或 Ctrl+C 取消后改用密钥认证")
                password = _ask_secret(ask_secret, "   密码（输入不回显）")
            if not password.strip():
                echo("❌ 密码连续三次为空，已取消（可改用密钥认证，或稍后在作业文件里补密码）。")
                return 1
            auth = {"type": "password", "password": password}
        sftp_cfg = {"host": host, "port": port, "username": username, "auth": auth}

        # ---------------------------------------------------------- ③ 远端文件
        echo("")
        echo("=== 远端文件 ===")
        remote_root = _ask_nonempty(ask, echo, "④ 远端目录（如 /statements 或 settlements）")
        if not remote_root:
            echo("❌ 远端目录连续三次为空，已取消。")
            return 1
        layout_choice = _ask_choice(
            ask,
            "   文件是怎么放的？",
            ("1 = 平铺：目录下直接是文件（文件名里带日期）", "2 = 按日期子目录：目录/{日期}/文件名"),
            "1",
            echo,
        )
        source_cfg: dict = {"root": remote_root, "layout": "flat"}
        if layout_choice == "2":
            source_cfg["layout"] = "date_dir"
            date_dir_regex = _ask(ask, "   日期子目录名的正则（含 (?P<date>...) 捕获组）", DEFAULT_DIR_REGEX)
            source_cfg["date_dir_regex"] = date_dir_regex
            default_regex = DEFAULT_FILE_REGEX_DIR
        else:
            if layout_choice not in ("1", "2"):
                echo(f"   编号 {layout_choice} 不是有效选项，按「平铺」继续。")
            default_regex = DEFAULT_FILE_REGEX_FLAT
        file_regex = _ask(ask, "   文件名正则（平铺需含 (?P<date>\\d{8}) 捕获组）", default_regex)
        source_cfg["file_regex"] = file_regex

        # ---------------------------------------------------------- ④ 表头
        echo("")
        headers = _collect_sample_headers(ask, echo, sftp_cfg, source_cfg)

        if headers:
            echo("")
            echo("=== 列类型（默认全部 string）===")
            for index, header in enumerate(headers, start=1):
                echo(f"    {index}. {header}")
            amount_raw = _ask(ask, "⑥ 哪些是金额/小数列？（列号或列名，逗号分隔；没有留空）")
            amount_indexes = _match_columns(headers, amount_raw, echo)
            int_raw = _ask(ask, "   哪些是整数（bigint）列？（同上，没有留空）")
            int_indexes = _match_columns(headers, int_raw, echo) - amount_indexes
            columns = build_columns(headers, amount_indexes, int_indexes)
        else:
            echo("")
            echo("   没有样本：先写占位列，生成后请打开 json 把 columns 改成真实列（表头要用源文件里的原文）。")
            placeholder = _ask(ask, "   先给第一列表头起个名（回车=col1）", "col1")
            columns = [{"header": placeholder, "name": slugify(placeholder) or "col1", "type": "string"}]

        # ---------------------------------------------------------- ⑤ 行级规则
        echo("")
        footer_value = _ask_bool(ask, echo, "⑦ 文件末尾有没有合计行（首列为空）", default=False)
        skip_field = ""
        if headers:
            skip_raw = _ask(ask, "   有没有「关键字段为空就跳过」的行？（填列名如 order_id，没有留空）")
            if skip_raw:
                matched = _match_columns(headers, skip_raw, echo, names=[col["name"] for col in columns])
                if matched:
                    skip_field = columns[sorted(matched)[0]]["name"]
        parse_cfg: dict = {"encoding": "utf-8-sig", "delimiter": "auto", "columns": columns}
        if footer_value:
            sum_names = [col["name"] for col in columns if str(col.get("type", "")).startswith("decimal")]
            if sum_names:
                parse_cfg["footer"] = {"sum": sum_names}
                echo(f"   合计行会按 {len(sum_names)} 个金额列核对“合计 = 数据行之和”（对不上直接报错）。")
            else:
                # 写 {"sum": []} 与"没配 footer"等价（运行时按空列表跳过核对）：留着会让用户
                # 以为配了合计核对，不如明说没启用
                echo("   提示：当前没有金额（decimal）列，合计行核对未启用。")
        if skip_field:
            parse_cfg["skip_if_empty"] = [skip_field]

        # ---------------------------------------------------------- ⑥ 缺文件核对
        echo("")
        check_missing = _ask_bool(ask, echo, "⑧ 要做缺文件核对吗（每天必须有一个文件）", default=True)
        missing_cfg: dict = {"check": check_missing}
        if check_missing:
            timezone_name = _ask(ask, "   按哪个时区的昨天核对最新文件？", DEFAULT_TZ)
            grace = _ask(ask, "   每天几点前跑会误报（HH:MM，留空=不设）", "")
            missing_cfg["timezone"] = timezone_name
            if grace:
                missing_cfg["grace"] = grace

        # ---------------------------------------------------------- ⑦ 告警与目标表
        echo("")
        echo("=== 告警与 MaxCompute 目标 ===")
        # webhook 是凭证（拿到就能往群里发消息），按密钥类处理、不回显；但它是 URL，
        # 首尾空白只可能是粘贴误带入——strip 后再判空/写入（不是密码那种"空白可能是本体"）
        webhook = _ask_secret(ask_secret, "⑨ 飞书告警 webhook（可留空，输入不回显）").strip()
        project = _ask(ask, "⑩ MaxCompute 项目名", "my_project")
        # 作业名允许连字符（用于文件名），但 MaxCompute 表名不允许：默认表名先把连字符换成下划线
        table = _ask(ask, "   目标表名（建议 <层级>_<业务域>_<过程>_di）", f"ods_{job_name.replace('-', '_')}_di")
        ak = _ask(ask, "   阿里云 AccessKeyId")  # _ask 已 strip
        # sk 走 _ask_secret（不 strip）：AK/SK 是固定格式的凭据（生成的 LTAI…/base64），
        # 首尾空白只可能是粘贴误带入——去掉后再判空/写入，否则 " " 会绕过下面的
        # "未填全"告警、生成一份必然 SignatureDoesNotMatch 的作业却报"已生成成功"
        sk = _ask_secret(ask_secret, "   阿里云 AccessKeySecret（输入不回显）").strip()
        if not ak or not sk:
            # 允许留空（凭证也可以只放 --config），但必须说清后果：不然向导照报
            # "已生成成功"，用户到 --check 才发现是一份跑不起来的配置
            echo("   ⚠️ AccessKeyId/AccessKeySecret 未填全：作业文件里没有可用凭证，")
            echo("      运行前需在 --config 的 maxcompute 块提供（或重新运行向导补齐）。")
        endpoint = _ask(ask, "   endpoint", DEFAULT_ENDPOINT)

        # ---------------------------------------------------------- ⑧ 组装并写出
        description = _ask(ask, "⑪ 一句话描述（可留空）", f"{remote_root} 下的按天文件 → {table}")
        job = {
            "job": job_name,
            "description": description,
            "maxcompute": {"project": project, "endpoint": endpoint, "access_key_id": ak, "access_key_secret": sk},
            "sftp": sftp_cfg,
            "source": source_cfg,
            "parse": parse_cfg,
            "target": {"project": project, "table": table, "comment": f"SFTP 报表（{file_regex}）列展开，pt=文件日期"},
            "missing": missing_cfg,
        }
        if webhook:
            job["notify"] = {"webhook": webhook}

        if out_path:
            target_path = Path(out_path)
            if not target_path.is_absolute():
                target_path = root / target_path
        else:
            # 默认路径只用 root 拼一次：root 本身可能是相对路径（workdir 传相对值时），
            # 不能再走「相对就拼 root」的通用分支——那会拼成 root/root/jobs/... 的嵌套
            target_path = root / "jobs" / f"{job_name}.json"
        if target_path.is_dir():
            raise SystemExit(
                f"--init-out 指向的是目录，需要给文件名：{target_path}（例如 {target_path / (job_name + '.json')}）"
            )
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            # 文件含 SFTP 密码/AK-SK/webhook 明文：写同目录临时文件（mkstemp，默认 0600）
            # 再 rename 顶替。原来 O_TRUNC 直接覆盖目标：打开瞬间旧配置就没了，写入
            # 失败/被 kill 时磁盘上只剩 0 字节或半截 JSON，重跑还得靠人救。
            # 已存在的旧文件先把权限收紧（符号链接跳过：chmod 会跟随链接改到真实文件上）
            if os.name != "nt" and target_path.exists() and not target_path.is_symlink():
                try:
                    os.chmod(target_path, 0o600)
                except OSError:
                    pass
            fd, tmp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=str(target_path.parent))
            tmp_path = Path(tmp_name)
            try:
                try:
                    handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
                except Exception:
                    try:
                        os.close(fd)  # fdopen 失败时 fd 还没被接管：显式关闭，否则泄漏到进程退出
                    except OSError:
                        pass
                    raise
                with handle:
                    handle.write(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, target_path)
                written_path = target_path
            except BaseException:
                # 含 BaseException（Ctrl+C/SystemExit）：临时文件里是含密钥的完整配置，
                # 中断也必须清掉，否则会在 jobs/ 里残留且向导声称"未生成任何文件"
                if not tmp_path.exists():
                    # os.replace 已把 tmp 移走、只是中断恰好落在 written_path 赋值之前：
                    # 配置已完整落盘，记下状态（外层按"文件已写全"报告），没有 tmp 要清
                    written_path = target_path
                else:
                    try:
                        tmp_path.unlink()
                    except FileNotFoundError:
                        pass  # 竞态：文件已经不在了，没有残留
                    except OSError as exc:
                        # 清理失败（Windows 上文件被占用/目录权限变化）不能静默：临时文件里是
                        # 含明文密钥的完整配置，向导下面还会打印"未生成任何文件"——不提示
                        # 残留路径，没人会去删这个文件
                        echo(f"   ⚠️ 清理临时文件失败（{exc}）：{tmp_path} 仍含明文密钥，请手工删除")
                raise
            if os.name != "nt":
                # 兜底收紧（mkstemp 本就是 0600）。失败只警告：os.replace 已经完成，
                # 让 chmod 的 OSError 落到下面的 except 会报"写文件失败（原文件未改动）"——
                # 与磁盘状态完全相反（新配置已落盘）
                try:
                    os.chmod(target_path, 0o600)
                except OSError as exc:
                    echo(f"   ⚠️ 权限收紧失败（{exc}）：文件已生成，请手工 chmod 600 {target_path}")
        except OSError as exc:
            # 只有"写文件"这一段失败才叫写文件失败（读取类 OSError 在各自的交互里已处理）；
            # 旧配置此时原样未动
            echo("")
            echo(f"写文件失败（原文件未改动）：{exc}")
            return 1

        # 后续命令里的 --job 必须给真实可用的路径：CLI 按当前目录解析 --job（没有 jobs/
        # 兜底），自定义 --init-out 时只给文件名，用户照抄会报"找不到作业文件"。
        # 相对当前目录的写法最直观（默认场景算出来就是 jobs/<名>.json）
        try:
            job_arg = os.path.relpath(target_path)
        except ValueError:  # Windows 跨盘符时无法计算相对路径
            job_arg = str(target_path.resolve())
        try:
            echo("")
            echo(f"✅ 已生成：{target_path}")
            echo("下一步：")
            echo("  1) 打开文件核对（尤其 sftp.auth、source.file_regex、parse.columns 的列头与类型）")
            echo(f"  2) 体检：  sftp2ods --job {job_arg} --check")
            echo(f"  3) 试跑：  sftp2ods --job {job_arg} --bizdate 20260920 --dry-run")
            echo(f"  4) 正式：  sftp2ods --job {job_arg} --bizdate ${{bizdate}}")
        except KeyboardInterrupt:
            # 文件已在 os.replace 时写全：中断只打断收尾提示，不能返回 130 让调用方
            # 以为失败、更不能再报"未生成任何文件"（含密钥的文件其实已在磁盘上）
            try:
                echo("")
                echo(f"✅ 已生成：{target_path}（Ctrl+C 落在收尾阶段，文件已写全）")
            except OSError:
                pass  # stdout 同时关了就静默：文件已落盘，结论不因提示写不出去而改变
        except OSError:
            # 收尾提示写 stdout 失败（管道对端退出/终端关闭，BrokenPipeError 是 OSError
            # 子类）：配置已在 os.replace 时完整落盘，这段 IO 失败不影响结果——不能落进
            # 外层"文件操作失败"分支（会报出与磁盘状态相反的结论），也不能逃出 run_init
            pass
        return 0
    except ConfigError as exc:
        echo("")
        echo(f"配置不合法：{exc}")
        return 1
    except OSError as exc:
        # 兜底（临时目录创建失败等写文件之外的 OSError）：给一句人话而不是裸 traceback。
        # 文件已在 os.replace 时落盘的话不能再报"文件操作失败"（与磁盘状态相反）
        try:
            echo("")
            if written_path is not None:
                echo(f"✅ 已生成：{written_path}（后续提示阶段出错：{exc}）")
            else:
                echo(f"文件操作失败：{exc}")
        except OSError:
            pass  # stdout 也关了：不影响磁盘状态与结论
        return 0 if written_path is not None else 1
    except EOFError:
        # stdin 关闭/无输入源（`sftp2ods --init <&-`、CI 里没接管道）：cli 的输入包装层
        # 把 ValueError/RuntimeError 一并翻译成 EOFError 才走到这里。这里**只**认 EOFError——
        # 以前连 ValueError 一起吞，向导内部真正的 ValueError 会被误报成"已取消"
        echo("")
        echo("已取消，未生成任何文件。")
        return 1
    except KeyboardInterrupt:
        # Ctrl+C 按 README 的退出码约定报 130，与同步流程保持一致。
        # os.replace 已完成时文件已落盘（含明文密钥），不能再报"未生成任何文件"——
        # 那会让操作者以为磁盘上没有这份配置，与内层收尾提示的"文件已写全"自相矛盾
        try:
            echo("")
            if written_path is not None:
                echo(f"✅ 已生成：{written_path}（Ctrl+C 落在收尾阶段，文件已写全）")
            else:
                echo("已取消，未生成任何文件。")
        except OSError:
            pass  # stdout 同时关了就静默：不影响磁盘状态与退出码
        return 130
    except RuntimeError as exc:  # "lost sys.stdin"（没有标准输入）
        if "stdin" not in str(exc):
            raise
        echo("")
        echo(f"无法读取交互输入（{exc}）；--init 需要在终端里交互运行。")
        return 1
