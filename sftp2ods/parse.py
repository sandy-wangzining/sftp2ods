# -*- coding: utf-8 -*-
"""CSV/TSV 解析：列头映射、分隔符探测、类型转换、合计行识别与校验。

解析口径（宁失败勿写错）：
- 表头按"规范化列名"（trim、小写、连续空白压一个空格）映射到配置的列，不靠列序号；
- 缺必需列头直接报错；缺可选列头告警并按空入库；
- 数据行严格检查列数与表头一致（strict_columns，默认开），防列错位静默写错；
- 金额/整数列解析失败直接报错，绝不当 0；空值一律存 NULL（None）；
- 可选：合计行（首列为空）不写库，且校验其金额 = 数据行之和（对不上说明口径变了，报错）。

注意：本模块只做"读文件 → 行值"，不碰网络与 MaxCompute，全部可离线测试。
"""

from __future__ import annotations

import codecs
import csv
import decimal
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .utils import ConfigError, as_bool, log

# CSV 单字段默认只允许 128KB，报表类文件很容易超；调到接近 MaxCompute 单列上限
_CSV_FIELD_LIMIT = 7_000_000
try:
    csv.field_size_limit(_CSV_FIELD_LIMIT)
except OverflowError:  # pragma: no cover - 32 位平台上 C long 装不下
    csv.field_size_limit(10**7)

# 分隔符白名单：自动探测只在这几个里选；显式指定时只允许单个字符
KNOWN_DELIMITERS = ("\t", ",", ";")
_DECIMAL_RE = re.compile(r"\Adecimal\s*\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\Z")
_SIMPLE_TYPES = ("string", "bigint", "double")
MAX_DECIMAL_PRECISION = 38  # MaxCompute decimal 精度上限

# 配置白名单（config.collect_warnings 用；放在这里与解析字段定义同处维护）
PARSE_KEYS = {
    "encoding",
    "delimiter",
    "columns",
    "on_missing_header",
    "skip_if_empty",
    "footer",
    "strict_columns",
    "empty_as",
}
COLUMN_KEYS = {"header", "name", "type", "comment", "required"}


def norm(text: str) -> str:
    """规范化列头 / 单元格：trim、小写、连续空白压成一个空格。"""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def normalize_type(value) -> str:
    """类型名归一化：'DECIMAL(19, 10)' → 'decimal(19,10)'；其余小写去空白。"""
    return re.sub(r"\s+", "", str(value or "")).lower()


def kind_of(type_text: str) -> str:
    """类型 → 取值方式：str / dec / int / float（配置校验通过后调用）。"""
    t = normalize_type(type_text)
    if t == "string":
        return "str"
    if t == "bigint":
        return "int"
    if t == "double":
        return "float"
    return "dec"


def validate_type(value, where: str) -> str:
    """校验列类型：只允许 string / bigint / double / decimal(p,s)（拼进 DDL，白名单制）。"""
    t = normalize_type(value)
    if t in _SIMPLE_TYPES:
        return t
    match = _DECIMAL_RE.fullmatch(t)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        if not (1 <= precision <= MAX_DECIMAL_PRECISION):
            raise ConfigError(f"{where} decimal 精度应在 1~{MAX_DECIMAL_PRECISION}，实际 {value!r}")
        if not (0 <= scale <= precision):
            raise ConfigError(f"{where} decimal 小数位应在 0~{precision}，实际 {value!r}")
        return f"decimal({precision},{scale})"
    raise ConfigError(f"{where} 不支持的类型 {value!r}（可用 string / bigint / double / decimal(p,s)）")


@dataclass
class Column:
    """一列的定义：源文件列头 → 目标表列名 / 类型。

    required：True/False 显式指定；None = 跟随 parse.on_missing_header
    （error 模式下必需、warn 模式下可选），保证"缺列怎么处理"只有一个口径。
    """

    header: str
    name: str
    type: str
    kind: str
    comment: str = ""
    required: bool | None = None

    def ddl_type(self) -> str:
        return self.type

    def ddl_comment(self) -> str:
        """DDL 里的注释文本（单引号转义）。"""
        return self.comment.replace("'", "''")


def build_columns(parse_cfg: dict) -> list[Column]:
    """从 parse.columns 构建列定义（不做配置校验，校验见 validate_parse_config）。"""
    result = []
    for item in parse_cfg.get("columns") or []:
        item = item or {}
        type_text = validate_type(item.get("type"), f"parse.columns[{item.get('name')!r}].type")
        required = item.get("required")
        if required is not None:
            required = as_bool(required, default=True, field=f"parse.columns[{item.get('name')!r}].required")
        result.append(
            Column(
                header=str(item.get("header") or "").strip(),
                name=str(item.get("name") or "").strip(),
                type=type_text,
                kind=kind_of(type_text),
                comment=str(item.get("comment") or "").strip(),
                required=required,
            )
        )
    return result


def validate_parse_config(parse_cfg: dict) -> None:
    """校验 parse 块（列定义 / 编码 / 分隔符 / 可选列 / 合计行）。错误信息带字段路径。"""
    from .utils import require_identifier

    columns_raw = parse_cfg.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise ConfigError(
            'parse.columns 必须是非空数组（每项形如 {"header": "Order ID", "name": "order_id", "type": "string"}）'
        )
    seen_headers, seen_names = {}, {}
    columns = []
    for index, item in enumerate(columns_raw):
        where = f"parse.columns[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 必须是对象，实际 {type(item).__name__}")
        header = str(item.get("header") or "").strip()
        if not header:
            raise ConfigError(f"{where}.header 不能为空（源文件里的列标题）")
        name = item.get("name")
        if not name:
            raise ConfigError(f"{where}.name 不能为空（目标表列名）")
        name = require_identifier(name, f"{where}.name")
        validate_type(item.get("type"), f"{where}.type")
        if "required" in item:
            as_bool(item.get("required"), default=True, field=f"{where}.required")
        norm_header = norm(header)
        if norm_header in seen_headers:
            raise ConfigError(f"{where}.header 与 parse.columns[{seen_headers[norm_header]}] 重复：{header!r}")
        seen_headers[norm_header] = index
        if name.lower() in seen_names:
            raise ConfigError(f"{where}.name 与 parse.columns[{seen_names[name.lower()]}] 重复：{name!r}")
        seen_names[name.lower()] = index
        columns.append(Column(header, name, "", ""))

    on_missing = str(parse_cfg.get("on_missing_header") or "error").lower()
    if on_missing not in ("error", "warn"):
        raise ConfigError(f"parse.on_missing_header 不支持：{on_missing}（可用 error / warn）")

    encoding = str(parse_cfg.get("encoding") or "utf-8-sig")
    try:
        codecs.lookup(encoding)
    except LookupError:
        raise ConfigError(f"parse.encoding 不是有效的编码名：{encoding!r}（如 utf-8-sig / gbk）")

    delimiter = parse_cfg.get("delimiter")
    if delimiter is not None and str(delimiter).lower() != "auto":
        text = str(delimiter)
        if len(text) != 1:
            raise ConfigError(
                f"parse.delimiter 必须是单个字符或 auto，实际 {delimiter!r}"
                f'（TSV 请写 \\t 表示制表符，逗号写 ","；自动探测写 auto）'
            )
        if text not in KNOWN_DELIMITERS:
            raise ConfigError(
                f"parse.delimiter 不支持 {delimiter!r}（可用 auto / {', '.join(repr(d) for d in KNOWN_DELIMITERS)}）"
            )

    as_bool(parse_cfg.get("strict_columns"), default=True, field="parse.strict_columns")

    empty_as = str(parse_cfg.get("empty_as") or "null").lower()
    if empty_as not in ("null", "empty"):
        raise ConfigError(f"parse.empty_as 不支持：{empty_as!r}（可用 null：空串存 NULL / empty：空串原样存 ''）")

    skip_raw = parse_cfg.get("skip_if_empty")
    skip_names = []
    if skip_raw is not None:
        if isinstance(skip_raw, str):
            skip_raw = [skip_raw]
        if not isinstance(skip_raw, list) or not all(str(x or "").strip() for x in skip_raw):
            raise ConfigError('parse.skip_if_empty 应是列名数组（如 ["order_id"]），空项不允许')
        skip_names = [str(x).strip() for x in skip_raw]
        unknown = [name for name in skip_names if name.lower() not in seen_names]
        if unknown:
            raise ConfigError(f"parse.skip_if_empty 里的列不在 parse.columns 中：{'、'.join(unknown)}")

    footer = parse_cfg.get("footer")
    if footer is not None:
        if not isinstance(footer, dict):
            raise ConfigError('parse.footer 必须是对象（如 {"sum": ["settlement_amount"]}）')
        unknown_keys = [key for key in footer if key != "sum" and not str(key).startswith(("//", "#"))]
        if unknown_keys:
            raise ConfigError(f"parse.footer 不支持的字段：{'、'.join(map(str, unknown_keys))}（当前仅 sum）")
        sums = footer.get("sum")
        if sums is not None:
            if not isinstance(sums, list) or not all(str(x or "").strip() for x in sums):
                raise ConfigError('parse.footer.sum 应是列名数组（如 ["net_settlement_amount"]）')
            for name in sums:
                name = str(name).strip()
                if name.lower() not in seen_names:
                    raise ConfigError(f"parse.footer.sum 里的列不在 parse.columns 中：{name}")
                # 合计核对把列值当数字相加：非 decimal 列（string 会 TypeError，double 会与 Decimal 混算报错）
                # 在配置阶段就拦住，别等解析到合计行才崩
                item = columns_raw[seen_names[name.lower()]]
                type_text = validate_type(item.get("type"), f"parse.columns[{item.get('name')!r}].type")
                if kind_of(type_text) != "dec":
                    raise ConfigError(
                        f"parse.footer.sum 的列 {name} 类型是 {item.get('type')!r}，"
                        f"合计核对只支持 decimal 列（string/double 等请从 sum 里去掉）"
                    )
    # 校验用不到构建结果，避免重复构造
    del columns


class ParseSpec:
    """一份解析方案（一次构建、多次使用）：列映射 + 行级规则。"""

    def __init__(self, parse_cfg: dict):
        self.columns = build_columns(parse_cfg)
        self.encoding = str(parse_cfg.get("encoding") or "utf-8-sig")
        self.delimiter = str(parse_cfg.get("delimiter") or "auto")
        self.on_missing_header = str(parse_cfg.get("on_missing_header") or "error").lower()
        self.strict_columns = as_bool(parse_cfg.get("strict_columns"), default=True, field="parse.strict_columns")
        # 空串的落库形态：null（默认，空串 → NULL）或 empty（空串原样存 ''，兼容旧 clink 表的既有口径）
        self.empty_as = str(parse_cfg.get("empty_as") or "null").lower()
        skip = parse_cfg.get("skip_if_empty") or []
        if isinstance(skip, str):
            skip = [skip]
        skip_names = {str(name).strip().lower() for name in skip}
        self.skip_indexes = [i for i, col in enumerate(self.columns) if col.name.lower() in skip_names]
        self.footer_enabled = parse_cfg.get("footer") is not None
        footer = parse_cfg.get("footer") or {}
        sum_names = [str(name).strip().lower() for name in (footer.get("sum") or [])]
        self.footer_sum_indexes = [i for i, col in enumerate(self.columns) if col.name.lower() in sum_names]

    # ---------------------------------------------------------------- 表头
    def map_header(self, header_row: list[str], filename: str) -> tuple[list[int], int]:
        """建「规范化列名 → 下标」映射 → 每列下标（缺失 = -1），顺带校验必需列头。"""
        hmap: dict[str, int] = {}
        for index, cell in enumerate(header_row):
            key = norm(cell)
            if not key:
                raise RuntimeError(f"{filename} 表头第 {index + 1} 列是空列名（行尾多了分隔符？），已中止")
            if key in hmap:
                raise RuntimeError(f"{filename} 表头有重复列名：{header_row[index]!r}，解析会丢列，已中止")
            hmap[key] = index
        pos: list[int] = []
        missing_required, missing_optional = [], []
        default_required = self.on_missing_header == "error"
        for col in self.columns:
            index = hmap.get(norm(col.header), -1)
            pos.append(index)
            if index < 0:
                required = col.required if col.required is not None else default_required
                (missing_required if required else missing_optional).append(col.header)
        if missing_required:
            raise RuntimeError(
                f"{filename} 缺少必需列头：{'、'.join(missing_required)}；"
                f'文件格式可能变了（可选列请在 parse.columns 里标 "required": false）'
            )
        if missing_optional:
            log(f"  [警告] {filename} 未匹配到列头：{'、'.join(missing_optional)}（这些列按空入库）")
        if self.footer_enabled and pos[0] < 0:
            raise RuntimeError(
                f"{filename} 缺少第一列（{self.columns[0].header!r}），无法识别合计行；"
                f"parse.footer 开启时第一列必须存在"
            )
        return pos, len(header_row)

    # ---------------------------------------------------------------- 取值
    def convert_row(self, row: list[str], pos: list[int], filename: str, row_no: int) -> list:
        """一行原始单元格 → 类型化值列表（空值 None；解析失败直接报错）。"""
        values: list = []
        for col, index in zip(self.columns, pos):
            raw = row[index].strip() if 0 <= index < len(row) else ""
            if col.kind == "str":
                values.append(raw if self.empty_as == "empty" else (raw or None))
                continue
            if not raw:
                values.append(None)  # 空金额/数字存 NULL，不硬转 0
                continue
            try:
                if col.kind == "dec":
                    value = decimal.Decimal(raw.replace(",", ""))
                    if not value.is_finite():  # NaN / Infinity 不是合法金额（写库后聚合全废）
                        raise ArithmeticError(f"非有限数 {value}")
                    values.append(value)
                elif col.kind == "int":
                    values.append(int(raw.replace(",", "")))
                else:  # float
                    value = float(raw.replace(",", ""))
                    if not math.isfinite(value):  # inf / nan 同理
                        raise ValueError(f"非有限数 {value}")
                    values.append(value)
            except (ValueError, ArithmeticError):
                raise RuntimeError(f"{filename} 第 {row_no} 行 {col.header} 不是合法的{_kind_cn(col.kind)}：{raw!r}")
        return values

    def row_skipped(self, values: list) -> bool:
        """命中 skip_if_empty 的行是否要跳过（任一指定列为空 → 跳过；'' 与 NULL 都算空）。"""
        return any(values[index] is None or values[index] == "" for index in self.skip_indexes)

    # ---------------------------------------------------------------- 主流程
    def iter_rows(self, path: Path, stats: dict | None = None):
        """逐行产出类型化值列表（表头校验、空行跳过、合计行识别与校验）。

        生成器被完整消费时做尾部校验（合计行位置与金额核对）；行数统计与写入两趟
        都会完整消费，验证因此会跑两遍（代价可忽略，换来写库前先拦下坏文件）。
        """
        stats = stats if stats is not None else {}
        stats.setdefault("skipped", 0)
        stats.setdefault("rows", 0)
        delimiter = self._delimiter_for(path)
        footer = None
        footer_row_no = last_data_row_no = None
        sums = [decimal.Decimal(0)] * len(self.footer_sum_indexes)
        header_pos = header_len = None
        content = False
        row_no = 0
        try:
            with path.open("r", encoding=self.encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter, strict=True)
                for row_no, row in enumerate(reader, start=1):
                    if not any(cell.strip() for cell in row):
                        continue  # 空行跳过
                    content = True
                    if header_pos is None:
                        header_pos, header_len = self.map_header(row, path.name)
                        continue
                    if len(row) > header_len:
                        raise RuntimeError(
                            f"{path.name} 第 {row_no} 行列数 {len(row)} 多于表头 {header_len}，字段会被截断，已中止"
                        )
                    if len(row) < header_len:
                        if self.strict_columns:
                            raise RuntimeError(
                                f"{path.name} 第 {row_no} 行列数 {len(row)} ≠ 表头 {header_len}；"
                                f"文件列结构可能变了（确认源文件没问题可改 parse.strict_columns=false 容忍短行）"
                            )
                        row = row + [""] * (header_len - len(row))
                    if self.footer_enabled and not row[header_pos[0]].strip():
                        # 首列为空 = 合计行，不入库（否则下游求和翻倍）
                        if footer is not None:
                            raise RuntimeError(
                                f"{path.name} 出现多行合计行（第 {footer_row_no}、{row_no} 行），格式可能变了"
                            )
                        footer, footer_row_no = row, row_no
                        continue
                    values = self.convert_row(row, header_pos, path.name, row_no)
                    if self.row_skipped(values):
                        stats["skipped"] += 1
                        continue
                    last_data_row_no = row_no
                    for k, index in enumerate(self.footer_sum_indexes):
                        sums[k] += values[index] or decimal.Decimal(0)
                    stats["rows"] += 1
                    yield values
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"{path.name} 不是 {self.encoding} 编码（读第 {row_no} 行时失败）：{exc}；"
                f"请在 parse.encoding 里指定源文件真实编码（如 gbk）"
            )
        except csv.Error as exc:
            raise RuntimeError(f"{path.name} 第 {row_no} 行 CSV 解析失败：{exc}；多半是引号未闭合/字段内含未转义的引号")
        if not content:
            log(f"  警告：{path.name} 是空文件（没有任何行）")
            return
        if header_pos is None:  # pragma: no cover - content=True 时不可能走到
            raise RuntimeError(f"{path.name} 没有可解析的表头行")
        if footer is not None:
            if last_data_row_no is not None and footer_row_no < last_data_row_no:
                raise RuntimeError(f"{path.name} 合计行不在数据行之后（第 {footer_row_no} 行），格式可能变了")
            if self.footer_sum_indexes:
                got = []
                for index in self.footer_sum_indexes:
                    raw = footer[index].strip() if index < len(footer) else ""
                    try:
                        value = decimal.Decimal(raw.replace(",", "")) if raw else decimal.Decimal(0)
                    except ArithmeticError:
                        raise RuntimeError(f"{path.name} 合计行金额解析失败，格式可能变了：{footer!r}")
                    if not value.is_finite():
                        raise RuntimeError(
                            f"{path.name} 合计行金额不是有限数（NaN/Infinity），格式可能变了：{footer!r}"
                        )
                    got.append(value)
                if got != sums:
                    raise RuntimeError(
                        f"{path.name} 合计行与数据行合计不一致（合计行={got}，数据行之和={sums}），已中止"
                    )

    def _delimiter_for(self, path: Path) -> str:
        if str(self.delimiter).lower() != "auto":
            return str(self.delimiter)
        return sniff_delimiter(path, self.encoding)


def _kind_cn(kind: str) -> str:
    return {"dec": "数字（decimal）", "int": "整数", "float": "小数"}.get(kind, "数值")


# =============================================================================
# 独立小工具（向导 / 探测用）
# =============================================================================


def sniff_delimiter(path: Path, encoding: str = "utf-8-sig") -> str:
    """探测分隔符：首行 Tab 比逗号多按 Tab；其次分号（仅当没有逗号时）；否则逗号。

    分隔符只看第一个非空行、且只在 Tab/逗号/分号里选：真正的分隔符统计在带引号的
    内容丰富时不可靠，宁可给一个"最像"的默认，让用户用 parse.delimiter 显式覆盖。
    """
    try:
        with path.open("r", encoding=encoding) as handle:
            for line in handle:
                if line.strip():
                    tabs, commas, semicolons = line.count("\t"), line.count(","), line.count(";")
                    if tabs > commas:
                        return "\t"
                    if commas == 0 and semicolons > 0:
                        return ";"
                    return ","
    except (OSError, UnicodeDecodeError):
        pass
    return ","


def read_header(path: Path, encoding: str = "utf-8-sig") -> list[str]:
    """读第一个非空行当表头，返回原始列名列表（向导生成列定义用）。"""
    if not path.is_file():
        raise ConfigError(f"文件不存在：{path}")
    delimiter = sniff_delimiter(path, encoding)
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            for row in reader:
                if any(cell.strip() for cell in row):
                    if not all(cell.strip() for cell in row):
                        raise ConfigError(f"{path.name} 表头里有空列名（行尾多了分隔符？）：{row!r}")
                    return [cell.strip() for cell in row]
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path.name} 不是 {encoding} 编码：{exc}；请换一个编码（如 gbk）再读")
    raise ConfigError(f"{path.name} 里没有找到表头行（空文件？）")


def iter_batches(paths: list[Path], spec: ParseSpec, batch_size: int = 500, stats: dict | None = None):
    """多个文件按 batch_size 攒批给 Tunnel 写入（Tunnel 按批传，别一行一条发）。

    stats 传入时，跳过行/数据行会累计进去（「先数行数」那趟用它统计）。
    """
    batch: list[list] = []
    for path in paths:
        for row in spec.iter_rows(path, stats):
            batch.append(row)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch
