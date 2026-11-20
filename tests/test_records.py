"""核对随档资料可以读取，以及原始记录引用完整。"""

from pathlib import Path
import tempfile
import unittest

from inspect_records import load_records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.data = load_records(Path(__file__).resolve().parents[1] / "data" / "records.json")

    def test_record_references(self):
        for source, field, target in [('service', 'associated', 'service'), ('shipment', 'order', 'service.order'), ('replies', 'card', 'inspection_cards'), ('service', 'verified', 'replies')]:
            group, _, key = target.partition(".")
            keys = {row[key or "id"] for row in self.data[group]}
            for row in self.data[source]:
                refs = row.get(field)
                if refs is None:
                    continue
                refs = refs if isinstance(refs, list) else [refs]
                with self.subTest(kind=source, record=row["id"], field=field):
                    self.assertLessEqual(set(refs), keys)

    def test_duplicate_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"组":[{"id":"1"},{"id":"1"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "编号重复"):
                load_records(path)

    def test_source_packet(self):
        self.assertEqual(self.data["models"][0]["aliases"], self.data["models"][1]["aliases"])
        orders = {r["order"] for r in self.data["service"] if r["phase"] == "复测"}
        self.assertEqual(orders, {"OX1", "OX4"})
        self.assertTrue({r["phase"] for r in self.data["service"]} >= {"承诺", "出库"})
        self.assertEqual({r["channel"] for r in self.data["inspection_cards"]}, {"远程", "到店"})


if __name__ == "__main__":
    unittest.main()
