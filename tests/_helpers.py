# -*- coding: utf-8 -*-
"""测试公共设施：假 SFTP / 假 MaxCompute，全部用例离线可跑（不连网络、不连数仓）。"""

from __future__ import annotations

import argparse
import csv
import errno
import io
import stat
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


def make_args(**overrides):
    """cli 参数默认值（与 build_parser 对齐，测试要什么改什么）。"""
    base = dict(
        job="jobs/x.json",
        config="",
        init=False,
        init_out="",
        check=False,
        bizdate="",
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
        self.tree = dict(tree or {})
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
        return self.tree[path]

    def stat(self, path):
        """跟随软链的 stat（paramiko SFTPClient.stat 的语义，对应 lstat 版是 listdir_attr）。"""
        if path in self.link_targets:
            return FakeEntry(path.rpartition("/")[2], self.link_targets[path])
        if path in self.contents:
            return FakeEntry(path.rpartition("/")[2], len(self.contents[path]))
        raise OSError(errno.ENOENT, "No such file", path)

    def get(self, remote, local):
        self.get_calls += 1
        if self.fail_downloads > 0:
            self.fail_downloads -= 1
            raise OSError("simulated download failure")
        Path(local).write_bytes(self.contents[remote])
        self.downloaded.append(remote)

    def close(self):
        pass

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


def add_file(fake: FakeSftp, path: str, data: bytes) -> FakeSftp:
    """加一个远端文件：父目录条目 + 文件条目 + 内容。"""
    parent, _, name = path.rpartition("/")
    fake.tree.setdefault(parent or ".", []).append(FakeEntry(name, len(data)))
    fake.contents[path] = data
    return fake


def add_dir(fake: FakeSftp, path: str) -> FakeSftp:
    """加一个远端目录条目。"""
    parent, _, name = path.rpartition("/")
    fake.tree.setdefault(parent or ".", []).append(FakeEntry(name, 0, is_dir=True))
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
    def __init__(self, table, partition, reopen):
        self.table = table
        self.partition = partition
        self.reopen = reopen

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def write(self, rows):
        self.table.written.setdefault(self.partition, []).extend([list(row) for row in rows])


class FakeTable:
    """假表：记录删/建分区与写入内容；table_schema 供结构校验。"""

    def __init__(self, columns=None, partitions=(("pt", "string"),)):
        self.written: dict[str, list] = {}
        self.deleted: list[str] = []
        self.created: list[str] = []
        self.writers: list[FakeWriter] = []
        self.is_virtual_view = False
        self.is_materialized_view = False
        self.is_transactional = False
        self.table_schema = FakeTableSchema(columns or [], partitions)

    def delete_partition(self, spec, if_exists=False):
        self.deleted.append(spec)
        # 先删再填：删掉后旧数据不再存在（count(*) 也应反映这一点）
        if spec.startswith("pt="):
            self.written.pop(spec, None)

    def create_partition(self, spec, if_not_exists=False):
        self.created.append(spec)

    def open_writer(self, partition=None, reopen=False):
        writer = FakeWriter(self, partition, reopen)
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
        self.stopped = True

    def open_reader(self):
        return FakeReader(self.rows)


class FakeOdps:
    """假 ODPS 客户端：run_sql 的 count(*) 从已写入内容现算。"""

    def __init__(self, table: FakeTable, table_name: str = "ods_demo_di"):
        self.table = table
        self.table_name = table_name
        self.sql: list[str] = []
        self.fail_writes = 0

    def exist_table(self, name):
        return name == self.table_name

    def get_table(self, name):
        if name != self.table_name:
            raise AssertionError(f"unexpected table: {name}")
        return self.table

    def run_sql(self, sql):
        self.sql.append(sql)
        if "count(*)" in sql:
            marker = "pt = '"
            start = sql.find(marker)
            end = sql.find("'", start + len(marker)) if start >= 0 else -1
            pt = sql[start + len(marker) : end] if start >= 0 and end > 0 else ""
            rows = self.table.written.get(f"pt={pt}", [])
            return FakeInstance(rows=[{"cnt": len(rows)}])
        return FakeInstance()
