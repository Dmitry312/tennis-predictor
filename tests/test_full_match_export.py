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



def test_selection_is_deterministic_and_bounded():
    details = {f"match-{n:02d}": {"rows": []} for n in range(20)}
    assert select_shared_matches({"nbbet_details/": details}, limit=3) == [
        "match-00", "match-01", "match-02"
    ]


def test_export_keeps_all_nested_json_and_nulls():
    import json
    match = "00000000-0000-0000-0000-000000000002"
    raw = {"players_json": '{"p1":"Player One","p2":"Player Two"}',
           "sets_json": '[{"first":7,"second":6,"tb":5}]',
           "extra_json": None}
    source = {"nbbet_details/": {match: {"rows": [raw], "source_keys": ["details/x.parquet"]}}}
    record = json.loads(build_export(source, {match: {"player1": "Player One"}}))["matches"][0]
    assert record["s3"]["nbbet_details/"]["rows"][0] == raw
    assert record["postgres"]["player1"] == "Player One"


def test_exact_partition_mapping_from_writer_layout():
    from infra.audit.full_match_export import related_partition_keys
    key = "pbp/gender=M/level=tour/year=2025/part-abc-00001.parquet"
    result = related_partition_keys(key)
    assert result["pbp/"] == key
    assert result["nbbet_details/"] == key.replace("pbp/", "nbbet_details/")
    assert result["nbbet_stats/"] == key.replace("pbp/", "nbbet_stats/")
    assert related_partition_keys("../secret") is None
    assert related_partition_keys("pbp/gender=M/level=tour/year=2025/../x.parquet") is None
