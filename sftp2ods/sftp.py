# -*- coding: utf-8 -*-
"""SFTP 数据源：连接、列远端文件、下载（paramiko；密码与私钥两种认证）。

设计要点：
- 每次操作独立建连、用完即断（与两个结算脚本一致）：调度任务里一次运行的文件数有限，
  换来"任何一次操作失败重试都从干净连接开始"，不留半死会话；
- 列目录与下载都走 utils.retry_call（指数退避）；认证失败（FatalSourceError）不重试；
- 下载先落 <名字>.part、核对大小后才改名生效：半截文件永远不会以正式名进解析；
- 远端布局两种：flat（root 下直接是文件）/ date_dir（root/{日期}/文件）；
- 日期一律从文件名或日期目录的命名捕获组 (?P<date>...) 提取，规范化成 YYYYMMDD。

测试友好：所有网络动作都收在 _connect / _run 两个口子上，测试替换 _connect 即可离线跑。
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .utils import ConfigError, FatalSourceError, log, retry_call

try:
    import paramiko
except ImportError:  # pragma: no cover - 未安装时给出明确指引（tests 环境也走这里）
    paramiko = None

DATE_RE = re.compile(r"\A\d{8}\Z")


def normalize_date(value: str, source_name: str, kind: str = "文件名") -> str:
    """把命名捕获组里的日期规范化成 YYYYMMDD（容忍 2026-09-21 / 2026/09/21）。

    形态或真实性不对直接报错：远端出现不能识别的日期时，宁可中止也不猜。
    """
    text = re.sub(r"[-/.]", "", str(value or "").strip())
    if not DATE_RE.match(text):
        raise ConfigError(
            f"{kind} {source_name!r} 里提取到的日期不是 8 位数字：{value!r}（检查 file_regex 的 date 捕获组）"
        )
    try:
        import datetime as _dt

        _dt.datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ConfigError(f"{kind} {source_name!r} 里提取到的日期不存在：{value!r}")
    return text


@dataclass
class RemoteFile:
    """远端一个待同步的文件。"""

    date: str  # 业务日期 YYYYMMDD（= 目标 pt）
    name: str  # 文件名
    size: int  # 字节数（远端列表值，下载后核对）
    remote: str  # 远端完整路径
    ledger_key: str  # 台账键（flat = 文件名；date_dir = 日期/文件名）

    def __repr__(self) -> str:  # 日志里简洁可读
        return f"<RemoteFile {self.date}/{self.name} {self.size}B>"


class SftpSource:
    """一个 SFTP 数据源的访问器（配置已由 config.validate_job 校验过形态）。"""

    def __init__(self, cfg: dict, source_cfg: dict | None = None):
        self.cfg = dict(cfg or {})
        auth = self.cfg.get("auth") or {}
        self.host = str(self.cfg.get("host") or "")
        self.port = int(self.cfg.get("port") or 22)
        self.username = str(self.cfg.get("username") or "")
        self.auth_type = str(auth.get("type") or "password").lower()
        self.password = str(auth.get("password") or "")
        self.key_file = str(auth.get("key_file") or "")
        self.passphrase = str(auth.get("passphrase") or "")
        self.connect_timeout = float(self.cfg.get("connect_timeout") or 30)
        self.io_timeout = float(self.cfg.get("io_timeout") or 600)
        self.retry_times = int(self.cfg.get("retry_times") or 3)
        self.retry_delay = float(self.cfg.get("retry_delay") or 10)
        source = source_cfg or self.cfg.get("_source") or {}
        self.layout = str(source.get("layout") or "flat")
        self.root = str(source.get("root") or "").strip().rstrip("/")
        self.download_dir = Path(source.get("download_dir") or ".")
        try:
            self.file_re = re.compile(str(source.get("file_regex") or ""))
        except re.error as exc:
            raise ConfigError(f"source.file_regex 不是合法正则：{exc}")
        dir_re = source.get("date_dir_regex")
        self.dir_re = None
        if self.layout == "date_dir":
            try:
                self.dir_re = re.compile(str(dir_re or ""))
            except re.error as exc:
                raise ConfigError(f"source.date_dir_regex 不是合法正则：{exc}")

    # ------------------------------------------------------------ 连接
    def _connect(self):
        """建立连接，返回 (ssh, sftp)。认证类失败抛 FatalSourceError（不重试）。

        方法单独留口：测试替换它就能离线跑全部列目录/下载逻辑。
        """
        if paramiko is None:
            raise ConfigError("缺少 paramiko：pip install paramiko（或 pip install -e .）")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # 与 StrictHostKeyChecking=no 同口径
        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        if self.auth_type == "key":
            key_path = Path(self.key_file).expanduser()
            if not key_path.is_file():
                # 密钥文件缺失是确定性错误：重试没有意义，直接失败（别让调度白等几轮退避）
                ssh.close()
                raise FatalSourceError(f"私钥文件不存在：{key_path}（sftp.auth.key_file）")
            kwargs["key_filename"] = str(key_path)
            if self.passphrase:
                kwargs["passphrase"] = self.passphrase
        else:
            kwargs["password"] = self.password
        try:
            ssh.connect(**kwargs)
            sftp = ssh.open_sftp()
        except paramiko.AuthenticationException as exc:
            ssh.close()
            raise FatalSourceError(
                f"SFTP 认证失败：{self.username}@{self.host}:{self.port} 的账号/密码/私钥不对（{exc}）"
            )
        except paramiko.PasswordRequiredException as exc:
            ssh.close()
            raise FatalSourceError(f"私钥需要口令，但 sftp.auth.passphrase 没配或不对（{exc}）")
        except paramiko.SSHException as exc:
            ssh.close()
            raise RuntimeError(f"SFTP 连接失败：{type(exc).__name__}: {exc}")
        except OSError as exc:
            ssh.close()
            raise RuntimeError(f"SFTP 连接失败（网络）：{exc}")
        try:
            channel = sftp.get_channel()
            if channel is not None:
                channel.settimeout(self.io_timeout)
        except Exception:  # noqa: BLE001 - 设置超时失败不影响已建立的会话
            pass
        return ssh, sftp

    def _run(self, desc: str, func):
        """建连 → 执行 → 断开；瞬时错误按配置重试（每次重试都是全新连接）。"""

        def _do():
            ssh, sftp = self._connect()
            try:
                return func(sftp)
            finally:
                try:
                    sftp.close()
                except Exception:  # noqa: BLE001 - 清理失败不掩盖业务结果
                    pass
                try:
                    ssh.close()
                except Exception:  # noqa: BLE001
                    pass

        return retry_call(
            _do,
            attempts=max(1, self.retry_times + 1),
            base_delay=self.retry_delay,
            desc=desc,
            fatal=(FatalSourceError,),
        )

    # ------------------------------------------------------------ 列文件
    def list_files(self) -> dict[str, list[RemoteFile]]:
        """列出远端全部匹配文件 → {业务日期: [RemoteFile, ...]}（日期升序，文件按名排序）。

        整个目录为空 / 不存在 → 返回 {}（由调用方报"远端没有任何文件"），
        与两个结算脚本口径一致：空目录是异常，但不是连接错误。
        """
        raw = self._run("列远端文件", self._scan)
        return {date: sorted(files, key=lambda item: item.name) for date, files in raw.items()}

    def _scan(self, sftp) -> dict[str, list[RemoteFile]]:
        root = self.root
        try:
            entries = sftp.listdir_attr(root or ".")
        except OSError as exc:
            log(f"  警告：列远端目录失败（{root or '.'}）：{exc}")
            return {}
        result: dict[str, list[RemoteFile]] = {}
        if self.layout == "date_dir":
            for entry in entries:
                if not stat.S_ISDIR(entry.st_mode or 0):
                    continue
                match = self.dir_re.fullmatch(entry.filename) if self.dir_re else None
                if not match:
                    continue
                date = normalize_date(match.group("date"), entry.filename, "日期目录名")
                try:
                    children = sftp.listdir_attr(f"{root}/{entry.filename}" if root else entry.filename)
                except OSError as exc:
                    log(f"  警告：列子目录失败（{entry.filename}）：{exc}")
                    continue
                for item in children:
                    if stat.S_ISDIR(item.st_mode or 0) or not self.file_re.fullmatch(item.filename):
                        continue
                    path = f"{root}/{entry.filename}/{item.filename}" if root else f"{entry.filename}/{item.filename}"
                    result.setdefault(date, []).append(
                        RemoteFile(date, item.filename, int(item.st_size or 0), path, f"{date}/{item.filename}")
                    )
        else:
            for entry in entries:
                # 目录跳过；非目录（普通文件/软链）都算候选——有的源方用软链指向当天文件
                if stat.S_ISDIR(entry.st_mode or 0):
                    continue
                match = self.file_re.fullmatch(entry.filename)
                if not match:
                    continue
                date = normalize_date(match.group("date"), entry.filename, "文件名")
                path = f"{root}/{entry.filename}" if root else entry.filename
                result.setdefault(date, []).append(
                    RemoteFile(date, entry.filename, int(entry.st_size or 0), path, entry.filename)
                )
        return result

    # ------------------------------------------------------------ 下载
    def download(self, item: RemoteFile, local_path: Path) -> Path:
        """下载一个文件到 local_path；先 .part 再核对大小改名（失败保留 .part 排障）。"""
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        def _do(sftp):
            tmp = local_path.with_name(local_path.name + ".part")
            tmp.unlink(missing_ok=True)  # 上次残留的 .part 会让行为不可预期，先删干净再下
            sftp.get(item.remote, str(tmp))
            return tmp

        tmp = self._run(f"下载 {item.name}", _do)
        actual = tmp.stat().st_size if tmp.is_file() else None
        if actual != item.size:
            raise RuntimeError(
                f"下载 {item.name} 大小不一致（远端 {item.size} 字节，本地 {actual}）；.part 已保留供排查，重跑会重新下载"
            )
        tmp.replace(local_path)
        return local_path
