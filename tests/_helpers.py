# -*- coding: utf-8 -*-
"""测试公共设施：假 SFTP / 假 MaxCompute，全部用例离线可跑（不连网络、不连数仓）。"""

from __future__ import annotations

import argparse
import copy
import csv
import errno
import io
import re
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sftp2ods.utils import setup_console


class OfflineTestCase(unittest.TestCase):
    """所有用例的基类：禁止真实 sleep（把 time.sleep 变成空操作），并统一控制台编码。"""

    def setUp(self):
        setup_console()
        patcher = mock.patch.object(time, "sleep", lambda *_args, **_kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 运行锁默认落在仓库根的 .run-locks/：单测用的临时作业路径每次哈希都不同，
        # 锁文件会无限累积（生产行为不变，这里只把锁根目录重定向到临时目录并在收尾清理）
        from sftp2ods import cli as cli_mod

        lock_root = tempfile.mkdtemp(prefix="sftp2ods-test-locks-")
        self.addCleanup(shutil.rmtree, lock_root, ignore_errors=True)
        lock_patcher = mock.patch.object(cli_mod, "ROOT", Path(lock_root))
        lock_patcher.start()
        self.addCleanup(lock_patcher.stop)


def make_args(**overrides):
    """cli 参数默认值（与 build_parser 对齐，测试要什么改什么）。"""
    base = dict(
        job="jobs/x.json",
        config="",
        init=False,
        init_out="",
        check=False,
        bizdate=None,  # argparse 默认：None = 没传；空串 = 显式传空（_check_cli_args 按参数错报 2）
        start_date="",
        end_date="",
        force=False,
        dry_run=False,
        no_notify=False,
        endpoint="",
        mc_profile="",
        cli_profile="",
        sql_timeout=600,
        log_file="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def minimal_job(**overrides) -> dict:
    """一个能通过校验的最小作业配置（flat 布局、两列）。"""
    job = {
        "job": "demo",
        "maxcompute": {"project": "demo_project"},
        "sftp": {
            "host": "sftp.example.com",
            "port": 22,
            "username": "user1",
            "auth": {"type": "password", "password": "secret-pw"},
        },
        "source": {"root": "/data", "layout": "flat", "file_regex": "report_(?P<date>\\d{8})\\.csv"},
        "parse": {
            "encoding": "utf-8-sig",
            "delimiter": "auto",
            "columns": [
                {"header": "Order ID", "name": "order_id", "type": "string"},
                {"header": "Settlement amount", "name": "settlement_amount", "type": "decimal(19,10)"},
            ],
        },
        "target": {"project": "demo_project", "table": "ods_demo_di"},
    }
    for key, value in overrides.items():
        job[key] = value
    return job


# =============================================================================
# 假 SFTP
# =============================================================================


class FakeEntry:
    """listdir_attr 的元素（paramiko SFTPAttributes 的最小替身）。"""

    def __init__(self, filename: str, size: int = 0, is_dir: bool = False, is_link: bool = False):
        self.filename = filename
        self.st_size = size
        if is_dir:
            self.st_mode = stat.S_IFDIR | 0o755
        elif is_link:
            self.st_mode = stat.S_IFLNK | 0o777
        else:
            self.st_mode = stat.S_IFREG | 0o644


class FakeSftp:
    """假 SFTP 会话：tree = {目录: [FakeEntry]}，contents = {远端路径: bytes}。"""

    def __init__(self, tree=None, contents=None):
        # 目录条目逐项复制到新对象：只 list() 浅拷贝的话 FakeEntry 仍与调用方共享，
        # replace_file/就地改 st_size 会写穿到调用方的数据结构（多个用例复用时互相污染）
        self.tree = {key: [copy.copy(entry) for entry in value] for key, value in (tree or {}).items()}
        self.contents = dict(contents or {})
        self.link_targets = {}  # 远端路径 -> 目标文件大小（stat 跟随软链后的真实大小）
        self.fail_downloads = 0  # 前 N 次下载失败（模拟网络抖动）
        self.fail_lists = 0  # 前 N 次列目录失败
        self.downloaded = []
        self.get_calls = 0

    def listdir_attr(self, path):
        if self.fail_lists > 0:
            self.fail_lists -= 1
            # 不带 errno：模拟网络/权限类失败（sftp2ods 必须把它抛出去重试，不能当"空目录"）
            raise OSError("simulated list failure")
        if path not in self.tree:
            # 与 paramiko 的真实行为一致：目录不存在是 ENOENT（只有这种才当"空目录"）
            raise OSError(errno.ENOENT, "No such file", path)
        return list(self.tree[path])  # 返回副本：被测代码就地 sort/append 不该污染假 SFTP

    def stat(self, path):
        """跟随软链的 stat（paramiko SFTPClient.stat 的语义，对应 lstat 版是 listdir_attr）。"""
        if path in self.link_targets:
            return FakeEntry(path.rpartition("/")[2], self.link_targets[path])
        if path in self.contents:
            return FakeEntry(path.rpartition("/")[2], len(self.contents[path]))
        if path in self.tree:
            # 已登记的目录也存在：对目录一律抛 ENOENT 与 paramiko 语义不符，
            # 会让"对目录做 stat 存在性判断"的代码在测试里走"文件不存在"分支
            return FakeEntry(path.rpartition("/")[2] or ".", 0, is_dir=True)
        raise OSError(errno.ENOENT, "No such file", path)

    def get(self, remote, local):
        self.get_calls += 1
        if self.fail_downloads > 0:
            self.fail_downloads -= 1
            raise OSError("simulated download failure")
        if remote not in self.contents:
            # 未登记路径按真实 paramiko 语义抛 OSError/FileNotFoundError：KeyError 不是
            # OSError，被测代码的 except OSError 分支不会命中，用例会以误导性原因失败
            raise OSError(errno.ENOENT, "No such file", remote)
        Path(local).write_bytes(self.contents[remote])
        self.downloaded.append(remote)

    def close(self):
        pass

    def replace_file(self, path: str, data: bytes) -> None:
        """替换已登记远端文件的内容并同步目录条目大小（模拟源方重写了文件）。

        用例不要自己去翻 tree/contents 内部结构：路径或目录名变了会静默匹配不到、
        依赖"大小变化"的断言以无关原因失败/空转。未登记的文件直接报错。
        """
        if path not in self.contents:
            raise AssertionError(f"replace_file: 未登记的远端文件 {path}")
        self.contents[path] = data
        name = path.rpartition("/")[2]
        for entry in self.tree.get(_parent_dir(path), []):
            if entry.filename == name:
                entry.st_size = len(data)
                return
        raise AssertionError(f"replace_file: 目录里找不到 {path} 的条目")

    def get_channel(self):
        return None


class FakeSsh:
    def close(self):
        pass


def connect_to(sftp):
    """给 SftpSource._connect 用的替身工厂。"""

    def _connect(self):
        return FakeSsh(), sftp

    return _connect


def _parent_dir(path: str) -> str:
    """path 的父目录键：绝对路径（"/a.csv"）的父目录是 "/" 而不是 "."，
    否则 listdir_attr("/") 在树里找不到、被伪装成"空目录/目录不存在"。"""
    parent, _, _name = path.rpartition("/")
    if parent:
        return parent
    return "/" if path.startswith("/") else "."


def add_file(fake: FakeSftp, path: str, data: bytes) -> FakeSftp:
    """加一个远端文件：父目录条目 + 文件条目 + 内容。"""
    parent, _, name = path.rpartition("/")
    fake.tree.setdefault(_parent_dir(path), []).append(FakeEntry(name, len(data)))
    fake.contents[path] = data
    return fake


def add_dir(fake: FakeSftp, path: str) -> FakeSftp:
    """加一个远端目录条目。"""
    parent, _, name = path.rpartition("/")
    fake.tree.setdefault(_parent_dir(path), []).append(FakeEntry(name, 0, is_dir=True))
    fake.tree.setdefault(path, [])
    return fake


def csv_bytes(headers, rows, delimiter=",", encoding="utf-8-sig") -> bytes:
    """生成 CSV 文件内容（默认 UTF-8 with BOM，与源文件一致）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode(encoding)


# =============================================================================
# 假 MaxCompute
# =============================================================================


class FakeSchemaColumn:
    def __init__(self, name, type_text):
        self.name = name
        self.type = type_text


class FakeTableSchema:
    def __init__(self, columns, partitions):
        self.columns = [FakeSchemaColumn(n, t) for n, t in columns]
        self.partitions = [FakeSchemaColumn(n, t) for n, t in partitions]


class FakeWriter:
    """Tunnel 写入会话替身：写入先攒在会话里、with 正常退出时才提交到表。

    reopen 语义与 pyodps 对齐（open_writer(reopen=True) 会开一个**新**上传会话，
    reopen=False 复用上次会话）：上一次失败会话残留的块在 reopen=False 时会与
    本轮数据一起提交——生产代码漏传 reopen=True 导致的"数据翻倍"由此可被测试发现。
    """

    def __init__(self, table, partition, reopen, blocks):
        self.table = table
        self.partition = partition
        self.reopen = reopen
        self.blocks = blocks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_info, exc_tb):
        if exc_type is None:
            # 会话提交：失败会话里残留的块（reopen=False 复用时）也在 blocks 里
            self.table.written.setdefault(self.partition, []).extend(self.blocks)
            self.table.sessions.pop(self.partition, None)
        else:
            # 会话中断：已上传的块留在服务端会话里（下一次 open_writer 可能复用）
            self.table.sessions[self.partition] = self.blocks
        return False

    def write(self, rows):
        self.blocks.extend([list(row) for row in rows])


class FakeTable:
    """假表：记录删/建分区与写入内容；table_schema 供结构校验。"""

    def __init__(self, columns=None, partitions=(("pt", "string"),)):
        self.written: dict[str, list] = {}
        self.sessions: dict[str, list] = {}  # 未提交（失败会话残留）的块
        self.deleted: list[str] = []
        self.created: list[str] = []
        self.delete_if_exists: list[bool] = []  # 记录 flag：生产代码漏传 True 时"删不存在的分区"会真报错
        self.create_if_not_exists: list[bool] = []
        self.writers: list[FakeWriter] = []
        self.is_virtual_view = False
        self.is_materialized_view = False
        self.is_transactional = False
        self.table_schema = FakeTableSchema(columns or [], partitions)

    def delete_partition(self, spec, if_exists=False):
        self.deleted.append(spec)
        self.delete_if_exists.append(if_exists)
        # 先删再填：删掉后旧数据不再存在（count(*) 也应反映这一点）。
        # 注意不动 sessions：服务端的上传会话不随分区删除消失，这正是重试必须
        # reopen=True（开新会话）的原因
        if spec.startswith("pt="):
            self.written.pop(spec, None)

    def create_partition(self, spec, if_not_exists=False):
        self.created.append(spec)
        self.create_if_not_exists.append(if_not_exists)

    def open_writer(self, partition=None, reopen=False):
        blocks = [] if reopen else list(self.sessions.get(partition, []))
        writer = FakeWriter(self, partition, reopen, blocks)
        self.writers.append(writer)
        return writer

    def rows_in(self, pt: str) -> list:
        return self.written.get(f"pt={pt}", [])


class FakeReader:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        return iter(self.rows)


class FakeInstance:
    def __init__(self, rows=None, success=True, terminated=False):
        self.rows = rows or []
        self._success = success
        self._terminated = terminated
        self.stopped = False

    def is_successful(self):
        return self._success

    def is_terminated(self):
        return self._terminated

    def wait_for_success(self, timeout=None):
        return None

    def stop(self):
        # 真实实例 stop 后进入"已终止"：否则 stop 后轮询 is_terminated() 的代码在测试里
        # 永不退出（OfflineTestCase 把 sleep 变成空操作，死循环不会被超时打断）
        self.stopped = True
        self._terminated = True

    def open_reader(self):
        return FakeReader(self.rows)


class FakeOdps:
    """假 ODPS 客户端：count(*) 从已写入内容现算，分区增删的 DDL 同步到 FakeTable。"""

    def __init__(self, table: FakeTable, table_name: str = "ods_demo_di"):
        self.table = table
        self.table_name = table_name
        self.sql: list[str] = []

    def exist_table(self, name):
        return str(name).rsplit(".", 1)[-1] == self.table_name

    def get_table(self, name):
        # 生产代码用全限定名（project.table）取表：核对时剥掉 project 前缀，
        # 保留"点名错就报错"的守护作用
        short = str(name).rsplit(".", 1)[-1]
        if short != self.table_name:
            raise AssertionError(f"unexpected table: {name}")
        return self.table

    def run_sql(self, sql):
        self.sql.append(sql)
        # 大小写/空格容错：判定别绑死 "count(*)" 的字面写法
        if "count(" in sql.lower().replace(" ", ""):
            # 正则容错空格/大小写：判定里已经 replace(" ","") + lower()，解析也按同口径，
            # 否则 `pt='x'`（无空格）这类合法 SQL 会以误导性的 AssertionError 失败
            match = re.search(r"pt\s*=\s*'([^']*)'", sql, re.I)
            if match is None:
                # 解析不出来时静默返回 0 会把"SQL 形态变了"伪装成"数据没写进去"，
                # 断言会以误导性的形式失败——显式炸出来
                raise AssertionError(f"fake 无法解析 count 语句里的分区值：{sql!r}")
            pt = match.group(1)
            rows = self.table.written.get(f"pt={pt}", [])
            return FakeInstance(rows=[{"cnt": len(rows)}])
        # 分区增删走带超时的 DDL（生产不再用 pyodps 的 delete/create_partition）：
        # 替身按同样的语义更新 FakeTable（deleted/created/if_exists/if_not_exists + 内容），
        # 这样"测试里的 table.deleted/created 断言"仍能反映真实行为
        match = re.search(
            r"(drop\s+if\s+exists|add\s+if\s+not\s+exists)\s+partition\s*\(\s*pt\s*=\s*'([^']*)'\s*\)",
            sql,
            re.I,
        )
        if match:
            spec = f"pt={match.group(2)}"
            # 直接更新 FakeTable 的状态（不调用它的 delete_partition/create_partition）：
            # 那两个方法在测试里被用来**证明**生产代码没再走 pyodps 表 API
            if match.group(1).lower().startswith("drop"):
                self.table.deleted.append(spec)
                self.table.delete_if_exists.append(True)
                self.table.written.pop(spec, None)
            else:
                self.table.created.append(spec)
                self.table.create_if_not_exists.append(True)
        elif "partition" in sql.lower() and ("drop" in sql.lower() or "add" in sql.lower()):
            # 像分区增删 DDL 却解析不出来（生产换写法/引号类型）：静默返回空实例会把
            # "fake 过时"伪装成"生产没删分区/没建分区"，用例以误导性原因失败——
            # 与上面对 count 分支的处理同口径，显式炸出来
            raise AssertionError(f"fake 无法解析分区 DDL：{sql!r}")
        return FakeInstance()
