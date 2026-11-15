"""Content audit tests using in-memory Parquet and fake S3."""
import io

import pyarrow as pa
import pyarrow.parquet as pq

from tennis_predictor.ingestion.audit_s3_content import audit


def parquet(rows):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    return buf.getvalue()


class FakeS3:
    def __init__(self):
        self.files = {
            "nbbet_details/gender=M/level=tour/year=2026/a.parquet": parquet([
                {"canonical_match_id": "a", "nbbet_match_id": 1, "status": 3,
                 "sets_json": "[]", "has_coverage": True, "fetched_at": None},
                {"canonical_match_id": "b", "nbbet_match_id": 2, "status": 3,
                 "sets_json": "[]", "has_coverage": False, "fetched_at": None}]),
            "nbbet_stats/gender=M/level=tour/year=2026/a.parquet": parquet([
                {"canonical_match_id": "a", "scope": 0, "fetched_at": None}]),
            "pbp/gender=M/level=tour/year=2026/a.parquet": parquet([
                {"canonical_match_id": "a", "set_num": 1, "game_num": 1,
                 "point_num": 1, "point_winner": 1, "game_complete": True}]),
            "nbbet_raw/gender=M/level=tour/year=2026/a.parquet": parquet([
                {"canonical_match_id": "a", "nbbet_match_id": 1, "fetched_at": None}]),
        }

    def list_objects_v2(self, **kwargs):
        return {"Contents": [
            {"Key": k, "Size": len(v)} for k, v in self.files.items()
            if k.startswith(kwargs["Prefix"])]}

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.files[kwargs["Key"]])}


def test_content_audit_reports_missing_datasets_without_writes():
    result = audit(FakeS3(), "bucket", 2026, 100, 5, 10)
    assert result["prefixes"]["nbbet_details"]["unique_matches"] == 2
    assert result["cross_dataset"]["details_without_stats"] == 1
    assert result["cross_dataset"]["details_without_pbp"] == 1
    assert result["cross_dataset"]["details_without_raw"] == 1
    assert result["samples"]["details_without_pbp"] == ["b"]
