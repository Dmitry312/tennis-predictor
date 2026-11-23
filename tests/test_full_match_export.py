from infra.audit.full_match_export import build_export, select_shared_matches


def test_keeps_original_fields_and_same_match_across_sources():
    key = "00000000-0000-0000-0000-000000000001"
    sources = {"nbbet_details/": {key: {"source_keys": ["nbbet_details/a.parquet"],
               "rows": [{"sets_json": '[{"score1":6,"score2":4}]', "player_name": "Example"}]}},
               "pbp/": {key: {"source_keys": ["pbp/a.parquet"], "rows": [{"point_num": 1}]}},
               "nbbet_stats/": {}}
    assert select_shared_matches(sources) == [key]
    import json
    output = json.loads(build_export(sources, {key: {"match_id": key, "surface": "clay"}}))
    item = output["matches"][0]
    assert item["postgres"]["surface"] == "clay"
    assert item["s3"]["nbbet_details/"]["rows"][0]["player_name"] == "Example"
    assert item["s3"]["pbp/"]["rows"][0]["point_num"] == 1
    assert item["s3"]["nbbet_stats/"] is None


def test_fails_on_oversized_export():
    key = "00000000-0000-0000-0000-000000000001"
    sources = {"nbbet_details/": {key: {"rows": [{"data": "x" * 100}]}}}
    import pytest
    with pytest.raises(ValueError, match="budget"):
        build_export(sources, {}, max_bytes=30)
