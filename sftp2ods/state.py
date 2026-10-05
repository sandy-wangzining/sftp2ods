# -*- coding: utf-8 -*-
"""上传台账：记录每个文件写到了哪个表/分区/大小/行数，重跑靠它跳过已上传的。

台账就是下载目录里的 .uploaded.json（与两个结算脚本同款、同字段），
两个工具可以共用一份台账（旧脚本已经写过的日期，迁移后不用重导）。
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from .utils import interprocess_lock, log

STATE_FILE_NAME = ".uploaded.json"


def load_state(path: Path) -> dict:
    """读台账；文件不存在返回空；内容坏了给出明确报错（不静默丢弃）。"""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        # UnicodeDecodeError（编码损坏）是 ValueError 子类，与读失败同一出口
        raise SystemExit(
            f"上传台账读不了：{path}（{exc}）；确认文件损坏可改名或删除，重跑会按本地文件重新上传（先删再填，幂等）"
        ) from exc
    if not content.strip():
        # 0 字节/纯空白：崩溃丢数据的典型形态（数据块还在页缓存时被断电/SIGKILL）。
        # 台账只是派生数据，按"没有台账"继续——一次崩溃不该让后续每次运行都硬失败
        log(f"  警告：上传台账是空文件（{path}）：按没有台账继续；重跑会按本地文件重新上传（先删再填，幂等）")
        return {}
    try:
        data = json.loads(content)
    except ValueError as exc:
        # JSONDecodeError 与 UnicodeDecodeError（文件被截断/编码损坏）都是 ValueError 子类；
        # 只接 JSONDecodeError 会让损坏的台账以裸 traceback 冒出来，而不是这句明确报错
        raise SystemExit(
            f"上传台账读不了：{path}（{exc}）；确认文件损坏可改名或删除，重跑会按本地文件重新上传（先删再填，幂等）"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(f"上传台账格式不对（顶层应为对象）：{path}")
    return data


def save_state(path: Path, state: dict) -> None:
    """写台账（进程间锁 + 与磁盘合并 + 临时文件原子替换）。

    两个工具可能共用一份台账：只做原子替换挡不住丢更新（A 读完后 B 写入的记录会被 A 整文件覆盖）。
    因此在 load-modify-save 外包一层跨平台文件锁（POSIX flock / Windows msvcrt），
    持锁期间重新读盘再合并本次记录。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(path.with_name(path.name + ".lock")):
        merged = load_state(path)
        merged.update(state)
        _write_state_unlocked(path, merged)


def _write_state_unlocked(path: Path, state: dict) -> None:
    """写台账（先写临时文件、fsync 落盘再原子替换，半截/空文件不会覆盖好台账）。调用方须已持锁。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        # 只 write_text 不 fsync 的话，断电/SIGKILL 后可能留下 0 字节台账（数据块还在
        # 页缓存，rename 已被日志记下）——虽然 load_state 现在能容忍空文件，仍应落到盘上
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def record_of(state: dict, keys, size: int, table: str, pt: str, project: str | None = None) -> dict | None:
    """台账里是否存在"与本次一致"的完成记录（keys 任一命中即可，兼容旧台账的键）。

    keys 里放当前键与历史键（如 date_dir 布局的 "20260920/xxx.csv" 与旧的 "xxx.csv"）。
    记录里带了 project 时也要一致：换过目标项目（如 dev → prod，表名相同）时不能拿
    另一个项目里的上传记录跳过——否则会把整段日期静默跳成"没数据"。
    旧脚本的台账没有 project 字段，按兼容处理（不因缺字段拒绝）。
    返回命中的记录 dict（调用方可能需要里面的 md5），没有返回 None。
    """
    for key in keys:
        record = state.get(key)
        if not isinstance(record, dict):
            continue
        if record.get("table") != table or record.get("pt") != pt:
            continue
        if size is not None and _as_int(record.get("size")) != size:
            # 大小未知（远端没给）时只要求其余字段一致，与 local_ready 的 None 语义对齐；
            # 旧台账里的 size 可能是字符串，统一按 int 归一化
            continue
        recorded_project = record.get("project")
        if recorded_project and recorded_project != project:
            # 记录带了 project 就必须与本次一致；本次没传（None）同样按不一致处理——
            # "不传"不等于"任意项目都算已上传"，否则换过目标项目（dev→prod、迁移后
            # 某条调用路径没带 project）会拿旧项目的记录跳过，整段日期静默变"没数据"
            continue
        return record
    return None


def _as_int(value):
    """台账字段的整数归一化（旧脚本可能把 size 写成字符串）；认不出返回 None。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def md5_of(path: Path) -> str:
    """文件的 md5 十六进制串（流式计算，几十 MB 也只用一次读 IO）；文件不存在/读不了返回空串。"""
    import hashlib

    digest = hashlib.md5()  # noqa: S324 - 这里只做完整性校验（本地文件 vs 台账），不涉及密码学场景
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def local_ready(target: Path, size: int | None, md5: str = "") -> bool:
    """本地文件已存在且大小与远端一致（不用重新下载）；md5 给定时（台账里有）再校验内容。

    旧台账没有 md5 字段：md5 传空串，退回只比大小（兼容迁移前的记录）。
    size=None（远端没给大小）时只要求文件存在：拿不到基准就没法比，重下也解决不了。
    """
    target = Path(target)
    # 一次 stat 完成"存在 + 类型 + 大小"：is_file() 与 stat() 之间存在 TOCTOU
    # （文件被并发清理/替换时会抛裸 FileNotFoundError，而不是走"需重新下载"分支）
    try:
        info = target.stat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if size is not None and info.st_size != size:
        return False
    if md5:
        return md5_of(target) == md5
    return True


def log_skip(date: str, files: list) -> None:
    """跳过整个日期时打一条（按天粒度，不刷屏）。"""
    log(f"{date}：{len(files)} 个文件已上传过（大小与台账一致），跳过")
