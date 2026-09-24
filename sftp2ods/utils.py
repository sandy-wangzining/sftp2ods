# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

try:
    import fcntl  # Linux / macOS：进程级运行锁
except ImportError:  # pragma: no cover - Windows 没有 fcntl
    fcntl = None

try:
    import msvcrt  # Windows：用首字节锁实现同样的效果
except ImportError:  # pragma: no cover - Linux / macOS 没有 msvcrt
    msvcrt = None


class FatalSourceError(RuntimeError):
    """确定性的源端错误（认证失败、密钥错）：重试没有意义，立刻失败让调度告警。

    注意不要用 OSError 当基类：paramiko 的网络类异常（socket 超时等）也是它的子类，
    拿来当"不重试"的篮子会把本该重试的抖动误杀。
    """


class ConfigError(SystemExit):
    """作业配置错误：重试多少次结果都一样，快速失败并让调度看到原因。

    继承 SystemExit 与项目里其它配置类报错（config.py / mc.py / cli.py）保持一致的退出行为，
    同时给重试循环一个可识别的类型——见 FatalSourceError 的说明。
    """


_lock = threading.Lock()
PROGRESS_EVERY = 10000  # 进度日志节流：每 N 行打一次
_sinks: list = []
_console_patched = False


def setup_console() -> None:
    """stdout/stderr 切 UTF-8，避免 Windows 控制台中文乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 某些重定向流不支持 reconfigure（如 CI 捕获）
            pass


def progress_log(label: str, count: int, unit: str = "行") -> None:
    """每 PROGRESS_EVERY 行打一条进度，避免大作业刷爆日志。"""
    if count and count % PROGRESS_EVERY == 0:
        log(f"    {label}：已处理 {count:,} {unit}")


def add_log_sink(handle) -> None:
    """把日志再写一份到文件（--log-file），句柄由调用方负责关闭。"""
    with _lock:
        _sinks.append(handle)


def remove_log_sink(handle) -> None:
    """摘掉日志文件并关闭句柄（同一进程里多次调用 main 时，残留句柄会继续写已关闭的文件）。"""
    if handle is None:
        return
    with _lock:
        if handle in _sinks:
            _sinks.remove(handle)
    try:
        handle.close()
    except ValueError:
        # 句柄已经被关过（同一进程里 main 多次调用时 _detach 会跑两遍）
        pass
    except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
        pass


_logged_once: set = set()


def reset_log_once() -> None:
    """清空"已打过的告警"记录（每次运行开始时调，见 cli.main）。"""
    with _lock:
        _logged_once.clear()


def log_once(message: str) -> None:
    """同一次运行里内容相同的告警只打一次，之后静默。"""
    with _lock:
        if message in _logged_once:
            return
        _logged_once.add(message)
    log(message)


def log(message: str) -> None:
    """线程安全的控制台输出；时间戳=运行机器本地时间，只标记执行时刻。

    防御：Windows CI/老控制台默认是 cp1252 之类编码，中文/符号会抛 UnicodeEncodeError；
    这里第一次调用时自动把控制台切到 UTF-8，切不了就用"可替换字符"降级输出，保证不中断业务。
    """
    global _console_patched
    if not _console_patched:
        setup_console()
        _console_patched = True

    import datetime as _dt

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with _lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = line.encode(encoding, "replace").decode(encoding, "replace")
            print(safe, flush=True)
        for handle in _sinks:
            try:
                handle.write(line + "\n")
                handle.flush()
            except Exception:  # noqa: BLE001 - 日志文件问题不影响主流程
                pass


class RunLock:
    """进程级运行锁：避免定时任务与手动执行（或两个实例）同时跑。

    - Linux / macOS：flock 排它锁；
    - Windows：msvcrt 首字节锁（同样是排它、非阻塞）；
    - 两种锁都没有的平台：退化为"不阻塞"，不挡运行；
    - 锁随进程退出自动释放，进程被 kill 也由内核释放，不会残留死锁。
    """

    def __init__(self, path: Path):
        """path 由调用方按作业算好（同名作业在不同目录不会互相顶掉，见 cli._lock_path）。"""
        self.path = path
        self.fh = None

    def __enter__(self):
        """拿锁；已被别人持有就抛 SystemExit（不等待）。"""
        if fcntl is None and msvcrt is None:
            return self
        try:
            # "a+" 而不是 "w"：w 会在打开时把文件截断，持锁进程刚写进去的 pid 就被抹掉了
            self.fh = open(self.path, "a+")
        except OSError as exc:
            raise SystemExit(
                f"无法创建运行锁文件 {self.path}（{exc}）；请检查该路径所在目录是否存在/可写，或用 --job 指定别处的作业"
            )
        if not _try_lock(self.fh):
            self.fh.close()
            self.fh = None
            raise SystemExit(f"已有任务在运行（锁文件 {self.path}），本次退出；确认没有任务在跑时可删除该文件后重试。")
        try:
            # 拿到锁之后才截断+写自己的 pid：拿不到锁时绝不能动内容，
            # 否则每一次被拦下的启动都会把持锁进程的标记清掉
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(str(os.getpid()))
            self.fh.flush()
        except OSError:  # 写进程号只是标记，失败不影响加锁
            pass
        return self

    def __exit__(self, *exc_info):
        """解锁并关句柄；锁文件本身保留（不删文件，避免削掉别人的锁）。"""
        if self.fh is not None:
            try:
                _unlock(self.fh)
            finally:
                self.fh.close()


def _try_lock(fh) -> bool:
    """对已打开的文件加排它锁；别人拿着锁时返回 False（不阻塞等待）。"""
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True  # 两种锁都没有：不阻塞（退回"无锁"行为）


def _unlock(fh) -> None:
    """释放锁；释放失败也没关系——进程退出时内核会兜底释放，不该因此让任务报错。"""
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
    elif msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# 布尔字符串的白名单：只认这些，其它字符串（如 "flase" 这种笔误）一律报配置错。
_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def as_bool(value, default: bool, field: str = "") -> bool:
    """配置里的布尔值：JSON 写 true/false、字符串 "true"/"false"、0/1 都认。

    不能"未知一律当真"：`target.allow_empty: "flase"` 会被当成开，0 行时把已有分区清空；
    宁失败勿写错——把笔误变成一次清晰的报错，而不是一次静默的错误分支。
    """
    where = f"{field} " if field else ""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "":
            return default
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ConfigError(
            f"{where}布尔值无法识别：{value!r}；请写 JSON 的 true/false，"
            f'或字符串 "true"/"false"（也认 0/1、yes/no、on/off）'
        )
    return bool(value)


# MaxCompute 常规标识符：字母/下划线开头 + 字母/数字/下划线
_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def require_identifier(value, where: str) -> str:
    """校验一个会直接拼进 DDL / SQL 的标识符（project / table / 列名 / stored_as）。

    这些值不是请求参数，而是**拼进语句的标识符**：带空格、连字符、分号的名字要么建表失败，
    要么成为注入点。配置虽然是本机文件，但名字写错时给一句人话，远好过让 MaxCompute
    抛一句看不出所以然的语法错。
    """
    text = str(value)
    if not _IDENT_RE.match(text):
        raise ConfigError(f"{where} 不是合法的 MaxCompute 标识符：{text!r}；只允许字母/数字/下划线且不能以数字开头")
    return text


# =============================================================================
# 脱敏：日志/异常里不出现密钥、签名、token
# =============================================================================

# 密钥字段名按「词」判断：先按下划线/中划线/驼峰切开再看每个词，这样
# accessToken / client_secret / X-Api-Key 都能认出来，而 task=? 不会因为含 "sk" 被误伤
_SENSITIVE_WORDS = {
    "sign",
    "signature",
    "sig",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "authorization",
    "auth",
    "apikey",
    "key",
    "accesskey",
    "sk",
    "ak",
    # 常见简写与 scheme 名：?pwd= / ?pw= / ?pass= / bearer: <token>
    "pwd",
    "pw",
    "pass",
    "bearer",
    # 会话类凭证：Set-Cookie: sid=... / session=...
    "cookie",
    "session",
    "sid",
}
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
# 参数名做左边界限制（不用 \b：下划线在正则里算词字符，client_secret 会被漏掉）
# 分隔符同时认 "key=value" 与 "key: value"（后者以前只认行首形态，行中部的
# "X-Api-Key: xxx" 会漏遮）；替换时保留原分隔符
_QUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})([:=])([^&\s\"']+)")
# 同时认单引号：异常里直接插值的 dict（f"{cfg}"）和 repr（{exc!r}）都是单引号形态
# `(?!\\.)` 让两个分支互斥：否则 "\x" 既能走 \\.、也能走 [\s\S]，一串反斜杠会让回溯指数爆炸
# （实测 36 个反斜杠要 19 秒，且发生在"解析失败要打印原因"的必经路径上）
_JSON_RE = re.compile(r"""(?i)(["']([^"']{1,64})["']\s*:\s*)(?P<q>["'])((?:\\.|(?!\\.)(?!(?P=q))[\s\S])*)(?P=q)""")
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer)\s+)[A-Za-z0-9._~+/=-]{6,}")
_BASIC_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S{8,}")
# URL 里的 userinfo（https://user:pass@host）
_URL_AUTH_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^/\s@]+)@")
# 请求头行：'X-Api-Key: xxx' / 'X-Api-Key=xxx' 形态
_HEADER_RE = re.compile(r"(?im)^(\s*([A-Za-z0-9_.\-]{1,64})\s*[:=]\s*)(.+)$")
# 飞书 webhook 形态：open.feishu.cn/open-apis/bot/v2/hook/<id>；scheme 部分可选——
# requests 的异常消息里只带 URL 的路径（"Max retries exceeded with url: /open-apis/..."），
# 这时靠这个规则兜底，别让 hook id 明文进日志
_WEBHOOK_RE = re.compile(r"(?i)((?:https?://[^\s\"']*?)?/hook/)[A-Za-z0-9\-_]{4,}")


def _is_sensitive_key(name) -> bool:
    """字段名是否含密钥语义（按词切分，避免 "task=1" 这类含 sk 的普通参数被误伤）。"""
    original = str(name)
    # 切词要保留原大小写：驼峰分支（[A-Z][a-z0-9]*）在已经 lower 的串上永远匹配不到
    words = _WORD_RE.findall(original)
    if any(word.lower() in _SENSITIVE_WORDS for word in words):
        return True
    lowered = original.lower()
    # 不用分隔符的写法：accesstoken / secretkey / accesskeyid
    return any(
        word in lowered
        for word in (
            "token",
            "secret",
            "password",
            "passwd",
            "passphrase",
            "signature",
            "apikey",
            "accesskey",
            "secretkey",
            "privatekey",
            "signkey",
            "keyid",
        )
    )


def redact(text: str) -> str:
    """把文本里的密钥/签名/token 值替换成 ***，用于日志与异常信息。

    规则顺序按"认得出的形态"从严到宽：Bearer/Basic 与配置片段先处理——query 规则会
    按 `=` / `:` 把值截断，先跑它的话 `header: 'Authorization=Bearer abc123def'` 会被
    切成 `Authorization=`，后面的 Bearer 规则就再也匹配不到了。
    """
    if not text:
        return text

    def _bearer(match: re.Match) -> str:
        """Bearer / Basic 形态：scheme 保留，值换掉。"""
        return match.group(1) + "***"

    def _url_auth(match: re.Match) -> str:
        """URL 里的 userinfo：只留账号，密码换掉（scheme://user:***@host）。"""
        return f"{match.group(1)}:***@"

    def _webhook(match: re.Match) -> str:
        """飞书 webhook：hook 后面的 id 是凭证，遮掉。"""
        return f"{match.group(1)}***"

    def _json(match: re.Match) -> str:
        """JSON/配置片段里的 "key": "value"：只吃字符串值，保留引号结构。"""
        quote = match.group("q")
        prefix, value = match.group(1), match.group(4)
        if _is_sensitive_key(match.group(2)):
            return f"{prefix}{quote}***{quote}"
        # 值本身可能是"被 JSON 编码成字符串的一整段 JSON"（异常里 {"data": "{\"token\": \"xxx\"}"} 形态）
        if '\\"' in value:
            try:
                decoded = json.loads(f'"{value}"')
            except ValueError:
                decoded = None
            if decoded is not None:
                redacted = redact(decoded)
                if redacted != decoded:
                    return f"{prefix}{quote}{json.dumps(redacted, ensure_ascii=False)[1:-1]}{quote}"
        # 键名不敏感时值里也可能藏着密钥（'X-Api-Key: xxx' 头行、查询串、嵌套结构），递归一次
        return f"{prefix}{quote}{redact(value)}{quote}"

    def _query(match: re.Match) -> str:
        """URL 查询串 / `key: value` 里的值：命中密钥词才替换，其余原样返回。"""
        if _is_sensitive_key(match.group(1)):
            return f"{match.group(1)}{match.group(2)}***"
        value = match.group(3)
        if "%" in value:
            try:
                decoded = unquote(value)
            except Exception:  # noqa: BLE001 - 解码失败按原文处理
                decoded = value
            if decoded != value and redact(decoded) != decoded:
                return f"{match.group(1)}{match.group(2)}***"
        return f"{match.group(1)}{match.group(2)}{redact(value)}"

    def _header(match: re.Match) -> str:
        """多行文本里的一行 "Header: value"：只吃头名命中密钥词的行。"""
        if _is_sensitive_key(match.group(2)):
            return f"{match.group(1)}***"
        return f"{match.group(1)}{redact(match.group(3))}"

    out = str(text)
    out = _BEARER_RE.sub(_bearer, out)
    out = _BASIC_RE.sub(_bearer, out)
    # URL userinfo 规则必须同时出现 "://" 与 "@" 才可能匹配，先做一次 O(n) 预判：
    # 它的 [a-z0-9+.\-]* 没有长度上限，在长文本（整段十六进制转储、超长 token）上会在每个
    # 起始位置贪婪回扫，实测 20KB 就要 10 秒、40KB 要 50 秒，且 C 层正则期间 Ctrl+C 也打断不了。
    # 其余规则都有 {1,64} 之类的长度上限（实测线性），只有这一条需要预判。
    if "://" in out and "@" in out:
        out = _URL_AUTH_RE.sub(_url_auth, out)
    out = _WEBHOOK_RE.sub(_webhook, out)
    # JSON 片段规则至少要出现引号才可能匹配；没引号的长文本（十六进制转储等）直接跳过，省一遍全量扫描
    if '"' in out or "'" in out:
        out = _JSON_RE.sub(_json, out)
    out = _QUERY_RE.sub(_query, out)
    # 头行规则放最后：它最宽松（只要求行首是 name: value），前面几条先处理过更精确的形态
    return _HEADER_RE.sub(_header, out)


# =============================================================================
# 值级脱敏：配置里的密钥值本身
# =============================================================================

_SECRET_MIN_LEN = 4
"""短于该长度的密钥值不做值级替换：`1` / `ok` 这种在普通文本里出现概率太高。"""

# sftp.auth 里承载凭证的字段名（结构性字段 type 不在此列）
_SFTP_SECRET_KEYS = frozenset({"password", "passphrase", "key_file", "token", "secret_key"})
_AUTH_SCHEMES = frozenset({"basic", "bearer", "token", "digest"})
# 结构性字段后缀：值为配置项名/位置，不是凭证
_AUTH_STRUCT_SUFFIXES = ("_field", "_in", "_name", "_param")


def _leaf_strings(value) -> list[str]:
    """递归收集 dict/list/tuple/set 里的字符串叶子（数字也按 str 收）。"""
    if isinstance(value, dict):
        return [item for sub in value.values() for item in _leaf_strings(sub)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for sub in value for item in _leaf_strings(sub)]
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _with_scheme_bare(value: str) -> list[str]:
    """`Bearer sk-xxx` 除整串外再收集裸 token：接口常常只回显后半截。"""
    head, sep, tail = value.partition(" ")
    if sep and head.lower() in _AUTH_SCHEMES and tail.strip():
        return [value, tail.strip()]
    return [value]


# 飞书 webhook 里的 hook id（/hook/<id>）：报错/日志里常只出现后半截
_WEBHOOK_ID_RE = re.compile(r"/hook/([A-Za-z0-9\-_]{4,})")


def _webhook_values(value) -> list[str]:
    """webhook 除整条 URL 外，再收集裸 hook id（形态脱敏只认 URL，裸 id 要靠值级遮）。"""
    result: list[str] = []
    for part in _leaf_strings(value):
        result.append(part)
        match = _WEBHOOK_ID_RE.search(part)
        if match:
            result.append(match.group(1))
    return result


def collect_secret_values(job: dict) -> list[str]:
    """收集作业配置里"可能被回显"的密钥字面量，供值级脱敏使用。

    覆盖：job.secrets、sftp.auth 的凭证字段、sftp 块里含密钥语义的键、
    notify.webhook（hook id 即凭证）、maxcompute 的 access key。
    返回值已去重并按从长到短排序：短值先替会把长密钥切成半截、留下可辨认的碎片。
    """
    if not isinstance(job, dict):
        return []
    values: list[str] = _leaf_strings(job.get("secrets"))
    sftp_cfg = job.get("sftp")
    if isinstance(sftp_cfg, dict):
        auth_cfg = sftp_cfg.get("auth")
        if isinstance(auth_cfg, dict):
            for key, val in auth_cfg.items():
                name = str(key)
                if name in _SFTP_SECRET_KEYS or (_is_sensitive_key(name) and not name.endswith(_AUTH_STRUCT_SUFFIXES)):
                    values += [part for value in _leaf_strings(val) for part in _with_scheme_bare(value)]
        for key, val in sftp_cfg.items():
            if str(key) == "auth":
                # auth 块上面已按字段名精确收集过；再整体收会把 type 的值（"password"/"key"）
                # 当成密钥，日志里正常出现的 "password" 一词会被全文遮成 ***
                continue
            if _is_sensitive_key(str(key)):
                values += _leaf_strings(val)
    notify_cfg = job.get("notify")
    if isinstance(notify_cfg, dict):
        values += _webhook_values(notify_cfg.get("webhook"))
    maxcompute = job.get("maxcompute")
    if isinstance(maxcompute, dict):
        for key, val in maxcompute.items():
            if _is_sensitive_key(str(key)):
                values += _leaf_strings(val)
    cleaned = (value.strip() for value in values)
    return sorted({value for value in cleaned if len(value) >= _SECRET_MIN_LEN}, key=len, reverse=True)


def redact_secrets(values, text: str) -> str:
    """值级 + 形态级双重脱敏：配置里的密钥值原样出现时也遮掉。

    形态规则认的是 `token=…` / `Bearer …` / `"key": "value"` 这类写法；paramiko
    报错常把凭证写进自由文本（`Authentication failed [password=...]`），只有按配置值
    精确替换才挡得住。两条路互不替代，值级先遮、再走形态兜底。
    """
    if not text:
        return text
    text = str(text)  # 与 redact 同样的宽容度：调用方直接传异常对象/数字也不会炸
    for value in sorted(set(values or ()), key=len, reverse=True):
        # 短值（< _SECRET_MIN_LEN）连值级替换也要挡：否则 "SEC" 会把别的密钥切成碎片
        if len(value) >= _SECRET_MIN_LEN and value in text:
            text = text.replace(value, "***")
    return redact(text)


# =============================================================================
# 通用重试
# =============================================================================


def retry_call(
    fn, attempts: int = 5, base_delay: float = 15, desc: str = "", fatal=(FatalSourceError,), max_delay: float = 300
):
    """执行 fn，瞬时错误指数退避重试；FatalSourceError 与调用方声明的不重试异常直接抛出。

    重试日志与最终异常都会做脱敏，避免把密码等打进日志。
    """
    delay = base_delay
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except fatal:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/服务端类错误统一重试
            last_err = exc
            if attempt == attempts:
                break
            log(f"  [{desc} 第 {attempt}/{attempts - 1} 次失败] {redact(str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # 报"重试 N-1 次"（成功那次之外又试了几次），与实际行为一致；from last_err 保住原始异常链
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{redact(str(last_err))}") from last_err
