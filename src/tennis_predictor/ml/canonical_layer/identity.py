"""A pinned registry projection shared by batch and single-match processing."""

from tennis_predictor.ml.identity_review.registry import ReviewedMappings


class PinnedIdentities:
    def __init__(self, registry, revision=None):
        self.registry = ReviewedMappings(registry)
        self.revision, self.token = self.registry.pin(revision)

    def resolve(self, metadata, source_ref):
        row = {**metadata, "source_ref": source_ref}
        matches = self.registry.resolve_match(
            row, source="nbbet", revision=self.revision, expected_token=self.token
        )
        # Snapshot-level provenance names the pinned revision. Keep only effective
        # evidence per shard so unrelated alias corrections can reuse it.
        return tuple(
            {k: v for k, v in match.items() if k not in ("registry_revision", "registry_token")}
            for match in matches
        )
