"""空数据试跑和大字段分批的公开使用契约。"""

import tempfile
from decimal import Decimal

from _helpers import OfflineTestCase, minimal_job
from test_cli import World, report

from sftp2ods import parse


class TestPublicIntegrity(OfflineTestCase):
    def test_dry_run_rejects_header_only_file_when_empty_is_disallowed(self):
        job = minimal_job()
        job["target"]["allow_empty"] = False
        with tempfile.TemporaryDirectory() as tmp:
            with World(tmp, job=job, files={"/data/report_20260920.csv": report([])}) as world:
                self.assertEqual(world.sync(bizdate="20260920", dry_run=True), 1)
                self.assertEqual(world.sync(bizdate="20260920"), 1)
                self.assertEqual(world.table.deleted, [])

    def test_byte_batches_preserve_rows_and_decimal_values(self):
        rows = [["汉" * 10, Decimal("1.01"), None] for _ in range(5)]
        batches = list(parse.iter_batches([], None, batch_size=500, prepared_rows=rows, max_bytes=70))
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual([row for batch in batches for row in batch], rows)

    def test_one_large_row_is_yielded_alone(self):
        rows = [["x" * 80], ["small"], ["end"]]
        batches = list(parse.iter_batches([], None, prepared_rows=rows, max_bytes=10))
        self.assertEqual([len(batch) for batch in batches], [1, 2])
        self.assertEqual([row for batch in batches for row in batch], rows)
