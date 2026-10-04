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
    def test_file_regex_requires_date_group(self):
        """file_regex 缺 (?P<date>) 时必须在配置阶段报错（否则扫描时抛 IndexError，还会被重试）。"""
        with self.assertRaises(SystemExit) as ctx:
            flat_source(source={"file_regex": r"report_\d{8}\.csv"})
        self.assertIn("(?P<date>", str(ctx.exception))

    def test_date_dir_regex_requires_date_group(self):
        with self.assertRaises(SystemExit) as ctx:
            flat_source(
                source={
                    "root": "/d",
                    "layout": "date_dir",
                    "file_regex": r"a\.csv",
                    "date_dir_regex": r"\d{8}",
                }
            )
        self.assertIn("(?P<date>", str(ctx.exception))


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

    def test_subdir_listing_failure_skips(self):
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
        # 远端列表说 10 字节，实际内容 8 字节 → 拒绝改名
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


class TestConnect(OfflineTestCase):
    def _fake_paramiko(self):
        module = mock.MagicMock(name="paramiko")

        class AuthenticationException(Exception):
            pass

        class SSHException(Exception):
            pass

        class PasswordRequiredException(SSHException):
            pass

        class AutoAddPolicy:
            pass

        class RejectPolicy:
            pass

        module.AuthenticationException = AuthenticationException
        module.SSHException = SSHException
        module.PasswordRequiredException = PasswordRequiredException
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

            def connect(self, **kwargs):
                if last.get("fail_auth"):
                    raise AuthenticationException("bad password")
                if last.get("fail_passphrase"):
                    raise PasswordRequiredException("private key file is encrypted")
                if last.get("fail_ssh"):
                    raise SSHException(last["fail_ssh"])
                self.kwargs = kwargs

            def open_sftp(self):
                return FakeSftpHandle()

            def close(self):
                pass

        module.SSHClient = SSHClient
        return module, last

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


class TestHostKeyPolicy(TestConnect):
    """主机指纹校验：默认严格（显式 RejectPolicy + 只认 known_hosts），
    sftp.host_key=auto_accept 显式降级为 AutoAddPolicy。"""

    def test_default_is_strict_load_system_host_keys(self):
        module, last = self._fake_paramiko()
        with mock.patch.object(sftp_mod, "paramiko", module):
            flat_source()._connect()
        self.assertTrue(last["client"].system_host_keys_loaded)
        # 显式 RejectPolicy：拒绝未知主机指纹，"严格"写死在代码里，不依赖 paramiko 的隐式默认策略
        self.assertIsInstance(last["client"].policy, module.RejectPolicy)

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
            with self.assertRaises(RuntimeError) as ctx:
                flat_source()._connect()
        self.assertIn("ssh-keyscan", str(ctx.exception))

    def test_auto_accept_error_has_no_keyscan_hint(self):
        """auto_accept 模式（显式不校验）下报错不应再提示 keyscan。"""
        module, last = self._fake_paramiko()
        last["fail_ssh"] = "Server 'h' not found in known_hosts"
        source = flat_source(sftp={"host_key": "auto_accept"})
        with mock.patch.object(sftp_mod, "paramiko", module):
            with self.assertRaises(RuntimeError) as ctx:
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
        self.assertEqual(sftp_mod.local_path_within(self.base, "a.csv", "a.csv"), self.base / "a.csv")
        self.assertEqual(
            sftp_mod.local_path_within(self.base, "20260920/a.csv", "a.csv"),
            self.base / "20260920" / "a.csv",
        )

    def test_colon_name_is_allowed(self):
        """远端是 POSIX，文件名带 ":" 合法：不能因为冒号就拒绝（Windows 上它仍在下载目录内）。"""
        key = "report:20260920.csv"
        self.assertEqual(sftp_mod.local_path_within(self.base, key, key), self.base / key)

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
