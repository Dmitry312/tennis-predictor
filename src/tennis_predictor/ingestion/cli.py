import argparse
import logging

from tennis_predictor.ingestion.nbbet.results import NbBetResultsCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

COLLECTORS = {
    "nbbet-results": NbBetResultsCollector,
    # "nbbet-pbp": ..., "championat": ..., "rankings": ... — по мере готовности
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("collector", choices=COLLECTORS.keys())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    collector = COLLECTORS[args.collector]()
    result = collector.run(dry_run=args.dry_run)
    if result.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
