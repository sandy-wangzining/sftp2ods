# -*- coding: utf-8 -*-
"""sftp：日期规范化、两种布局的列目录、下载（.part/大小核对/重试）、连接参数。"""

from __future__ import annotations

import errno
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import FakeEntry, FakeSftp, OfflineTestCase, add_dir, add_file, connect_to  # noqa: E402

from sftp2ods import sftp as sftp_mod  # noqa: E402
from sftp2ods.utils import FatalSourceError  # noqa: E402


def flat_source(**overrides) -> sftp_mod.SftpSource:
    cfg = {
        "host": "h",
        "port": 22,
        "username": "u",
        "auth": {"type": "password", "password": "pw"},
        "retry_times": 0,
    }
    cfg.update(overrides.pop("sftp", {}))
    source = {"root": "/data", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})\\.csv"}
    source.update(overrides.pop("source", {}))
    if overrides:
        raise TypeError(f"未知 override: {sorted(overrides)}")
    return sftp_mod.SftpSource(cfg, source)


def date_dir_source(**overrides) -> sftp_mod.SftpSource:
    return flat_source(
        source={
            "root": "settlements",
            "layout": "date_dir",
            "date_dir_regex": "(?P<date>\\d{8})",
            "file_regex": "detail_.*_(?P<date>\\d{8})_USD\\.csv",
        },
        **overrides,
    )


class TestSourceConfigGuards(OfflineTestCase):
    def test_layout_case_is_accepted(self):
        """`"DATE_DIR"` 这类大小写变体要按 date_dir 处理（与 validate_job 的 .lower() 口径一致）。"""
        source = flat_source(
            source={
                "root": "settlements",
                "layout": "DATE_DIR",
                "date_dir_regex": "(?P<date>\\d{8})",
                "file_regex": "detail_(?P<date>\\d{8})\\.csv",
            }
        )
        self.assertEqual(source.layout, "date_dir")
        self.assertIsNotNone(source.dir_re)

    def test_file_regex_requires_date_group(self):
        """file_regex 缺 (?P<date>) 时必须在配置阶段报错（否则扫描时抛 IndexError，还会被重试）。"""
        with self.assertRaises(SystemExit) as ctx:
            flat_source(source={"file_regex": r"report_\d{8}\.csv"})
        self.assertIn("(?P<date>", str(ctx.exception))

    def test_date_dir_regex_requires_date_group(self):
        """只让 date_dir_regex 非法：file_regex 用合法带组值，断言唯一指向 date_dir_regex
        的校验（两处都缺 (?P<date>) 时，先触发的 file_regex 校验会让用例假绿）。"""
        with self.assertRaises(SystemExit) as ctx:
            flat_source(
                source={
                    "root": "/d",
                    "layout": "date_dir",
                    "file_regex": r"detail_(?P<date>\d{8})\.csv",
                    "date_dir_regex": r"\d{8}",
                }
            )
        self.assertIn("date_dir_regex", str(ctx.exception))
        self.assertIn("(?P<date>", str(ctx.exception))


class TestZeroConfigAndRoot(OfflineTestCase):
    def test_unknown_override_keys_error(self):
        with self.assertRaises(TypeError) as ctx:
            flat_source(host="x")
        self.assertIn("未知", str(ctx.exception))

    def test_zero_timeouts_and_delay_are_kept(self):
        source = flat_source(sftp={"retry_times": 0, "retry_delay": 0, "connect_timeout": 0, "io_timeout": 0})
        self.assertEqual(source.retry_times, 0)
        self.assertEqual(source.retry_delay, 0.0)
        self.assertEqual(source.connect_timeout, 0.0)
        self.assertEqual(source.io_timeout, 0.0)

    def test_root_slash_lists_filesystem_root(self):
        """root="/" 必须扫根目录，不能 rstrip 成空后再 listdir(".")（家目录）。"""
        fake = FakeSftp()
        fake.tree["/"] = [FakeEntry("report_20260920.csv", 4)]
        fake.contents["/report_20260920.csv"] = b"a,b\n"
        fake.tree["."] = []
        source = flat_source(source={"root": "/"}, sftp={"retry_times": 0})
        self.assertEqual(source.root, "/")
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertIn("20260920", files)
        self.assertEqual(files["20260920"][0].remote, "/report_20260920.csv")


class TestNormalize(OfflineTestCase):
    def test_valid(self):
        self.assertEqual(sftp_mod.normalize_date("20260920", "f"), "20260920")
        self.assertEqual(sftp_mod.normalize_date("2026-09-20", "f"), "20260920")
        self.assertEqual(sftp_mod.normalize_date("2026/09/20", "f"), "20260920")

    def test_invalid_forms(self):
        for bad in ("2026920", "abcdefgh", "", "2026-09"):
            with self.assertRaises(SystemExit):
                sftp_mod.normalize_date(bad, "f")

    def test_invalid_real_date(self):
        with self.assertRaises(SystemExit) as ctx:
            sftp_mod.normalize_date("20261301", "f")
        self.assertIn("不存在", str(ctx.exception))


class TestScanFlat(OfflineTestCase):
    def test_matches_and_ignores(self):
        fake = FakeSftp()
        add_file(fake, "/data/report_20260920.csv", b"a,b\n1,2\n")
        add_file(fake, "/data/report_20260921.csv", b"a,b\n1,2\n")
        add_file(fake, "/data/other_20260920.csv", b"x")
        add_dir(fake, "/data/subdir")
        source = flat_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual(sorted(files), ["20260920", "20260921"])
        item = files["20260920"][0]
        self.assertEqual(item.name, "report_20260920.csv")
        self.assertEqual(item.remote, "/data/report_20260920.csv")
        self.assertEqual(item.ledger_key, "report_20260920.csv")
        self.assertEqual(item.size, len(b"a,b\n1,2\n"))

    def test_root_missing_is_empty(self):
        source = flat_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(FakeSftp())):
            self.assertEqual(source.list_files(), {})

    def test_multi_files_per_date_sorted(self):
        fake = FakeSftp()
        add_file(fake, "/data/report_20260920.csv", b"1")
        add_file(fake, "/data/report_20260920_2.csv", b"22")
        source = flat_source(source={"file_regex": "report_(?P<date>\\d{8})(?:_2)?\\.csv"})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual([item.name for item in files["20260920"]], ["report_20260920.csv", "report_20260920_2.csv"])

    def test_symlink_file_accepted(self):
        """有的源方用软链指向当天文件；只要不是目录、名字匹配就应视为候选。"""
        from _helpers import FakeEntry

        fake = FakeSftp()
        link = FakeEntry("report_20260920.csv", 4)
        link.st_mode = stat.S_IFLNK | 0o777
        fake.tree["/data"] = [link]
        fake.contents["/data/report_20260920.csv"] = b"a\n1\n"
        source = flat_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual(sorted(files), ["20260920"])

    def test_bad_date_in_name_errors(self):
        fake = FakeSftp()
        add_file(fake, "/data/report_20261399.csv", b"1")
        source = flat_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            with self.assertRaises(SystemExit):
                source.list_files()


class TestScanDateDir(OfflineTestCase):
    def test_date_from_dir(self):
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        add_dir(fake, "settlements/20260921")
        add_file(fake, "settlements/20260920/detail_x_20260920_USD.csv", b"a\n1\n")
        add_file(fake, "settlements/20260920/summary_x_20260920_USD.csv", b"zz")
        add_file(fake, "settlements/20260921/detail_y_20260920_USD.csv", b"a\n1\n")
        add_file(fake, "settlements/stray.csv", b"x")
        source = date_dir_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual(sorted(files), ["20260920", "20260921"])
        self.assertEqual([item.name for item in files["20260920"]], ["detail_x_20260920_USD.csv"])
        item = files["20260921"][0]
        # pt 以目录日期为准（文件里的日期只是命名风格）
        self.assertEqual(item.date, "20260921")
        self.assertEqual(item.remote, "settlements/20260921/detail_y_20260920_USD.csv")
        self.assertEqual(item.ledger_key, "20260921/detail_y_20260920_USD.csv")

    def test_bad_port_is_config_error(self):
        """sftp.port 非法值给配置错（原来抛裸 ValueError）；范围也校验。"""
        for bad in ("abc", 0, 70000):
            with self.assertRaises(sftp_mod.ConfigError):
                sftp_mod.SftpSource({"host": "h", "username": "u", "port": bad})

    def test_symlinked_date_dir_is_scanned(self):
        """指向日期目录的软链（st_mode 是 S_IFLNK）要跟随判断：flat 布局显式支持软链，
        date_dir 下若直接跳过，整个业务日期会从结果里静默消失。"""
        fake = FakeSftp()
        fake.tree["settlements"] = [FakeEntry("20260920", 0, is_dir=True, is_link=True)]
        fake.tree["settlements/20260920"] = [FakeEntry("detail_x_20260920_USD.csv", 8)]
        fake.contents["settlements/20260920/detail_x_20260920_USD.csv"] = b"a,b\n1,2\n"
        source = date_dir_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual(sorted(files), ["20260920"])
        self.assertEqual(files["20260920"][0].remote, "settlements/20260920/detail_x_20260920_USD.csv")

    def test_empty_subdir_yields_nothing(self):
        """日期目录存在但没有匹配文件：返回空（列目录失败的两条语义由下面两个用例分别覆盖）。"""
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        source = date_dir_source()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            files = source.list_files()
        self.assertEqual(files, {})

    def test_subdir_transient_failure_propagates(self):
        """子目录列失败若是瞬时错误（非"路径不存在"），必须抛出去让重试生效。

        修复前 _scan 把所有 OSError 都吞成"跳过这个日期"：网络抖一下就会静默少一天数据，
        而且 retry_call 永远看不到失败、重试形同虚设。
        """
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        add_file(fake, "settlements/20260920/detail_20260920_USD.csv", b"a,b\n1,2\n")
        original = fake.listdir_attr

        def flaky(path):
            if path.endswith("20260920"):
                raise OSError("connection reset by peer")  # 非 ENOENT 的瞬时错误
            return original(path)

        source = date_dir_source(sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            with mock.patch.object(fake, "listdir_attr", flaky):
                with self.assertRaises(RuntimeError) as ctx:
                    source.list_files()
        self.assertIn("connection reset", str(ctx.exception))

    def test_subdir_missing_is_skipped(self):
        """子目录真的不存在（ENOENT）时才按"没有这一天"跳过。"""
        fake = FakeSftp()
        add_dir(fake, "settlements/20260920")
        original = fake.listdir_attr

        def vanished(path):
            if path.endswith("20260920"):
                raise OSError(errno.ENOENT, "No such file", path)
            return original(path)

        source = date_dir_source(sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            with mock.patch.object(fake, "listdir_attr", vanished):
                self.assertEqual(source.list_files(), {})


class TestDownload(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = FakeSftp()
        add_file(self.fake, "/data/report_20260920.csv", b"a,b\n1,2\n")
        self.source = flat_source()
        self.item = None

    def _item(self):
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            return self.source.list_files()["20260920"][0]

    def test_download_success(self):
        item = self._item()
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            target = self.source.download(item, self.tmp / item.ledger_key)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"a,b\n1,2\n")
        self.assertFalse((self.tmp / (item.name + ".part")).exists())
        self.assertEqual(self.fake.downloaded, ["/data/report_20260920.csv"])

    def test_download_size_mismatch_keeps_part(self):
        item = self._item()
        # 远端列表声明 999 字节，实际内容 8 字节 → 拒绝改名
        item.size = 999
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            with self.assertRaises(RuntimeError) as ctx:
                self.source.download(item, self.tmp / item.name)
        self.assertIn("大小不一致", str(ctx.exception))
        self.assertTrue((self.tmp / (item.name + ".part")).is_file())
        self.assertFalse((self.tmp / item.name).exists())

    def test_download_retries_then_succeeds(self):
        item = self._item()
        self.fake.fail_downloads = 1
        source = flat_source(sftp={"retry_times": 1, "retry_delay": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            target = source.download(item, self.tmp / item.name)
        self.assertTrue(target.is_file())
        self.assertEqual(self.fake.get_calls, 2)

    def test_download_failure_exhausts(self):
        item = self._item()
        self.fake.fail_downloads = 5
        source = flat_source(sftp={"retry_times": 1, "retry_delay": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            with self.assertRaises(RuntimeError):
                source.download(item, self.tmp / item.name)

    def test_retry_times_zero_does_not_retry(self):
        """retry_times=0 表示只试一次，不能被 `x or 3` 吞成 3 次重试。"""
        item = self._item()
        self.fake.fail_downloads = 1
        source = flat_source(sftp={"retry_times": 0, "retry_delay": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(self.fake)):
            with self.assertRaises(RuntimeError):
                source.download(item, self.tmp / item.name)
        self.assertEqual(self.fake.get_calls, 1)


class FakeChannel:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class FakeSftpHandle(FakeSftp):
    def __init__(self):
        super().__init__()
        self.channel = FakeChannel()

    def get_channel(self):
        return self.channel


class _ParamikoFixture(OfflineTestCase):
    """只放 _fake_paramiko 等共用夹具，不含 test_*：避免子类继承父类用例被重复执行。"""

    def _fake_paramiko(self):
        module = mock.MagicMock(name="paramiko")

        class AuthenticationException(Exception):
            pass

        class SSHException(Exception):
            pass

        class PasswordRequiredException(SSHException):
            pass

        class BadHostKeyException(SSHException):
            pass

        class AutoAddPolicy:
            pass

        class RejectPolicy:
            pass

        module.AuthenticationException = AuthenticationException
        module.SSHException = SSHException
        module.PasswordRequiredException = PasswordRequiredException
        module.BadHostKeyException = BadHostKeyException
        module.AutoAddPolicy = AutoAddPolicy
        module.RejectPolicy = RejectPolicy
        last = {}

        class SSHClient:
            def __init__(self):
                self.kwargs = None
                last["client"] = self

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def load_system_host_keys(self):
                self.system_host_keys_loaded = True

            def load_host_keys(self, filename):
                self.user_host_keys = getattr(self, "user_host_keys", [])
                self.user_host_keys.append(filename)

            def connect(self, **kwargs):
                if last.get("fail_auth"):
                    raise AuthenticationException("bad password")
                if last.get("fail_passphrase"):
                    raise PasswordRequiredException("private key file is encrypted")
                if last.get("fail_bad_host_key"):
                    raise BadHostKeyException("host key mismatch")
                if last.get("fail_ssh"):
                    raise SSHException(last["fail_ssh"])
                self.kwargs = kwargs

            def open_sftp(self):
                return FakeSftpHandle()

            def close(self):
                pass

        module.SSHClient = SSHClient
        return module, last


class TestConnect(_ParamikoFixture):
    def test_password_auth_kwargs(self):
        module, last = self._fake_paramiko()
        source = flat_source()
        with mock.patch.object(sftp_mod, "paramiko", module):
            ssh, sftp = source._connect()
        kwargs = last["client"].kwargs
        self.assertEqual(kwargs["password"], "pw")
        self.assertFalse(kwargs["look_for_keys"])
        self.assertFalse(kwargs["allow_agent"])
        self.assertEqual(kwargs["port"], 22)
        self.assertEqual(sftp.channel.timeout, 600)
        del ssh

    def test_key_auth_kwargs(self):
        module, last = self._fake_paramiko()
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "some_key"
            key_file.write_text("dummy", encoding="utf-8")
            source = flat_source(
                sftp={"auth": {"type": "key", "key_file": str(key_file), "passphrase": "pp"}},
            )
            with mock.patch.object(sftp_mod, "paramiko", module):
                source._connect()
        kwargs = last["client"].kwargs
        self.assertEqual(kwargs["key_filename"], str(key_file))
        self.assertEqual(kwargs["passphrase"], "pp")

    def test_auth_failure_is_fatal(self):
        module, last = self._fake_paramiko()
        last["fail_auth"] = True
        source = flat_source(sftp={"retry_times": 3})
        with mock.patch.object(sftp_mod, "paramiko", module):
            with self.assertRaises(FatalSourceError) as ctx:
                source._connect()
        self.assertIn("认证失败", str(ctx.exception))

    def test_password_required_is_fatal(self):
        module, last = self._fake_paramiko()
        last["fail_passphrase"] = True
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "some_key"
            key_file.write_text("dummy", encoding="utf-8")
            source = flat_source(sftp={"auth": {"type": "key", "key_file": str(key_file)}})
            with mock.patch.object(sftp_mod, "paramiko", module):
                with self.assertRaises(FatalSourceError) as ctx:
                    source._connect()
        self.assertIn("口令", str(ctx.exception))

    def test_missing_key_file_is_fatal_without_retry(self):
        source = flat_source(sftp={"auth": {"type": "key", "key_file": "~/definitely/not/here_xyz"}})
        with mock.patch.object(sftp_mod, "paramiko", mock.MagicMock()):
            with self.assertRaises(FatalSourceError) as ctx:
                source._connect()
        self.assertIn("私钥文件不存在", str(ctx.exception))

    def test_channel_timeout_failure_is_logged(self):
        """get_channel 不可用时 io_timeout 会静默失效——必须留一条日志，别让配置项假装生效。"""
        module = self._fake_paramiko()[0]
        source = flat_source()
        logged = []
        with mock.patch.object(sftp_mod, "paramiko", module):
            with mock.patch.object(FakeSftpHandle, "get_channel", side_effect=AttributeError("no get_channel")):
                with mock.patch.object(sftp_mod, "log_once", logged.append):
                    source._connect()
        self.assertTrue(any("io_timeout" in str(line) for line in logged), logged)


class TestScanSafety(OfflineTestCase):
    """列目录的安全边界与软链大小语义。"""

    def test_remote_name_with_path_separator_rejected(self):
        """远端返回含路径分隔符 / .. 的名字必须拒绝：它会被拼进本地路径，逃出下载目录。

        修复前 download_dir / "../../x.csv" 由内核解析，远端内容能写到任意本地路径。
        """
        fake = FakeSftp()
        evil = "20260920/../../../PWNED.csv"
        fake.tree["/data"] = [FakeEntry(evil, 5)]
        fake.contents[f"/data/{evil}"] = b"pwned"
        source = flat_source(source={"file_regex": r"(?P<date>\d{8}).*"}, sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            with self.assertRaises(FatalSourceError) as ctx:
                source.list_files()
        self.assertIn("不合法", str(ctx.exception))

    def test_symlink_uses_target_size(self):
        """软链在 READDIR 里的 st_size 是链接自身长度；要用 stat() 取目标真实大小。

        否则下载后的大小核对必然失败（.part 留着、重试也没用），台账也永远对不上。
        """
        fake = FakeSftp()
        link = "/data/report_20260920.csv"
        fake.tree["/data"] = [FakeEntry("report_20260920.csv", size=len(link), is_link=True)]
        fake.contents[link] = b"a,b\n1,2\n"
        fake.link_targets[link] = len(fake.contents[link])
        source = flat_source(sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            item = source.list_files()["20260920"][0]
        self.assertEqual(item.size, len(b"a,b\n1,2\n"))

    def test_symlink_stat_failure_marks_size_unknown(self):
        """软链 stat 失败时按"大小未知"处理（None），不能退回链接长度——
        那会让下载成功后仍被判"大小不一致"，且报错指向错误的方向。"""
        fake = FakeSftp()
        link = "/data/report_20260920.csv"
        fake.tree["/data"] = [FakeEntry("report_20260920.csv", size=len(link), is_link=True)]
        # contents 与 link_targets 都不登记 → stat 抛 ENOENT
        source = flat_source(sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            item = source.list_files()["20260920"][0]
        self.assertIsNone(item.size)
        self.assertEqual(item.size_text, "大小未知")

    def test_entry_without_size_is_unknown(self):
        """READDIR 未返回 st_size（paramiko 给 None）时不能当成 0 字节：下载后核对必然误报。"""
        fake = FakeSftp()
        entry = FakeEntry("report_20260920.csv", size=0)
        entry.st_size = None
        fake.tree["/data"] = [entry]
        fake.contents["/data/report_20260920.csv"] = b"a,b\n"
        source = flat_source(sftp={"retry_times": 0})
        with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
            item = source.list_files()["20260920"][0]
        self.assertIsNone(item.size)

    def test_download_skips_size_check_when_unknown(self):
        """大小未知时下载成功即通过（不做核对），不会因为拿不到基准把正常文件判失败。"""
        fake = FakeSftp()
        fake.contents["/data/report_20260920.csv"] = b"a,b\n1,2\n"
        source = flat_source(sftp={"retry_times": 0})
        item = sftp_mod.RemoteFile(
            date="20260920",
            name="report_20260920.csv",
            size=None,
            remote="/data/report_20260920.csv",
            ledger_key="report_20260920.csv",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sftp_mod.SftpSource, "_connect", connect_to(fake)):
                got = source.download(item, Path(tmp) / "a.csv")
            self.assertEqual(got.read_bytes(), b"a,b\n1,2\n")


class TestHostKeyPolicy(_ParamikoFixture):
    """主机指纹校验：默认严格（显式 RejectPolicy + 只认 known_hosts），
    sftp.host_key=auto_accept 显式降级为 AutoAddPolicy。"""

    def test_default_is_strict_load_system_host_keys(self):
        module, last = self._fake_paramiko()
        with mock.patch.object(sftp_mod, "paramiko", module):
            flat_source()._connect()
        self.assertTrue(last["client"].system_host_keys_loaded)
        # 显式 RejectPolicy：拒绝未知主机指纹，"严格"写死在代码里，不依赖 paramiko 的隐式默认策略
        self.assertIsInstance(last["client"].policy, module.RejectPolicy)

    def test_loads_user_known_hosts_when_present(self):
        module, last = self._fake_paramiko()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            known = home / ".ssh" / "known_hosts"
            known.parent.mkdir()
            known.write_text("host ssh-rsa AAAA\n", encoding="utf-8")
            with mock.patch.object(sftp_mod, "paramiko", module):
                with mock.patch.object(sftp_mod.Path, "home", return_value=home):
                    flat_source()._connect()
        self.assertEqual(last["client"].user_host_keys, [str(known)])

    def test_skips_user_known_hosts_when_missing(self):
        module, last = self._fake_paramiko()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(sftp_mod, "paramiko", module):
                with mock.patch.object(sftp_mod.Path, "home", return_value=home):
                    flat_source()._connect()
        self.assertFalse(getattr(last["client"], "user_host_keys", []))

    def test_auto_accept_uses_autoadd_policy(self):
        module, last = self._fake_paramiko()
        source = flat_source(sftp={"host_key": "auto_accept"})
        with mock.patch.object(sftp_mod, "paramiko", module):
            source._connect()
        self.assertIsInstance(last["client"].policy, module.AutoAddPolicy)
        self.assertFalse(getattr(last["client"], "system_host_keys_loaded", False))

    def test_unknown_host_hint_mentions_keyscan(self):
        """严格模式下未知主机的报错要带 ssh-keyscan 提示（否则用户不知道去哪登记指纹）。"""
        module, last = self._fake_paramiko()
        last["fail_ssh"] = "Server 'h' not found in known_hosts"
        with mock.patch.object(sftp_mod, "paramiko", module):
            with self.assertRaises(FatalSourceError) as ctx:
                flat_source()._connect()
        self.assertIn("ssh-keyscan", str(ctx.exception))

    def test_bad_host_key_is_fatal(self):
        """指纹不匹配是确定性安全错误，不能当瞬时故障重试。"""
        module, last = self._fake_paramiko()
        last["fail_bad_host_key"] = True
        with mock.patch.object(sftp_mod, "paramiko", module):
            with self.assertRaises(FatalSourceError) as ctx:
                flat_source()._connect()
        self.assertIn("指纹", str(ctx.exception))

    def test_auto_accept_error_has_no_keyscan_hint(self):
        """auto_accept 模式（显式不校验）下报错不应再提示 keyscan。"""
        module, last = self._fake_paramiko()
        last["fail_ssh"] = "Server 'h' not found in known_hosts"
        source = flat_source(sftp={"host_key": "auto_accept"})
        with mock.patch.object(sftp_mod, "paramiko", module):
            with self.assertRaises(FatalSourceError) as ctx:
                source._connect()
        self.assertNotIn("ssh-keyscan", str(ctx.exception))


class TestLocalPathWithin(OfflineTestCase):
    """本地落地路径必须在下载目录内：挡 Windows 盘符相对名，但不误伤合法的冒号文件名。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "download"
        self.base.mkdir()

    def test_plain_and_nested_within(self):
        # 两侧都 resolve 后比较：macOS 上临时目录在 /var（符号链接）下，未规范化的路径不相等
        self.assertEqual(
            sftp_mod.local_path_within(self.base, "a.csv", "a.csv").resolve(),
            (self.base / "a.csv").resolve(),
        )
        self.assertEqual(
            sftp_mod.local_path_within(self.base, "20260920/a.csv", "a.csv").resolve(),
            (self.base / "20260920" / "a.csv").resolve(),
        )

    def test_colon_name_is_allowed(self):
        """远端是 POSIX，文件名带 ":" 合法：不能因为冒号就拒绝（Windows 上它仍在下载目录内）。"""
        key = "report:20260920.csv"
        self.assertEqual(sftp_mod.local_path_within(self.base, key, key).resolve(), (self.base / key).resolve())

    def test_parent_traversal_rejected(self):
        with self.assertRaises(FatalSourceError):
            sftp_mod.local_path_within(self.base, "../escape.csv", "escape.csv")

    def test_windows_drive_relative_rejected_on_all_platforms(self):
        """盘符相对名（"Z:xxx"/"a:b.csv"）在 NT 上会跳出下载目录；判定显式化后任何平台都拒绝
        （安全关键路径不因平台被跳过，跨平台行为一致）。"""
        for key in ("Z:20260920.csv", "a:b.csv"):
            with self.assertRaises(FatalSourceError):
                sftp_mod.local_path_within(self.base, key, key)


if __name__ == "__main__":
    unittest.main()
