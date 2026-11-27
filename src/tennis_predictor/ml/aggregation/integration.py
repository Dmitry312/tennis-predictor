"""Canonical-to-store boundary for future live callers, without deployment hooks."""

from tennis_predictor.ml.canonical_layer.evidence import fingerprint
from tennis_predictor.ml.canonical_layer.storage import transform_code_hash
from tennis_predictor.ml.canonical_layer.transform import MatchTransform

from .batch import INPUT_TABLES
from .contracts import MatchInput


def live_match_input(bundle, config, identities, *, source_ref=None):
    """Resolve a single observed bundle using the same pinned policy as batch."""
    transform = MatchTransform(bundle, config)
    bindings = identities.resolve(
        transform.meta, source_ref or transform.ref(transform.metadata, 0)
    )
    transform.identity_bindings = bindings
    tables = transform.run()
    revision = fingerprint(
        [transform_code_hash(), bundle.manifest["bundle_digest"], config, bindings]
    )
    return MatchInput(bundle.match_id, revision, {key: tables[key] for key in INPUT_TABLES})
