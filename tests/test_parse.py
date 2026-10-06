# -*- coding: utf-8 -*-
"""parse：类型校验、表头映射、取值转换、合计行、分隔符/编码、分批。"""

from __future__ import annotations

import copy
import decimal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 只在路径缺失时追加（不插到最前面）：避免把仓库根/tests 目录置于标准库与第三方库
# 之前遮蔽同名模块；按 README 在仓库根运行（或 CI 里 pip install -e .）时，
# 本地包本来就在搜索路径最前（python -m 会把当前目录放在 sys.path[0]）
for _path in (Path(__file__).resolve().parents[1], Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from _helpers import OfflineTestCase, csv_bytes  # noqa: E402

from sftp2ods import parse as parse_mod  # noqa: E402


def parse_cfg(columns, **overrides) -> dict:
    # 深拷贝列定义：模块级 COLUMNS 被多个用例共享，被测代码若在校验/规格化时回写列字典，
    # 用例之间会互相污染（顺序依赖的隐性坑）。deepcopy 同时兼容"故意传非法元素"的用例
    cfg = {"encoding": "utf-8-sig", "delimiter": "auto", "columns": copy.deepcopy(columns)}
    cfg.update(overrides)
    return cfg


def spec(columns, **overrides) -> parse_mod.ParseSpec:
    cfg = parse_cfg(columns, **overrides)
    parse_mod.validate_parse_config(cfg)
    return parse_mod.ParseSpec(cfg)


class TestTypes(OfflineTestCase):
    def test_normalize(self):
        self.assertEqual(parse_mod.normalize_type("DECIMAL(19, 10)"), "decimal(19,10)")
        self.assertEqual(parse_mod.normalize_type(" STRING "), "string")

    def test_kind(self):
        self.assertEqual(parse_mod.kind_of("string"), "str")
        self.assertEqual(parse_mod.kind_of("decimal(19,10)"), "dec")
        self.assertEqual(parse_mod.kind_of("bigint"), "int")
        self.assertEqual(parse_mod.kind_of("double"), "float")

    def test_valid_types(self):
        self.assertEqual(parse_mod.validate_type("string", "x"), "string")
        self.assertEqual(parse_mod.validate_type("decimal(19,10)", "x"), "decimal(19,10)")

    def test_invalid_types(self):
        for bad in ("varchar(10)", "decimal(0,0)", "decimal(39,0)", "decimal(5,7)", "int", "text"):
            with self.assertRaises(SystemExit):
                parse_mod.validate_type(bad, "x")


class TestValidateParseConfig(OfflineTestCase):
    def test_ok_minimal(self):
        parse_mod.validate_parse_config(parse_cfg([{"header": "A", "name": "a", "type": "string"}]))

    def test_columns_required(self):
        for bad in (None, [], {}, "x"):
            with self.assertRaises(SystemExit):
                parse_mod.validate_parse_config({"columns": bad})

    def test_column_shape(self):
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg(["not-a-dict"]))
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg([{"name": "a", "type": "string"}]))
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg([{"header": "A", "type": "string"}]))
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg([{"header": "A", "name": "bad-name", "type": "string"}]))

    def test_duplicate_header_and_name(self):
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.validate_parse_config(
                parse_cfg(
                    [
                        {"header": "Order ID", "name": "a", "type": "string"},
                        {"header": " order  id ", "name": "b", "type": "string"},
                    ]
                )
            )
        self.assertIn("重复", str(ctx.exception))
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(
                parse_cfg(
                    [
                        {"header": "A", "name": "x", "type": "string"},
                        {"header": "B", "name": "X", "type": "string"},
                    ]
                )
            )

    def test_encoding(self):
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "string"}], encoding="no-such-encoding")
            )
        self.assertIn("编码", str(ctx.exception))

    def test_delimiter(self):
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg([{"header": "A", "name": "a", "type": "string"}], delimiter="||"))
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(parse_cfg([{"header": "A", "name": "a", "type": "string"}], delimiter="|"))

    def test_on_missing_header(self):
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "string"}], on_missing_header="skip")
            )

    def test_skip_if_empty_unknown(self):
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "string"}], skip_if_empty=["nope"])
            )
        self.assertIn("nope", str(ctx.exception))

    def test_footer_sum_unknown(self):
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "string"}], footer={"sum": ["nope"]})
            )
        with self.assertRaises(SystemExit):
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "string"}], footer={"bogus": []})
            )

    def test_footer_sum_must_be_decimal(self):
        # 两列：首列给"合计行识别"用，sum 指向第二列（首列不能进 sum，见下一条校验）
        for bad_type in ("string", "bigint", "double"):
            with self.assertRaises(SystemExit) as ctx:
                parse_mod.validate_parse_config(
                    parse_cfg(
                        [
                            {"header": "A", "name": "a", "type": "string"},
                            {"header": "B", "name": "b", "type": bad_type},
                        ],
                        footer={"sum": ["b"]},
                    )
                )
            self.assertIn("decimal", str(ctx.exception))
        parse_mod.validate_parse_config(
            parse_cfg(
                [
                    {"header": "A", "name": "a", "type": "string"},
                    {"header": "B", "name": "b", "type": "decimal(19,10)"},
                ],
                footer={"sum": ["b"]},
            )
        )

    def test_footer_comment_keys_allowed(self):
        parse_mod.validate_parse_config(
            parse_cfg(
                [
                    {"header": "A", "name": "a", "type": "string"},
                    {"header": "B", "name": "b", "type": "decimal(19,10)"},
                ],
                footer={"//说明": "x", "sum": ["b"]},
            )
        )

    def test_footer_conflicts_with_skip_if_empty_on_first_column(self):
        """footer 靠"首列为空"识别合计行；skip_if_empty 若也指向第一列，首列为空的数据行
        会先被判成合计行、跳过规则永远不生效（多行时还会报"多行合计行"）——配置阶段拦下。"""
        columns = [
            {"header": "Order ID", "name": "order_id", "type": "string"},
            {"header": "Status", "name": "status", "type": "string"},
            {"header": "Amount", "name": "amount", "type": "decimal(19,10)"},
        ]
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.validate_parse_config(parse_cfg(columns, skip_if_empty=["order_id"], footer={"sum": ["amount"]}))
        self.assertIn("第一列", str(ctx.exception))
        # 跳过规则不指向首列时没冲突（正常配置）
        parse_mod.validate_parse_config(parse_cfg(columns, skip_if_empty=["status"], footer={"sum": ["amount"]}))


class SpecTestCase(OfflineTestCase):
    """带临时目录的小工具。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_file(self, name: str, content: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def rows_of(self, path: Path, s: parse_mod.ParseSpec, stats=None):
        return list(s.iter_rows(path, stats))

    def assert_rows(self, got, expected):
        """逐元素比较行：Decimal 按 as_tuple 比（== 会忽略尾随零，"1234.5" 与 "1234.50" 相等），
        float 列用 assertAlmostEqual（二进制浮点不适合精确相等），其余照常。"""
        self.assertEqual(len(got), len(expected), f"行数不一致：{got!r} != {expected!r}")
        for got_row, exp_row in zip(got, expected):
            self.assertEqual(len(got_row), len(exp_row))
            for got_val, exp_val in zip(got_row, exp_row):
                if isinstance(exp_val, decimal.Decimal):
                    self.assertIsInstance(got_val, decimal.Decimal)
                    self.assertEqual(got_val.as_tuple(), exp_val.as_tuple())
                elif isinstance(exp_val, float):
                    self.assertAlmostEqual(got_val, exp_val)
                else:
                    self.assertEqual(got_val, exp_val)


COLUMNS = [
    {"header": "Order ID", "name": "order_id", "type": "string"},
    {"header": "Settlement amount", "name": "settlement_amount", "type": "decimal(19,10)"},
    {"header": "Count", "name": "cnt", "type": "bigint"},
    {"header": "Rate", "name": "rate", "type": "double"},
]


class TestHeaderMapping(SpecTestCase):
    def test_maps_by_normalized_header(self):
        path = self.make_file(
            "a.csv",
            csv_bytes([" order  id ", "SETTLEMENT AMOUNT", "Count", "Rate"], [["o1", "1.5", "3", "0.25"]]),
        )
        s = spec(COLUMNS)
        self.assert_rows(self.rows_of(path, s), [["o1", decimal.Decimal("1.5"), 3, 0.25]])

    def test_missing_required_errors(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", "Count"], [["o1", "1"]]))
        s = spec([c for c in COLUMNS if c["name"] != "rate"])
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("缺少必需列头", str(ctx.exception))
        self.assertIn("Settlement amount", str(ctx.exception))

    def test_missing_optional_warns_and_nulls(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", "Count", "Rate"], [["o1", "3", "0.5"]]))
        columns = [
            {"header": "Order ID", "name": "order_id", "type": "string"},
            {"header": "Settlement amount", "name": "settlement_amount", "type": "decimal(19,10)", "required": False},
            {"header": "Count", "name": "cnt", "type": "bigint"},
            {"header": "Rate", "name": "rate", "type": "double"},
        ]
        s = spec(columns)
        logs = []
        with mock.patch.object(parse_mod, "log", logs.append):
            got = self.rows_of(path, s)
        self.assert_rows(got, [["o1", None, 3, 0.5]])
        # 不仅结果要补 NULL，"warn" 告警本身也要真的发出来（静默忽略不该通过本用例）
        self.assertTrue(
            any("未匹配到列头" in str(line) and "Settlement amount" in str(line) for line in logs),
            logs,
        )

    def test_on_missing_header_warn_mode(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", "Count", "Rate"], [["o1", "3", "0.5"]]))
        s = spec(COLUMNS, on_missing_header="warn")
        logs = []
        with mock.patch.object(parse_mod, "log", logs.append):
            got = self.rows_of(path, s)
        self.assert_rows(got, [["o1", None, 3, 0.5]])
        self.assertTrue(any("未匹配到列头" in str(line) for line in logs), logs)

    def test_extra_source_headers_recorded_and_ignored(self):
        path = self.make_file(
            "a.csv",
            csv_bytes(
                ["Order ID", "Settlement amount", "Count", "Rate", "New column", "Another"],
                [["o1", "1.5", "3", "0.25", "x", "y"]],
            ),
        )
        stats = {"skipped": 0}
        self.assert_rows(self.rows_of(path, spec(COLUMNS), stats), [["o1", decimal.Decimal("1.5"), 3, 0.25]])
        self.assertEqual(stats["extra_headers"], ["New column", "Another"])

    def test_extra_headers_accumulate_across_files(self):
        """多文件共用 stats 时新增列必须累计，不能被后一个文件覆盖。"""
        p1 = self.make_file(
            "a.csv",
            csv_bytes(
                ["Order ID", "Settlement amount", "Count", "Rate", "NewA"],
                [["o1", "1.5", "3", "0.25", "x"]],
            ),
        )
        p2 = self.make_file(
            "b.csv",
            csv_bytes(
                ["Order ID", "Settlement amount", "Count", "Rate", "NewB"],
                [["o2", "1.5", "3", "0.25", "y"]],
            ),
        )
        stats = {"skipped": 0}
        list(parse_mod.iter_batches([p1, p2], spec(COLUMNS), batch_size=10, stats=stats))
        self.assertEqual(stats["extra_headers"], ["NewA", "NewB"])

    def test_no_extra_headers_entry_when_all_mapped(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1.5", "3", "0.25"]]))
        stats = {"skipped": 0}
        self.rows_of(path, spec(COLUMNS), stats)
        self.assertNotIn("extra_headers", stats)

    def test_duplicate_file_header_errors(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", "Order ID"], [["1", "2"]]))
        with self.assertRaises(RuntimeError):
            self.rows_of(path, spec(COLUMNS[:1]))

    def test_empty_file_header_errors(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", ""], [["1", "2"]]))
        with self.assertRaises(RuntimeError):
            self.rows_of(path, spec(COLUMNS[:1]))


class TestIterRows(SpecTestCase):
    def test_types_and_empty_values(self):
        rows = [["o1", "1,234.50", "", ""], ["o2", "", "7", "1e3"]]
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], rows))
        got = self.rows_of(path, spec(COLUMNS))
        # Decimal 按 as_tuple 比：能发现"小数位丢了"（1234.50 被写成 1234.5 时 == 仍相等）
        self.assert_rows(got, [["o1", decimal.Decimal("1234.50"), None, None], ["o2", None, 7, 1000.0]])

    def test_bad_decimal_errors(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "abc", "1", "1"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(COLUMNS))
        self.assertIn("Settlement amount", str(ctx.exception))

    def test_bad_int_errors(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "1.5", "1"]]))
        with self.assertRaises(RuntimeError):
            self.rows_of(path, spec(COLUMNS))

    def test_nan_decimal_rejected(self):
        for bad in ("NaN", "Infinity", "-Infinity", "sNaN"):
            path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", bad, "1", "1"]]))
            with self.assertRaises(RuntimeError) as ctx:
                self.rows_of(path, spec(COLUMNS))
            self.assertIn("Settlement amount", str(ctx.exception))

    def test_non_finite_float_rejected(self):
        for bad in ("inf", "-inf", "nan", "Infinity"):
            path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "1", bad]]))
            with self.assertRaises(RuntimeError) as ctx:
                self.rows_of(path, spec(COLUMNS))
            self.assertIn("Rate", str(ctx.exception))

    def test_strict_columns(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(COLUMNS))
        self.assertIn("列数", str(ctx.exception))

    def test_strict_columns_false_pads(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2"]]))
        s = spec(COLUMNS, strict_columns=False)
        self.assert_rows(self.rows_of(path, s), [["o1", decimal.Decimal("1"), 2, None]])

    def test_extra_columns_always_error(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3", "extra"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(COLUMNS, strict_columns=False))
        self.assertIn("多于表头", str(ctx.exception))

    def test_empty_as_empty_keeps_empty_string(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["", "1", "2", "3"]]))
        s = spec(COLUMNS, empty_as="empty")
        self.assert_rows(self.rows_of(path, s), [["", decimal.Decimal("1"), 2, 3]])
        # skip_if_empty 对 '' 同样生效（空串与 NULL 都算空）
        s2 = spec(COLUMNS, empty_as="empty", skip_if_empty=["order_id"])
        stats = {"skipped": 0}
        self.assertEqual(self.rows_of(path, s2, stats), [])
        self.assertEqual(stats["skipped"], 1)

    def test_empty_as_invalid(self):
        with self.assertRaises(SystemExit):
            spec(COLUMNS, empty_as="blank")

    def test_skip_if_empty(self):
        path = self.make_file(
            "a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"], ["", "1", "2", "3"]])
        )
        s = spec(COLUMNS, skip_if_empty=["order_id"])
        stats = {"skipped": 0}
        got = self.rows_of(path, s, stats)
        self.assertEqual(len(got), 1)
        self.assertEqual(stats["skipped"], 1)

    def test_skip_column_missing_header_is_error(self):
        """skip_if_empty 的列不在文件表头时，按"空值"跳过会把所有行跳掉、整段写 0 行——
        必须显式报错（先删后填的流程下等于清空分区）。"""
        cols = [dict(c, required=False) for c in COLUMNS]
        path = self.make_file(
            "a.csv",
            csv_bytes([c["header"] for c in COLUMNS if c["name"] != "order_id"], [["1", "2", "3"]]),
        )
        s = spec(cols, skip_if_empty=["order_id"])
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("skip_if_empty", str(ctx.exception))

    def test_empty_file(self):
        path = self.make_file("a.csv", b"")
        stats = {"skipped": 0, "rows": 0}
        self.assertEqual(self.rows_of(path, spec(COLUMNS), stats), [])
        self.assertEqual(stats["rows"], 0)

    def test_empty_file_with_footer_is_error(self):
        """配置了合计行时空文件必须中止：截断成 0 字节是最常见的故障形态，
        静默写 0 行在先删后填的流程下等于清空分区。"""
        path = self.make_file("a.csv", b"")
        s = spec(COLUMNS, footer={"sum": ["settlement_amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("空文件", str(ctx.exception))

    def test_header_only(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], []))
        self.assertEqual(self.rows_of(path, spec(COLUMNS)), [])

    def test_blank_lines_skipped(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]]) + b"\r\n,\r\n,,,\r\n\r\n"
        path = self.make_file("a.csv", content)
        self.assertEqual(len(self.rows_of(path, spec(COLUMNS))), 1)


class TestFooter(SpecTestCase):
    COLS = [
        {"header": "Name", "name": "name", "type": "string"},
        {"header": "Amount", "name": "amount", "type": "decimal(19,10)"},
    ]

    def make(self, rows, footer=None) -> Path:
        all_rows = list(rows)
        if footer is not None:
            all_rows.append(footer)
        return self.make_file("a.csv", csv_bytes([c["header"] for c in self.COLS], all_rows))

    def test_footer_row_may_differ_in_column_count(self):
        """合计行是人写的汇总行，列数常与数据行不一致：识别必须排在列数校验之前，
        不能被 strict_columns 误判成"列结构变了"整份中止。"""
        cols = list(self.COLS) + [{"header": "Extra", "name": "extra", "type": "string"}]
        path = self.make_file(
            "a.csv",
            csv_bytes(
                [c["header"] for c in cols],
                [["a", "1.5", "x"], ["b", "2.5", "y"], ["", "4.0"]],
            ),
        )
        s = spec(cols, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1][1], decimal.Decimal("2.5"))

    def test_footer_row_may_be_longer_than_header(self):
        """合计行带尾随分隔符（解析出 3 列而表头 2 列）也要先按合计行识别：
        "多于表头"的校验只作用于数据行，不能把合法文件中止。"""
        path = self.make_file(
            "a.csv",
            csv_bytes(
                [c["header"] for c in self.COLS],
                [["a", "1.5"], ["b", "2.5"], ["", "4.0", ""]],
            ),
        )
        s = spec(self.COLS, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1][1], decimal.Decimal("2.5"))

    def test_footer_sum_may_exceed_column_range(self):
        """合计是派生值：多行之和天然可以超出单列 decimal(p,s) 范围（两行 99999999.99
        的合法和就超 decimal(10,2)），不能按数据行的范围校验把合法文件中止。"""
        cols = [
            {"header": "Name", "name": "name", "type": "string"},
            {"header": "Amount", "name": "amount", "type": "decimal(10,2)"},
        ]
        path = self.make_file(
            "a.csv",
            csv_bytes(
                [c["header"] for c in cols],
                [["a", "99999999.99"], ["b", "99999999.99"], ["", "199999999.98"]],
            ),
        )
        s = spec(cols, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)

    def test_footer_skipped_and_validated(self):

        path = self.make(
            [["a", "1.5"], ["b", "2.5"]],
            footer=["", "4.0"],
        )
        s = spec(self.COLS, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1][1], decimal.Decimal("2.5"))

    def test_footer_dirty_number_is_rejected_like_data_rows(self):
        """合计行的数字口径与数据行一致：下划线/非 ASCII 脏值同样拒绝（Decimal 也接受它们）。"""
        for raw, must in (("1_0", "下划线"), ("１２３", "非 ASCII")):
            path = self.make([["a", raw], ["b", raw]], footer=["", raw])
            s = spec(self.COLS, footer={"sum": ["amount"]})
            with self.assertRaises(RuntimeError) as ctx:
                self.rows_of(path, s)
            self.assertIn(must, str(ctx.exception))

    def test_footer_value_out_of_decimal_range_is_rejected(self):
        """合计行也要过 decimal(p,s) 范围校验：数据行通过、合计行超限时同样报错。"""
        cols = [
            {"header": "Name", "name": "name", "type": "string"},
            {"header": "Amount", "name": "amount", "type": "decimal(5,2)"},
        ]
        rows = csv_bytes(["Name", "Amount"], [["a", "1.00"], ["", "12345.67"]])
        path = self.make_file("f.csv", rows)
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(cols, footer={"sum": ["amount"]}))
        self.assertIn("合计行", str(ctx.exception))

    def test_footer_mismatch_errors(self):
        path = self.make([["a", "1.5"]], footer=["", "9.9"])
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("合计", str(ctx.exception))

    def test_footer_enabled_but_missing_is_error(self):
        """配置了 parse.footer 但文件没有合计行：当截断/格式变化，必须失败。"""
        path = self.make([["a", "1.5"], ["b", "2.5"]])
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("没有合计行", str(ctx.exception))

    def test_footer_requires_config_order_match_file_order(self):
        """footer 开启时配置第一列必须是文件首列：识别用「文件首列」而校验用「配置第一列」，
        两套口径在列序不一致时打架（漏检/误报）；直接要求重合并给出可操作的报错。
        """
        cols = [
            {"header": "金额A", "name": "amount_a", "type": "decimal(19,2)"},
            {"header": "金额B", "name": "amount_b", "type": "decimal(19,2)"},
            {"header": "单据号", "name": "doc_no", "type": "string"},
        ]
        rows = csv_bytes(["单据号", "金额A", "金额B"], [["d1", "1.0", "2.0"], ["", "1.0", "2.0"]])
        path = self.make_file("o.csv", rows)
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(cols, footer={"sum": ["amount_b"]}))
        self.assertIn("不是文件首列", str(ctx.exception))

    def test_footer_enabled_header_only_is_error(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in self.COLS], []))
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("没有合计行", str(ctx.exception))

    def test_multiple_footers_error(self):
        rows = csv_bytes([c["header"] for c in self.COLS], [["a", "1"], ["", "0"], ["b", "2"], ["", "1"]])
        path = self.make_file("a.csv", rows)
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError):
            self.rows_of(path, s)

    def test_footer_not_last_errors(self):
        rows = csv_bytes([c["header"] for c in self.COLS], [["a", "1"], ["", "0"], ["b", "2"]])
        path = self.make_file("a.csv", rows)
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("合计行不在数据行之后", str(ctx.exception))

    def test_footer_without_sum_only_skips(self):
        path = self.make([["a", "1"]], footer=["", "999"])
        s = spec(self.COLS, footer={})
        self.assertEqual(len(self.rows_of(path, s)), 1)

    def test_footer_requires_first_column(self):
        s = spec(self.COLS, footer={"sum": ["amount"]})
        # 表头缺第一列：footer 开启时直接报错
        self.make_file("b.csv", csv_bytes(["Amount"], [["1"]]))
        with self.assertRaises(RuntimeError):
            self.rows_of(self.tmp / "b.csv", s)

    def test_footer_amounts_use_same_number_rules_as_data_rows(self):
        """合计行金额与数据行走同一套数字口径：畸形千分位（1,23）必须报错，不能静默读错。"""
        path = self.make([["a", "1.5"]], footer=["", "1,23"])
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("千分位", str(ctx.exception))

    def test_footer_amounts_thousands_marker_ok(self):
        path = self.make([["a", "1,234.50"], ["b", "2.50"]], footer=["", "1,237.00"])
        s = spec(self.COLS, footer={"sum": ["amount"]})
        self.assertEqual(len(self.rows_of(path, s)), 2)

    def test_footer_sum_follows_header_mapping_not_config_index(self):
        """合计行按表头映射取值：文件多一列时不能用配置列下标去索引原始行。"""
        path = self.make_file(
            "a.csv",
            csv_bytes(["Name", "Extra", "Amount"], [["a", "999", "1.5"], ["b", "0", "2.5"], ["", "0", "4.0"]]),
        )
        s = spec(self.COLS, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)

    def test_footer_sum_missing_mapped_column_errors(self):
        cols = [
            {"header": "Name", "name": "name", "type": "string"},
            {"header": "Amount", "name": "amount", "type": "decimal(19,10)", "required": False},
        ]
        # 文件里没有 Amount，但合计行不是空行（否则会被当成空行跳过）
        path = self.make_file("a.csv", csv_bytes(["Name", "Extra"], [["a", "z"], ["", "4.0"]]))
        s = spec(cols, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("合计行无法核对", str(ctx.exception))
        self.assertIn("Amount", str(ctx.exception))

    def test_skip_if_empty_rows_still_count_in_footer_sum(self):
        """skip_if_empty 的行不写库，但仍计入合计（与文件合计行同口径）。"""
        cols = [
            {"header": "Name", "name": "name", "type": "string"},
            {"header": "Note", "name": "note", "type": "string"},
            {"header": "Amount", "name": "amount", "type": "decimal(19,10)"},
        ]
        path = self.make_file(
            "a.csv",
            csv_bytes(
                ["Name", "Note", "Amount"],
                [["a", "keep", "1.5"], ["b", "", "2.5"], ["", "", "4.0"]],
            ),
        )
        s = spec(cols, footer={"sum": ["amount"]}, skip_if_empty=["note"])
        stats = {"skipped": 0, "rows": 0}
        got = self.rows_of(path, s, stats)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "a")
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["rows"], 1)


class TestDelimiterEncoding(SpecTestCase):
    def test_tab_detected(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]], delimiter="\t")
        path = self.make_file("a.tsv", content)
        self.assertEqual(parse_mod.sniff_delimiter(path), "\t")
        self.assertEqual(len(self.rows_of(path, spec(COLUMNS))), 1)

    def test_semicolon_detected_without_commas(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]], delimiter=";")
        path = self.make_file("a.csv", content)
        self.assertEqual(parse_mod.sniff_delimiter(path), ";")
        self.assertEqual(len(self.rows_of(path, spec(COLUMNS))), 1)

    def test_explicit_delimiter_wins(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]], delimiter="\t")
        path = self.make_file("a.tsv", content)
        s = spec(COLUMNS, delimiter="\t")
        self.assertEqual(len(self.rows_of(path, s)), 1)

    def test_gbk_encoding(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]], encoding="gbk")
        path = self.make_file("a.csv", content)
        s = spec(COLUMNS, encoding="gbk")
        self.assertEqual(len(self.rows_of(path, s)), 1)

    def test_wrong_encoding_errors_with_hint(self):
        content = csv_bytes([c["header"] for c in COLUMNS], [["订单一", "1", "2", "3"]], encoding="gbk")
        path = self.make_file("a.csv", content)
        s = spec(COLUMNS, encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("gbk", str(ctx.exception))

    def test_sniff_delimiter_failure_warns_and_falls_back(self):
        """探测分隔符失败（文件不可读/编码不符）不能静默回退逗号：要留一条线索。"""
        path = self.make_file("a.csv", "金额,数量\n1,2\n".encode("gbk"))
        logs = []
        with mock.patch.object(parse_mod, "log", logs.append):
            self.assertEqual(parse_mod.sniff_delimiter(path, encoding="utf-8"), ",")
        self.assertTrue(any("探测分隔符失败" in str(line) for line in logs), logs)


class TestBatches(SpecTestCase):
    def test_batch_size(self):
        rows = [[f"o{i}", "1", "2", "3"] for i in range(5)]
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], rows))
        batches = list(parse_mod.iter_batches([path], spec(COLUMNS), batch_size=2))
        self.assertEqual([len(b) for b in batches], [2, 2, 1])

    def test_multiple_files_merge(self):
        p1 = self.make_file("a1.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]]))
        p2 = self.make_file("a2.csv", csv_bytes([c["header"] for c in COLUMNS], [["o2", "1", "2", "3"]]))
        batches = list(parse_mod.iter_batches([p1, p2], spec(COLUMNS), batch_size=10))
        self.assertEqual(sum(len(b) for b in batches), 2)

    def test_prepared_rows_does_not_reread_files(self):
        """写库复用第一趟行列表时，不再打开源文件。"""
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3"]]))
        rows = list(spec(COLUMNS).iter_rows(path))
        path.unlink()
        batches = list(parse_mod.iter_batches([path], spec(COLUMNS), prepared_rows=rows))
        self.assertEqual(sum(len(b) for b in batches), 1)


class TestReadHeader(SpecTestCase):
    def test_reads_first_row(self):
        path = self.make_file("a.csv", csv_bytes(["A", "B"], [["1", "2"]]))
        self.assertEqual(parse_mod.read_header(path), ["A", "B"])

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            parse_mod.read_header(self.tmp / "nope.csv")

    def test_empty_file(self):
        path = self.make_file("a.csv", b"")
        with self.assertRaises(SystemExit):
            parse_mod.read_header(path)


class TestValueRange(SpecTestCase):
    """取值必须落在列类型允许的范围内（超出宁可在写库前失败）。"""

    def test_thousands_ok_but_euro_decimal_rejected(self):
        """千分位逗号照常支持；欧式小数（1.234,56）报错，而不是静默缩水 1000 倍。"""
        s = spec([{"header": "金额", "name": "amount", "type": "decimal(19,2)"}])
        ok = self.make_file("ok.csv", csv_bytes(["金额"], [["1,234.50"]]))
        self.assert_rows(self.rows_of(ok, s), [[decimal.Decimal("1234.50")]])
        for raw in ("1.234,56", "1,23", "1,,2"):
            bad = self.make_file("bad.csv", csv_bytes(["金额"], [[raw]]))
            with self.assertRaises(RuntimeError) as ctx:
                self.rows_of(bad, s)
            self.assertIn("千分位", str(ctx.exception))

    def test_decimal_scale_and_precision_enforced(self):
        s = spec([{"header": "金额", "name": "amount", "type": "decimal(19,2)"}])
        too_scale = self.make_file("s.csv", csv_bytes(["金额"], [["1.999"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(too_scale, s)
        self.assertIn("小数位", str(ctx.exception))
        too_big = self.make_file("b.csv", csv_bytes(["金额"], [["12345678901234567890"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(too_big, s)
        self.assertIn("整数位", str(ctx.exception))

    def test_bigint_range_enforced(self):
        s = spec([{"header": "n", "name": "n", "type": "bigint"}])
        ok = self.make_file("ok.csv", csv_bytes(["n"], [[str(2**63 - 1)]]))
        self.assertEqual(self.rows_of(ok, s), [[2**63 - 1]])
        bad = self.make_file("bad.csv", csv_bytes(["n"], [[str(2**63)]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(bad, s)
        self.assertIn("bigint", str(ctx.exception))

    def test_numeric_columns_reject_underscores_and_unicode_digits(self):
        """三种数值解析都比「报表里的数字」宽松（PEP 515 下划线 int("1_0")==10、
        Decimal("1_0")==10、Unicode 数字照收），口径要对齐：一律拒，不能静默改值。"""
        cases = (("1_0", "下划线"), ("１２３", "非 ASCII"))
        for col_type in ("bigint", "double", "decimal(19,4)"):
            s = spec([{"header": "n", "name": "n", "type": col_type}])
            for raw, must in cases:
                path = self.make_file("dirty.csv", csv_bytes(["n"], [[raw]]))
                with self.assertRaises(RuntimeError) as ctx:
                    self.rows_of(path, s)
                self.assertIn(must, str(ctx.exception))

    def test_decimal_trailing_zeros_are_not_rejected(self):
        """尾部补零（decimal(10,2) 下的 1.500）数值上精确可表示，不该被当成"小数位超限"拦下；
        真正会被舍入改值的（1.234）仍要报错。"""
        s = spec([{"header": "金额", "name": "amount", "type": "decimal(10,2)"}])
        ok = self.make_file("ok.csv", csv_bytes(["金额"], [["1.500"], ["2.3400"]]))
        got = self.rows_of(ok, s)
        self.assert_rows(got, [[decimal.Decimal("1.500")], [decimal.Decimal("2.3400")]])
        bad = self.make_file("bad.csv", csv_bytes(["金额"], [["1.234"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(bad, s)
        self.assertIn("小数位", str(ctx.exception))

    def test_huge_exponent_is_rejected_without_allocating(self):
        """单元格里 1e-1000000000 这类指数不能触发 10**N 的巨型整数构造。

        修复前按 frac_digits-scale 直接算 10**999999996（4 亿位，数百 MB + 秒级 CPU，
        再大直接 MemoryError——那还不是可读的 ValueRangeError）。用宽松的墙钟上界兜住
        "卡死/分配巨大内存"这一类失败模式（正常路径 <10ms，阈值只拦数量级异常）。
        """
        s = spec([{"header": "金额", "name": "amount", "type": "decimal(19,4)"}])
        path = self.make_file("huge.csv", csv_bytes(["金额"], [["1e-1000000000"]]))
        started = time.perf_counter()
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("小数位", str(ctx.exception))
        self.assertLess(time.perf_counter() - started, 5.0, "指数路径疑似退化成巨型整数运算")


class TestFooterPrecision(SpecTestCase):
    def test_large_amounts_not_rounded_by_default_context(self):
        """decimal(38,10) 的大额累加不能被 decimal 默认的 28 位精度舍入（否则天天假报不一致）。"""
        big = "1234567890123456789012345678.1234567890"
        with decimal.localcontext() as ctx:
            ctx.prec = 60
            total = str(decimal.Decimal(big) * 2)
        columns = [
            {"header": "id", "name": "id", "type": "string"},
            {"header": "a", "name": "a", "type": "decimal(38,10)"},
        ]
        path = self.make_file("big.csv", csv_bytes(["id", "a"], [["x", big], ["y", big], ["", total]]))
        self.assertEqual(len(self.rows_of(path, spec(columns, footer={"sum": ["a"]}))), 2)


class TestFooterConfigGuards(OfflineTestCase):
    def test_footer_sum_cannot_include_first_column(self):
        """合计行靠「首列为空」识别，sum 里不能有第一列（那一列取不到合计值）。"""
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.validate_parse_config(
                parse_cfg([{"header": "A", "name": "a", "type": "decimal(19,10)"}], footer={"sum": ["a"]})
            )
        self.assertIn("第一列", str(ctx.exception))


class TestParseSpecGuards(OfflineTestCase):
    def test_empty_columns_rejected_with_clean_error(self):
        """columns 为空时不能等到 map_header/footer 里抛无上下文的 IndexError，构造时就快速失败。"""
        with self.assertRaises(SystemExit) as ctx:
            parse_mod.ParseSpec({})
        self.assertIn("parse.columns", str(ctx.exception))
        with self.assertRaises(SystemExit):
            parse_mod.ParseSpec({"columns": []})


if __name__ == "__main__":
    unittest.main()
