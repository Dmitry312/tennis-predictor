"""S3 protocol and local cache tests; no AWS connection."""

from contextlib import closing
from io import BytesIO
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from tennis_predictor.ml.canonical_layer.evidence import digest
from tennis_predictor.ml.identity_review import staging
from tennis_predictor.ml.identity_review.bulk import import_approved_groups
from tennis_predictor.ml.identity_review.publication import RegistryPublication, read_registry
from tests.test_bulk_identity_integration import packet
from tests.test_identity_review import Source, match


def failure(code):
    return ClientError({"Error": {"Code": code}}, "test")


class LocalS3:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise failure("NoSuchKey")
        body = self.objects[Key]
        return {"Body": BytesIO(body), "ETag": digest(body)}

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        current = self.objects.get(Key)
        if (IfMatch and (current is None or digest(current) != IfMatch)) or (
            IfNoneMatch == "*" and current is not None
        ):
            raise failure("PreconditionFailed")
        if not (IfMatch or IfNoneMatch):
            raise AssertionError("Unconditional writes are forbidden")
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        self.writes.append(Key)


def test_verified_publish_fetch_rollback_and_idempotence(tmp_path):
    stage, registry = tmp_path / "evidence.sqlite", tmp_path / "registry.sqlite"
    dataset = staging.extract(stage, Source([match(1)]))["dataset"]
    with closing(staging.connection(stage)) as db:
        groups = {
            row[0]: f"player-{i}"
            for i, row in enumerate(
                db.execute("SELECT DISTINCT group_id FROM observations ORDER BY group_id")
            )
        }
    first = import_approved_groups(stage, registry, packet(dataset, groups))
    s3 = LocalS3()
    publication = RegistryPublication(s3, "test", "registry/test")
    one = publication.publish(registry, expected_current="root")
    assert one["token"] == first["token"] and s3.writes[-1].endswith("/CURRENT.json")
    writes = list(s3.writes)
    assert publication.publish(registry, expected_current="root") == one
    assert s3.writes == writes
    cache = tmp_path / "cache" / "registry.sqlite"
    assert publication.fetch(cache) == one
    original = cache.read_bytes()

    groups.pop(next(iter(groups)))
    second = import_approved_groups(stage, registry, packet(dataset, groups, first["token"]))
    two = publication.publish(registry, expected_current=one["token"])
    assert two["token"] == second["token"]
    with pytest.raises(ValueError, match="expected predecessor"):
        publication.rollback(one["token"], expected_current=one["token"])
    blob = f"registry/test/snapshots/{two['sha256']}.sqlite"
    s3.objects[blob] = b"corrupt"
    with pytest.raises(ValueError, match="integrity failure"):
        publication.fetch(cache)
    assert cache.read_bytes() == original
    assert publication.rollback(one["token"], expected_current=two["token"]) == one
    assert publication.fetch(cache) == one
    assert read_registry(cache) == (1, one["token"])


def test_pointer_update_fails_closed_on_concurrent_write(tmp_path):
    stage, registry = tmp_path / "evidence.sqlite", tmp_path / "registry.sqlite"
    dataset = staging.extract(stage, Source([match(1)]))["dataset"]
    import_approved_groups(
        stage,
        registry,
        {
            "dataset": dataset,
            "parent": "root",
            "recorded_at": "2026-11-27T10:37:00+03:00",
            "approval_ref": "fixture",
        },
        expected={"players_by_gender": {"M": 2}, "aliases": 2, "observations": 2, "matches": 1},
    )
    s3 = LocalS3()
    publication = RegistryPublication(s3, "test", "test")
    real_put = s3.put_object

    def lose_cas(**kwargs):
        if kwargs["Key"].endswith("CURRENT.json"):
            raise failure("PreconditionFailed")
        real_put(**kwargs)

    s3.put_object = Mock(side_effect=lose_cas)
    with pytest.raises(ClientError, match="PreconditionFailed"):
        publication.publish(registry, expected_current="root")
    assert not any(key.endswith("CURRENT.json") for key in s3.objects)
