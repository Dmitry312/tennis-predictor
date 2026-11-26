"""Offline real-catalog checks plus fabricated failure cases; no live sources."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("ml_catalog_audit",ROOT/"scripts/audit_ml_catalog.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def test_real_catalog_deterministic_readonly_and_offline(monkeypatch):
    def deny(*args,**kwargs): raise AssertionError("network forbidden")
    monkeypatch.setattr(socket,"socket",deny)
    catalog=ROOT/"docs/ml/data_catalog"
    paths=sorted(catalog.rglob("*.json"))
    before={str(p):m.digest(p) for p in paths}
    one=m.audit(); two=m.audit()
    assert one==two and one["production_access"] is False
    assert one["match_count"]==20
    assert one["records_by_source"]==dict(matches=20,nbbet_details=9,nbbet_stats=25,pbp=1350,nbbet_raw=5)
    assert all(not p["uncatalogued"] for p in one["plans"])
    assert sum(r["odds_pair_valid"] for r in one["matches"])==4
    assert not any(f["code"].endswith("mismatch") for r in one["matches"] for f in r["findings"])
    assert before=={str(p):m.digest(p) for p in paths}
    assert all(not r["coverage_complete"] and not r["pbp"]["full_match_verified"] for r in one["matches"])
    assert sum(any(f["code"]=="pbp_rows_vs_service_points" for f in r["findings"]) for r in one["matches"])==8
    assert all(c["agrees"] for r in one["matches"] for c in r["stat_additivity"])

def test_cli_from_other_directory(tmp_path):
    run=subprocess.run([sys.executable,str(ROOT/"scripts/audit_ml_catalog.py")],cwd=tmp_path,
                       text=True,capture_output=True,check=True)
    assert json.loads(run.stdout)["match_count"]==20
    assert not list(tmp_path.iterdir())

@pytest.mark.parametrize("coverage,rows,expected",[
    ("object_missing",[],"verified_path_object_missing"),
    ("decode_limit",[],"decode_limit_skipped"),
    ("partial_row_groups",[{}],"partially_read"),
    ("join_column_missing",[],"link_not_established"),
    ("selected_object_only",[],"verified_object_no_matching_rows"),
    ("selected_object_only",[{}],"selected_rows_present"),
    ("unknown",[],"unavailable_for_verification")])
def test_distinct_coverage(coverage,rows,expected):
    assert m.state(dict(coverage=coverage,rows=rows))==expected

def test_point_conflicts_are_not_silenced():
    row=dict(set_num=1,game_num=1,point_num=2,server=1,point_winner=2,
             server_won_point=True,score_server="15",score_returner="0",is_tiebreak=False)
    codes={f["code"] for f in m.check_points([row,row])["findings"]}
    assert {"duplicate_point","point_sequence_gap","server_winner_disagreement","score_winner_disagreement"}<=codes

def test_score_candidates_never_label_retirement_or_incomplete():
    sets=[dict(score_team1=6,score_team2=2),dict(score_team1=6,score_team2=3)]
    assert m.score_candidate(sets,3)==1
    assert m.score_candidate(sets,9) is None
    assert m.score_candidate([dict(score_team1=2,score_team2=1)],3) is None
    assert not m.odds("nan",2) and not m.odds(None,2)
    assert m.odds("1.5","500") and not m.odds("1",2)

def test_duplicate_json_rejected():
    with pytest.raises(ValueError): json.loads('{"a":1,"a":2}',object_pairs_hook=m.unique)
