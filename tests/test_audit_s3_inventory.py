from tennis_predictor.ingestion.audit_s3_inventory import audit, summarize_key


class FakeS3:
    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        return {"Contents": [
            {"Key": prefix + "gender=M/level=tour/year=2026/part-a.parquet", "Size": 12},
            {"Key": prefix + "gender=F/level=challenger/year=2025/part-b.parquet", "Size": 0},
        ]}


def test_partition_parser():
    assert summarize_key("pbp/gender=M/level=tour/year=2026/a.parquet") == {
        "gender": "M", "level": "tour", "year": "2026"
    }


def test_inventory_filters_year_and_reads_only():
    result = audit(FakeS3(), "test-bucket", year=2026)
    for value in result["prefixes"].values():
        assert value["totals"]["objects"] == 1
        assert value["totals"]["bytes"] == 12
        assert not value["truncated"]


def test_inventory_flags_empty_files():
    result = audit(FakeS3(), "test-bucket")
    for value in result["prefixes"].values():
        assert value["totals"]["zero_byte"] == 1
