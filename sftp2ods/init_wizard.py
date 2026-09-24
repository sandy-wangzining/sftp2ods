# -*- coding: utf-8 -*-
"""交互式建配置向导（`sftp2ods --init`）。

目的：让不熟悉配置字段的人也能接新数据源——按提示回答问题、连一次 SFTP 看真实文件，
必要时自动拉一个样本文件生成列定义，最后得到一份能直接 `--check` 的作业 JSON。
（密钥直接写进文件，作业文件在 .gitignore 里。）

设计：所有提问都允许回车用默认值；answers 通过 ask 注入，便于单元测试。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import parse as parse_mod
from .config import DEFAULT_TZ
from .sftp import SftpSource
from .utils import ConfigError

DEFAULT_ENDPOINT = "http://service.us-west-1.maxcompute.aliyun.com/api"
DEFAULT_FILE_REGEX_FLAT = "settlement_report_(?P<date>\\d{8})\\.csv"
DEFAULT_FILE_REGEX_DIR = "data_(?P<date>\\d{8})\\.csv"
DEFAULT_DIR_REGEX = "(?P<date>\\d{8})"
SAMPLE_CHOICES = (
    "1 = 先连 SFTP 拉一个最新文件（推荐，表头/类型自动带出来）",
    "2 = 读取本地 CSV 文件的表头",
    "3 = 暂时没有样本（生成占位列，之后自己在 json 里改）",
)
SAMPLE_IDS = ("1", "2", "3")


def _ask(ask, prompt: str, default: str = "") -> str:
    """问一个问题；空输入用默认值。"""
    hint = f"（默认 {default}）" if default else ""
    answer = str(ask(f"{prompt}{hint}：") or "").strip()
    return answer or default


def _ask_choice(ask, prompt: str, choices: tuple, default: str = "0", echo=print) -> str:
    """让用户从编号选项里选一个，返回选中的编号字符串。"""
    echo(prompt)
    for line in choices:
        echo(f"    {line}")
    return _ask(ask, "请选择编号", default)


def _ask_int(ask, prompt: str, default: int, echo=print, minimum: int | None = None) -> int:
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
    seen: dict[str, int] = {}
    columns = []
    for index, header in enumerate(headers):
        name = slugify(header) or f"col_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
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
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(headers):
                result.add(index)
            else:
                echo(f"   列号 {token} 超出范围（1~{len(headers)}），已忽略")
            continue
        lowered = token.lower()
        matches = [i for i, header in enumerate(headers) if header.strip().lower() == lowered]
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
        path = Path(path_text).expanduser()
        try:
            headers = parse_mod.read_header(path)
        except ConfigError as exc:
            echo(f"   读取失败：{exc}")
            continue
        if headers:
            return headers
    return None


def _read_remote_sample(ask, echo, sftp_cfg: dict, source_cfg: dict) -> list[str] | None:
    """连 SFTP 拉一个最新文件、读表头；连接/下载失败给提示并返回 None。"""
    workdir = Path(tempfile.mkdtemp(prefix="sftp2ods-init-"))
    try:
        try:
            source = SftpSource(sftp_cfg, source_cfg)
            files_by_date = source.list_files()
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            echo(f"   连接/列目录失败：{exc}")
            return None
        if not files_by_date:
            echo("   远端没有任何匹配文件（检查目录/正则），换个样本来源吧。")
            return None
        date = max(files_by_date)
        item = sorted(files_by_date[date], key=lambda it: it.name)[0]
        echo(f"   最新文件：{date}/{item.name}（{item.size:,} 字节），下载中 ...")
        try:
            local = source.download(item, workdir / item.name)
        except Exception as exc:  # noqa: BLE001
            echo(f"   下载失败：{exc}")
            return None
        return parse_mod.read_header(local)
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
            echo(f"   读到 {len(headers)} 列：{'、'.join(headers[:6])}{' 等' if len(headers) > 6 else ''}")
            return headers
        echo("   没拿到表头，可以重选来源。")
    return None


def run_init(out_path: str = "", ask=input, echo=print, workdir: Path | None = None) -> int:
    """交互式生成作业配置，返回退出码（0 成功 / 1 取消）。

    默认输出到「当前目录/jobs/<作业名>.json」——无论是在源码目录还是 pip 安装后运行都合理。
    """
    root = Path(workdir) if workdir else Path.cwd()
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
        port = _ask_int(ask, "   端口", 22, echo, minimum=1)
        username = _ask_nonempty(ask, echo, "   登录名")
        if not username:
            echo("❌ 登录名连续三次为空，已取消。")
            return 1
        auth_choice = _ask_choice(ask, "③ SFTP 怎么认证？", ("1 = 密码", "2 = 私钥文件"), "1", echo)
        auth: dict = {}
        if auth_choice == "2":
            key_file = _ask(ask, "   私钥文件路径", "~/.ssh/id_rsa")
            passphrase = _ask(ask, "   私钥口令（没有就留空）")
            auth = {"type": "key", "key_file": key_file}
            if passphrase:
                auth["passphrase"] = passphrase
        else:
            if auth_choice != "1":
                echo(f"   编号 {auth_choice} 不是有效选项，按「密码」继续。")
            password = _ask(ask, "   密码")
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
        footer_value = _ask(ask, "⑦ 文件末尾有没有合计行（首列为空）？(y/n)", "n").lower().startswith("y")
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
            parse_cfg["footer"] = {"sum": sum_names}
            if sum_names:
                echo(f"   合计行会按 {len(sum_names)} 个金额列核对“合计 = 数据行之和”（对不上直接报错）。")
        if skip_field:
            parse_cfg["skip_if_empty"] = [skip_field]

        # ---------------------------------------------------------- ⑥ 缺文件检查
        echo("")
        check_missing = _ask(ask, "⑧ 要做缺文件检查吗（每天必须有一个文件）？(y/n)", "y").lower().startswith("y")
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
        webhook = _ask(ask, "⑨ 飞书告警 webhook（可留空）")
        project = _ask(ask, "⑩ MaxCompute 项目名", "my_project")
        table = _ask(ask, "   目标表名（建议 <层级>_<业务域>_<过程>_di）", f"ods_{job_name}_di")
        ak = _ask(ask, "   阿里云 AccessKeyId")
        sk = _ask(ask, "   阿里云 AccessKeySecret")
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

        target_path = Path(out_path) if out_path else root / "jobs" / f"{job_name}.json"
        if not target_path.is_absolute():
            target_path = root / target_path
        if target_path.is_dir():
            raise SystemExit(
                f"--init-out 指向的是目录，需要给文件名：{target_path}（例如 {target_path / (job_name + '.json')}）"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(target_path, 0o600)  # 含密钥，收紧权限（Windows 忽略）

        echo("")
        echo(f"✅ 已生成：{target_path}")
        echo("下一步：")
        echo("  1) 打开文件核对（尤其 sftp.auth、source.file_regex、parse.columns 的列头与类型）")
        echo(f"  2) 体检：  sftp2ods --job {target_path.name} --check")
        echo(f"  3) 试跑：  sftp2ods --job {target_path.name} --bizdate 20260920 --dry-run")
        echo(f"  4) 正式：  sftp2ods --job {target_path.name} --bizdate ${{bizdate}}")
        return 0
    except ConfigError as exc:
        echo("")
        echo(f"配置不合法：{exc}")
        return 1
    except (KeyboardInterrupt, EOFError, ValueError):
        # stdin 被关闭（`sftp2ods --init <&-`、CI 里没接管道）时 input() 抛的是 ValueError / RuntimeError
        echo("")
        echo("已取消，未生成任何文件。")
        return 1
    except RuntimeError as exc:  # "lost sys.stdin"（没有标准输入）
        if "stdin" not in str(exc):
            raise
        echo("")
        echo(f"无法读取交互输入（{exc}）；--init 需要在终端里交互运行。")
        return 1
