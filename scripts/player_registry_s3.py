"""Explicitly publish, restore or cache a compact player registry on existing S3."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tennis_predictor.config import get_settings
from tennis_predictor.ml.canonical_layer.evidence import encoded
from tennis_predictor.ml.identity_review.publication import RegistryPublication
from tennis_predictor.storage.s3_client import get_s3_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket")
    parser.add_argument("--prefix", default="ml/player-registry/v1")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("registry", type=Path)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("token")
    for command in (publish, rollback):
        command.add_argument("--confirm", action="store_true", required=True)
        command.add_argument("--expected-current", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("destination", type=Path)
    fetch.add_argument("--token")
    args = parser.parse_args()
    publication = RegistryPublication(
        get_s3_client(), args.bucket or get_settings().s3_bucket, args.prefix
    )
    if args.command == "publish":
        result = publication.publish(args.registry, expected_current=args.expected_current)
    elif args.command == "rollback":
        result = publication.rollback(args.token, expected_current=args.expected_current)
    else:
        result = publication.fetch(args.destination, args.token)
    sys.stdout.buffer.write(encoded(result))


if __name__ == "__main__":
    main()
