"""Standing owner policy, not a per-batch acknowledgement or third-party licence."""
from pathlib import Path
from scripts import export_match_samples as e

POLICY_ID="owner-public-sports-2026-11-26"
PATH=Path(__file__).resolve().parents[1]/"docs/ml/data_catalog/publication-policy.json"


def standing_policy():
    policy=e.read_json(PATH,16384)
    e.need(policy.get("policy_id")==POLICY_ID and
           policy.get("retain_original_sports_values") is True and
           policy.get("per_batch_manual_attestations") is False and
           policy.get("content_guard")=="required_fail_closed" and
           policy.get("third_party_redistribution_rights")==
               "not_certified_by_repository_owner_permission","publication_policy_invalid")
    e.inspect_content(policy)
    return policy
