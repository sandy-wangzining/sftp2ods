# -*- coding: utf-8 -*-
"""parse：类型校验、表头映射、取值转换、合计行、分隔符/编码、分批。"""

from __future__ import annotations

import decimal
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import OfflineTestCase, csv_bytes  # noqa: E402

from sftp2ods import parse as parse_mod  # noqa: E402


def parse_cfg(columns, **overrides) -> dict:
    cfg = {"encoding": "utf-8-sig", "delimiter": "auto", "columns": columns}
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
        self.assertEqual(self.rows_of(path, s), [["o1", decimal.Decimal("1.5"), 3, 0.25]])

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
        self.assertEqual(self.rows_of(path, s), [["o1", None, 3, 0.5]])

    def test_on_missing_header_warn_mode(self):
        path = self.make_file("a.csv", csv_bytes(["Order ID", "Count", "Rate"], [["o1", "3", "0.5"]]))
        s = spec(COLUMNS, on_missing_header="warn")
        self.assertEqual(self.rows_of(path, s), [["o1", None, 3, 0.5]])

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
        self.assertEqual(got[0], ["o1", decimal.Decimal("1234.50"), None, None])
        # 空 bigint/double 存 NULL；1e3 转 float
        self.assertEqual(got[1], ["o2", None, 7, 1000.0])

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
        self.assertEqual(self.rows_of(path, s), [["o1", decimal.Decimal("1"), 2, None]])

    def test_extra_columns_always_error(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["o1", "1", "2", "3", "extra"]]))
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, spec(COLUMNS, strict_columns=False))
        self.assertIn("多于表头", str(ctx.exception))

    def test_empty_as_empty_keeps_empty_string(self):
        path = self.make_file("a.csv", csv_bytes([c["header"] for c in COLUMNS], [["", "1", "2", "3"]]))
        s = spec(COLUMNS, empty_as="empty")
        self.assertEqual(self.rows_of(path, s), [["", decimal.Decimal("1"), 2, 3]])
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

    def test_empty_file(self):
        path = self.make_file("a.csv", b"")
        stats = {"skipped": 0, "rows": 0}
        self.assertEqual(self.rows_of(path, spec(COLUMNS), stats), [])
        self.assertEqual(stats["rows"], 0)

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

    def test_footer_skipped_and_validated(self):
        path = self.make(
            [["a", "1.5"], ["b", "2.5"]],
            footer=["", "4.0"],
        )
        s = spec(self.COLS, footer={"sum": ["amount"]})
        got = self.rows_of(path, s)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1][1], decimal.Decimal("2.5"))

    def test_footer_mismatch_errors(self):
        path = self.make([["a", "1.5"]], footer=["", "9.9"])
        s = spec(self.COLS, footer={"sum": ["amount"]})
        with self.assertRaises(RuntimeError) as ctx:
            self.rows_of(path, s)
        self.assertIn("合计", str(ctx.exception))

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
        path = self.make([["a", "1"]], footer=None)
        s = spec(self.COLS, footer={"sum": ["amount"]})
        # 表头缺第一列：footer 开启时直接报错
        self.make_file("b.csv", csv_bytes(["Amount"], [["1"]]))
        with self.assertRaises(RuntimeError):
            self.rows_of(self.tmp / "b.csv", s)
        del path


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
        self.assertEqual(self.rows_of(ok, s), [[decimal.Decimal("1234.50")]])
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


if __name__ == "__main__":
    unittest.main()
