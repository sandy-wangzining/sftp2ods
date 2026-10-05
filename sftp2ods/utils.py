# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote

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
        except (AttributeError, OSError, ValueError):
            # 只吞"这个流不支持 reconfigure"（含 io.UnsupportedOperation，它是 OSError/ValueError
            # 的子类）；吞掉别的异常会把本函数自身的编程错误也一起静默，日后无从排查乱码
            pass


def progress_log(label: str, count: int, unit: str = "行") -> None:
    """每 PROGRESS_EVERY 行打一条进度，避免大作业刷爆日志。"""
    if count and count % PROGRESS_EVERY == 0:
        log(f"    {label}：已处理 {count:,} {unit}")


def add_log_sink(handle) -> None:
    """把日志再写一份到文件（--log-file），句柄由调用方负责关闭。"""
    global _log_sink_warned
    with _lock:
        _sinks.append(handle)
        _log_sink_warned = False


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
_log_sink_warned = False


def reset_log_once() -> None:
    """清空"已打过的告警"记录（每次运行开始时调，见 cli.main）。"""
    global _log_sink_warned
    with _lock:
        _logged_once.clear()
        _log_sink_warned = False


def log_once(message: str) -> None:
    """同一次运行里内容相同的告警只打一次，之后静默。"""
    with _lock:
        if message in _logged_once:
            return
        _logged_once.add(message)
    log(message)


def _warn_log_sink_once(exc: BaseException) -> None:
    """日志文件写失败时往 stderr 打一条，并把该 sink 摘掉后不再静默重试。"""
    global _log_sink_warned
    with _lock:
        if _log_sink_warned:
            return
        _log_sink_warned = True
    try:
        msg = redact(str(exc))  # 与其它出口同口径：异常文本可能把密钥值写进自由文本
        sys.stderr.write(f"警告：日志文件写入失败（{type(exc).__name__}: {msg}），已停止写入该文件\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - stderr 也坏了就放弃
        pass


def log(message: str) -> None:
    """线程安全的控制台输出；时间戳=运行机器本地时间（带时区偏移），只标记执行时刻。

    防御：Windows CI/老控制台默认是 cp1252 之类编码，中文/符号会抛 UnicodeEncodeError；
    这里第一次调用时自动把控制台切到 UTF-8，切不了就用"可替换字符"降级输出，保证不中断业务。
    """
    global _console_patched
    if not _console_patched:
        # check-then-set 放进锁里：多线程首次调用时不会重复执行 setup_console
        # （TextIOWrapper.reconfigure 不是线程安全的）
        with _lock:
            if not _console_patched:
                setup_console()
                _console_patched = True

    import datetime as _dt

    # 带时区偏移的本地时间：跨时区/夏令时排障时能和调度系统、服务端日志对齐
    stamp = _dt.datetime.now(_dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    line = f"[{stamp}] {message}"
    # print 与 sink 写入都放在锁外：慢速目标（管道被压满、NFS/满盘上的 --log-file）
    # 只会拖慢这条日志本身，不该把全局 _lock 占住——否则其它线程的 log_once /
    # add_log_sink / remove_log_sink 会一起卡死，整个进程表现为停滞
    try:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = line.encode(encoding, "replace").decode(encoding, "replace")
            print(safe, flush=True)
    except (OSError, ValueError, RuntimeError, AttributeError):
        # stdout 断管/已关闭（BrokenPipeError、`| head` 提前退出；属性缺失等极端形态
        # 会抛 RuntimeError/AttributeError）：日志函数不能反过来把业务打挂
        pass
    with _lock:
        sinks = list(_sinks)  # 快照：写的时候不持锁
    failed_exc = None
    broken: list = []
    for handle in sinks:
        try:
            handle.write(line + "\n")
            handle.flush()
        except Exception as exc:  # noqa: BLE001 - 日志文件问题不影响主流程，但必须可见一次
            failed_exc = exc
            broken.append(handle)
    if failed_exc is not None:
        with _lock:
            # 从当前列表里剔除写坏的（不用写前快照覆盖：期间新加的 sink 不能丢）
            _sinks[:] = [handle for handle in _sinks if handle not in broken]
        # 摘掉的同时把句柄关掉：只从列表里移除的话句柄会一直挂到进程退出（写坏的
        # 文件句柄本就不可再用，留着只是泄漏）
        for handle in broken:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
        _warn_log_sink_once(failed_exc)


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
            # encoding + errors=replace：locale 非 UTF-8 时读写中文主机名不会抛 UnicodeError
            # POSIX 上 O_NOFOLLOW：锁路径若是符号链接就拒绝跟随（避免被指向别的文件），
            # 新建时按 0600（与 feishu2ods 的锁同口径）
            open_kwargs: dict = {"encoding": "utf-8", "errors": "replace"}
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                open_kwargs["opener"] = lambda path, flags, _nf=nofollow: os.open(path, flags | _nf, 0o600)
            self.fh = open(self.path, "a+", **open_kwargs)
        except (OSError, UnicodeError) as exc:
            raise SystemExit(
                f"无法创建运行锁文件 {self.path}（{exc}）；请检查该路径所在目录是否存在/可写，或用 --job 指定别处的作业"
            ) from exc
        try:
            locked = _try_lock(self.fh)
        except BaseException:
            # 含 SystemExit（文件系统不支持锁且未设 SFTP2ODS_ALLOW_NO_LOCK）：__enter__ 抛出后
            # with 不会调 __exit__，不在这里关句柄就按次泄漏 fd（与 interprocess_lock 同口径）
            self.fh.close()
            self.fh = None
            raise
        if not locked:
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


def _lock_error_kind(exc: OSError) -> str:
    """文件锁 OSError：busy / unsupported / other。"""
    code = getattr(exc, "errno", None)
    busy = {errno.EAGAIN, errno.EACCES, errno.EDEADLK}
    wow = getattr(errno, "EWOULDBLOCK", None)
    if wow is not None:
        busy.add(wow)
    unsupported = set()
    for name in ("ENOLCK", "ENOTSUP", "EOPNOTSUPP"):
        val = getattr(errno, name, None)
        if val is not None:
            unsupported.add(val)
    if code in busy:
        return "busy"
    if code in unsupported:
        return "unsupported"
    return "other"


def _handle_lock_oserror(exc: OSError, *, blocking: bool) -> bool:
    """busy → 非阻塞返回 False；文件系统不支持锁 → 默认 fail-closed 拒绝执行
    （除非显式 SFTP2ODS_ALLOW_NO_LOCK=1 接受无互斥风险）；其余抛出。"""
    kind = _lock_error_kind(exc)
    if kind == "busy":
        if blocking:
            raise
        return False
    if kind == "unsupported":
        if os.environ.get("SFTP2ODS_ALLOW_NO_LOCK", "").strip() == "1":
            log_once(
                f"  警告：文件系统不支持文件锁（{exc}）；SFTP2ODS_ALLOW_NO_LOCK=1 "
                f"已显式接受无互斥风险，本次不加锁继续执行"
            )
            return True
        # fail-closed：无锁继续会让两个实例并发写同一作业/表、台账丢更新，宁可拒绝执行
        raise SystemExit(
            f"文件系统不支持文件锁（{exc}）：拒绝无锁执行（并发实例会互相写坏数据）。"
            f"请把锁目录/台账放到支持锁的本地磁盘，或确认无人并发时设置 SFTP2ODS_ALLOW_NO_LOCK=1"
        )
    raise  # 裸 raise：保留原始 traceback（raise exc 会把堆栈重置到这里）


def _acquire_lock(fh, *, blocking: bool) -> bool:
    """加排它锁。True = 已持有或无锁继续；False = 非阻塞时锁被占用。"""
    if fcntl is not None:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fh, flags)
            return True
        except OSError as exc:
            return _handle_lock_oserror(exc, blocking=blocking)
    if msvcrt is not None:
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), mode, 1)
            return True
        except OSError as exc:
            return _handle_lock_oserror(exc, blocking=blocking)
    return True


def _try_lock(fh) -> bool:
    """对已打开的文件加排它锁；别人拿着锁时返回 False（不阻塞等待）。"""
    return _acquire_lock(fh, blocking=False)


@contextmanager
def interprocess_lock(path: Path):
    """进程间排它锁（阻塞），Windows 用 msvcrt、POSIX 用 flock；不支持则告警后无锁继续。

    注意 Windows 的等待语义：msvcrt.LK_LOCK 只会重试约 10 秒（每秒一次），超时后
    抛 EDEADLOCK，被 _handle_lock_oserror 按 busy 上抛——即 Windows 上"阻塞"最多
    等约 10 秒；POSIX 的 flock 才是真正无限等待。临界区都是秒级（台账/状态写回），
    生产跑 Linux，这里以文档口径为准不额外加循环重试。

    锁文件本身不删除（避免削掉别人的锁）。用于台账这类短临界区的 load-modify-save。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return
    # 与 RunLock 同一套加固：POSIX 上 O_NOFOLLOW（锁路径若是符号链接就拒绝跟随，
    # 免得锁到/写到别人的文件上）、新建按 0600；encoding/errors 防 locale 非 UTF-8
    open_kwargs: dict = {"encoding": "utf-8", "errors": "replace"}
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        open_kwargs["opener"] = lambda p, flags, _nf=nofollow: os.open(p, flags | _nf, 0o600)
    fh = open(path, "a+", **open_kwargs)
    try:
        _acquire_lock(fh, blocking=True)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


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
    if isinstance(value, (int, float)):
        return bool(value)  # 0 → False、1 → True（数字配置的常见写法）
    # 其它类型（数组/对象）不能 bool() 兜底：[] 会被静默当成 False、绕过 fail-closed 约定
    raise ConfigError(f"{where}布尔值类型不支持：{type(value).__name__}（{value!r}）；请写 true/false 或 0/1")


# MaxCompute 常规标识符：字母/下划线开头 + 字母/数字/下划线
_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def require_identifier(value, where: str) -> str:
    """校验一个会直接拼进 DDL / SQL 的标识符（project / table / 列名 / stored_as）。

    这些值不是请求参数，而是**拼进语句的标识符**：带空格、连字符、分号的名字要么建表失败，
    要么成为注入点。配置虽然是本机文件，但名字写错时给一句人话，远好过让 MaxCompute
    抛一句看不出所以然的语法错。
    """
    if not isinstance(value, str) or not value:
        # 不能先 str() 再校验：str(None) == "None"、str(True) == "True" 都能过标识符正则，
        # 配置漏填时会被静默拼出一个名叫 None / True 的表名——快速失败，别写错对象
        raise ConfigError(f"{where} 缺失或不是字符串：{value!r}")
    if not _IDENT_RE.match(value):
        raise ConfigError(f"{where} 不是合法的 MaxCompute 标识符：{value!r}；只允许字母/数字/下划线且不能以数字开头")
    return value


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
# 分隔符同时认 "key=value" 与 "key: value"，且允许分隔符后跟空格/制表符（组 3 = 保留原空白）：
# 没有这个 \s*，"X-Api-Key: sk-xxx"（冒号后带空格）在行中部两条规则都匹配不上，密钥会明文进
# 日志（requests 异常里就是 `... [X-Api-Key: sk-xxx]` 这种形态）
_QUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})([\"']?)([:=])([ \t]*)([^&\s\"']+)")
# 敏感键的值吃到行尾/`&`为止：`password=my secret` 原来只遮 "my"、"secret" 明文留下
# （口令短语很常见）。负向先行断言只挡「引号后紧跟 ***」的已遮罩文本（避免把
# `"secret_key": "***", "page": 2` 整行再吞一遍）；未闭合引号（password="abc 被日志
# 截断）或「带引号的键」+ 不带引号的值（\"password\": my secret）必须走这条兜底——
# 否则 KV/JSON 要收尾引号、常规 QUERY 的值类不吃引号，三套规则全绕过
_QUERY_SPACE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<key>[A-Za-z0-9_.\-]{1,64})(?P<q>[\"']?)(?P<sep>\s*[:=]\s*)(?!\s*[\"']?\s*\*\*\*)(?P<val>[^&\n]+)"
)


def _mask_spaced_values(text: str) -> str:
    """敏感键 + 无引号值遮到行尾/`&`（口令短语含空格不被第一个词截断）。

    用扫描器而不是一次 sub：非敏感键的贪婪值会吞掉其后的 k=v（sub 不重叠），
    这里非敏感只前移到"值起点"继续扫，后续键照常处理。
    """
    parts: list[str] = []
    pos = 0
    while True:
        match = _QUERY_SPACE_RE.search(text, pos)
        if not match:
            parts.append(text[pos:])
            break
        parts.append(text[pos : match.start()])
        head = f"{match.group('key')}{match.group('q')}{match.group('sep')}"
        if _is_sensitive_key(match.group("key")):
            parts.append(head + "***")
            pos = match.end()
        else:
            parts.append(head)
            pos = match.start("val")
    return "".join(parts)


# 同时认单引号：异常里直接插值的 dict（f"{cfg}"）和 repr（{exc!r}）都是单引号形态
# `(?!\\.)` 让两个分支互斥：否则 "\x" 既能走 \\.、也能走 [\s\S]，一串反斜杠会让回溯指数爆炸
# （实测 36 个反斜杠要 19 秒，且发生在"解析失败要打印原因"的必经路径上）
_JSON_RE = re.compile(r"""(?i)(["']([^"']{1,64})["']\s*:\s*)(?P<q>["'])((?:\\.|(?!\\.)(?!(?P=q))[\s\S])*)(?P=q)""")
# 键不带引号、值带引号（access_token='t-xxx' / app_secret: "xx"）：f-string 的 !r 插值与
# repr 的输出正好是这种形态，而 _QUERY_RE 的值部分 [^&\s"']+ 不吃引号——行中出现的这类
# 取值会整段漏遮（行首的由 _HEADER_RE 兜底，行中不会）。值体与 _JSON_RE 同款互斥分支，
# 转义引号与「另一种引号出现在值里」（repr 会改用另一种引号包裹）都能认。
_KV_QUOTED_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})(\s*[:=]\s*)(?P<q>[\"'])((?:\\.|(?!\\.)(?!(?P=q))[^\n])*)(?P=q)"
)
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer)\s+)[A-Za-z0-9._~+/=-]{6,}")
_BASIC_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S{8,}")
# URL 里的 userinfo（https://user:pass@host）。scheme 部分限长（{0,63}）：无上限时
# 在长小写字母数字串上会在每个起始位置贪婪回扫（实测 20KB 要 10 秒、40KB 要 50 秒），
# 限长后整条规则保持线性（真实 scheme 远短于 63 个字符）
_URL_AUTH_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,63}://[^/\s:@]+):([^\s/]+)@")
# 口令部分吃到最后一个 @ 为止（host 里不可能有 @）：`user:p@ss@host` 原来在第一个 @
# 截断，口令余段（@ss@host 的 p 之后部分）明文留下
# 请求头行：'X-Api-Key: xxx' / 'X-Api-Key=xxx' 形态
_HEADER_RE = re.compile(r"(?im)^(\s*([A-Za-z0-9_.\-]{1,64})\s*[:=]\s*)(.+)$")
# 飞书 webhook 形态：open.feishu.cn/open-apis/bot/v2/hook/<id>；scheme 部分可选——
# requests 的异常消息里只带 URL 的路径（"Max retries exceeded with url: /open-apis/..."），
# 这时靠这个规则兜底，别让 hook id 明文进日志
# 可选前缀限长（{0,1024}）：无上限的惰性展开在"超长且无空白、又没有 /hook/"的
# 文本上会二次回溯（每个 https:// 起点都要扫到 token 末尾）；限长后保持线性。
# 真实 webhook 的 URL 前缀远短于 1024 字符。
_WEBHOOK_RE = re.compile(r"(?i)((?:https?://[^\s\"']{0,256}?)?/hook/)[A-Za-z0-9\-_]{4,}")


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


def redact(text: str, _depth: int = 0) -> str:
    """把文本里的密钥/签名/token 值替换成 ***，用于日志与异常信息。

    规则顺序按"认得出的形态"从严到宽：Bearer/Basic 与配置片段先处理——query 规则会
    按 `=` / `:` 把值截断，先跑它的话 `header: 'Authorization=Bearer abc123def'` 会被
    切成 `Authorization=`，后面的 Bearer 规则就再也匹配不到了。

    _depth：内部递归深度，调用方不要传。_percent_encoded_secret 解码后回调 redact，
    而 redact 又会解 "%25..."（每层只减 3 个字符）：上千字符的多层编码文本能把递归
    喂到 Python 上限，把"脱敏"本身打成 RecursionError。到上限按"宁可多脱敏"整段遮掉。
    """
    if not text:
        return text
    if _depth >= _MAX_REDACT_DEPTH:
        return "***"

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
            except (ValueError, RecursionError):
                # RecursionError：值里含超深嵌套（构造性 payload）时 json.loads 会递归爆栈；
                # 脱敏流程不能反过来把进程打崩，按"反转义失败"处理
                decoded = None
            if decoded is not None:
                redacted = redact(decoded, _depth + 1)
                if redacted != decoded:
                    return f"{prefix}{quote}{json.dumps(redacted, ensure_ascii=False)[1:-1]}{quote}"
        # 键名不敏感时值里也可能藏着密钥（'X-Api-Key: xxx' 头行、查询串、嵌套结构），递归一次。
        # 深度要透传（_depth+1）：漏传的话深度护栏在这条路径上失效
        return f"{prefix}{quote}{redact(value, _depth + 1)}{quote}"

    def _kv_quoted(match: re.Match) -> str:
        """`key='value'` / `key: "value"`（键无引号、值有引号）：命中密钥词才遮值。"""
        key, gap, qchar, value = (match.group(1), match.group(2), match.group(3), match.group(4))
        head = f"{key}{gap}{qchar}"
        if _is_sensitive_key(key):
            return f"{head}***{qchar}"
        # 键名不敏感时值里也可能藏着密钥（'note=access_token=abc'）：递归一次兜底
        redacted = redact(value, _depth + 1)
        if redacted != value:
            return f"{head}{redacted}{qchar}"
        return match.group(0)

    out = str(text)
    out = _BEARER_RE.sub(_bearer, out)
    out = _BASIC_RE.sub(_bearer, out)
    # URL userinfo 规则必须同时出现 "://" 与 "@" 才可能匹配，先做一次 O(n) 预判省掉
    # 一次无谓的全量扫描。该规则的 scheme 部分已限长（见 _URL_AUTH_RE），配合预判在
    # 长文本（整段十六进制转储、超长 token）上保持线性；没有预判 + 无上限的旧写法
    # 实测 20KB 要 10 秒、40KB 要 50 秒，且 C 层正则期间 Ctrl+C 也打断不了。
    if "://" in out and "@" in out:
        out = _URL_AUTH_RE.sub(_url_auth, out)
    out = _WEBHOOK_RE.sub(_webhook, out)
    # JSON 片段规则至少要出现引号才可能匹配；没引号的长文本（十六进制转储等）直接跳过，省一遍全量扫描
    if '"' in out or "'" in out:
        out = _JSON_RE.sub(_json, out)
        out = _KV_QUOTED_RE.sub(_kv_quoted, out)
    # 敏感键 + 无引号值先整体遮到行尾（password=my secret），再走常规 query 扫描
    out = _mask_spaced_values(out)
    return _query_header_redact(out, _depth)


def _query_header_redact(text: str, _depth: int = 0) -> str:
    """查询串与头行脱敏：迭代扫描，避免按「段数」递归把长 query 打成 O(n²)/RecursionError。"""
    return _HEADER_RE.sub(_header_mask, _replace_query_iter(text, _depth))


def _header_mask(match: re.Match) -> str:
    if _is_sensitive_key(match.group(2)):
        return f"{match.group(1)}***"
    return match.group(0)


def _replace_query_iter(text: str, _depth: int = 0) -> str:
    """从左到右替换 query 形态；非敏感键的值再从值起点继续扫（嵌套 key=value），不调用 redact()。"""
    parts: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        match = _QUERY_RE.search(text, pos)
        if not match:
            parts.append(text[pos:])
            break
        parts.append(text[pos : match.start()])
        key, quote, sep, gap, value = (match.group(1), match.group(2), match.group(3), match.group(4), match.group(5))
        if _is_sensitive_key(key) or _percent_encoded_secret(value, _depth):
            parts.append(f"{key}{quote}{sep}{gap}***")
            pos = match.end()
            continue
        parts.append(f"{key}{quote}{sep}{gap}")
        next_pos = match.start(5)
        if next_pos <= pos:
            pos = match.end()
            parts.append(value)
            continue
        pos = next_pos
    return "".join(parts)


def _percent_encoded_secret(value: str, _depth: int = 0) -> bool:
    if "%" not in value:
        return False
    try:
        decoded = unquote(value)
    except Exception:  # noqa: BLE001 - 解码失败按原文处理
        return False
    if decoded == value:
        return False
    # 解码后再走完整脱敏（Bearer/JSON 等）；深度跟 % 解码层数走，不跟 query 段数走
    return redact(decoded, _depth + 1) != decoded


# =============================================================================
# 值级脱敏：配置里的密钥值本身
# =============================================================================

_SECRET_MIN_LEN = 4
"""短于该长度的密钥值不做值级替换：`1` / `ok` 这种在普通文本里出现概率太高。"""

# 脱敏递归的深度上限：正常文本 ≤1 层（query 值里再嵌编码），构造性文本
# （多层 %25 编码，每层只减 3 个字符）能打爆递归栈；到上限按"宁可多脱敏"整段遮掉
_MAX_REDACT_DEPTH = 10

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
    if not isinstance(values, (list, tuple, set, frozenset)):
        # 单个字符串会被 set() 拆成单字符（值级脱敏静默失效、凭证明文进日志）；
        # int/None 等标量会让下面的 for 抛 TypeError、把真正的失败原因顶掉。
        # 一律按"只有一个密钥"包一层
        values = [values]
    text = str(text)  # 与 redact 同样的宽容度：调用方直接传异常对象/数字也不会炸
    # 密钥值先 str 化（与 api2ods 同口径）：job.secrets 里的数字（如 app_id）直接传进来时
    # len() 会 TypeError，把真正的失败原因顶掉；None/bool 不是密钥（str 化后会把文本里的
    # "None"/"True" 误替成 ***），跳过
    secrets = {str(v) for v in (values or ()) if v is not None and not isinstance(v, bool)}
    for secret in sorted(secrets, key=len, reverse=True):
        # 短值（< _SECRET_MIN_LEN）连值级替换也要挡：否则 "SEC" 会把别的密钥切成碎片
        if len(secret) < _SECRET_MIN_LEN:
            continue
        # 凭证可能以 URL 编码形态出现在自由文本里（`+`/`/`/`=` 会被 quote 编码；
        # 部分编码器更激进，连 `-` 这类字符也编码成 %2D），而自由文本没有可识别的
        # 键名，形态规则挡不住；只替明文会漏。明文、quote、quote_plus 与"非字母数字
        # 全编码"四种形态一起替换（长值优先的排序不变）。
        # 按字节（不是 chr(b) 的 Latin-1 字符）判断：>=0x80 的字节在 Latin-1 里常恰好是
        # "字母"（0xE5='å'），原样保留会让含中文的密钥生成错误的编码变体、漏遮
        aggressive = "".join(
            f"%{b:02X}" if not (b < 128 and chr(b).isalnum()) else chr(b)
            for b in secret.encode("utf-8", "surrogatepass")
        )
        for variant in (secret, quote(secret, safe=""), quote_plus(secret), aggressive):
            if variant:
                text = text.replace(variant, "***")
    return redact(text)


# =============================================================================
# 通用重试
# =============================================================================


# requests 的「确定性」异常：请求根本没发出去（缺 scheme、URL/请求头非法），重试多少次
# 都是同一结果。都是 ValueError 子类，直接加 ValueError 会把"响应体解析失败"这类
# 可能重试成功的错误也卷进来，所以按类型点名；requests 未安装时为空
try:  # pragma: no cover - requests 未安装的离线环境走空元组
    from requests import exceptions as _requests_exceptions

    DETERMINISTIC_HTTP_ERRORS: tuple[type[BaseException], ...] = tuple(
        exc_type
        for name in ("MissingSchema", "InvalidSchema", "InvalidURL", "InvalidHeader", "URLRequired")
        if isinstance(exc_type := getattr(_requests_exceptions, name, None), type)
    )
except ImportError:  # pragma: no cover
    DETERMINISTIC_HTTP_ERRORS = ()


def retry_call(
    fn,
    attempts: int = 5,
    base_delay: float = 15,
    desc: str = "",
    fatal=(FatalSourceError,),
    max_delay: float = 300,
    secrets=(),
):
    """执行 fn，瞬时错误指数退避重试；FatalSourceError 与调用方声明的不重试异常直接抛出。

    重试日志与最终异常都会做脱敏，避免把密码等打进日志。secrets 给定时（如
    collect_secret_values 的结果）另外做值级脱敏：凭证出现在自由文本里时形态规则挡不住。
    """
    if attempts < 1:
        # attempts<=0 时循环体一次都不执行，last_err 保持 None，最终报错会变成
        # "重试 -1 次仍失败：None"（丢失失败原因）——提前给一句明确的参数错误
        raise ValueError(f"retry_call 的 attempts 必须 >= 1，当前 {attempts}")
    delay = min(base_delay, max_delay)  # 首次退避也受上限约束：base_delay 配得比 max_delay 大时不超
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except fatal:
            raise
        except (
            TypeError,
            AttributeError,
            KeyError,
            NameError,
            ImportError,
            *DETERMINISTIC_HTTP_ERRORS,
        ) as exc:
            # 确定性编程错误：字段名写错/对象类型不对，重试多少次都是同一个结果，
            # 退避只会白等几分钟、还把原始错误类型包成 RuntimeError 掩盖掉。
            # 消息同样过脱敏（KeyError 回显的键里可能带凭证值）
            raise RuntimeError(
                f"{desc} 出现确定性错误（不重试）：{type(exc).__name__}: {redact_secrets(secrets, str(exc))}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 网络/服务端类错误统一重试
            last_err = exc
            if attempt == attempts:
                break
            # 分子/分母都按"总尝试次数"口径，避免写成 第 x/(n-1) 次 这种对不上的读法
            log(f"  [{desc} 第 {attempt}/{attempts} 次尝试失败] {redact_secrets(secrets, str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # 报"重试 N-1 次"（成功那次之外又试了几次），与实际行为一致；from last_err 保住原始异常链
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{redact_secrets(secrets, str(last_err))}") from last_err
