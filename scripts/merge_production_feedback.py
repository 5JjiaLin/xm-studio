"""Merge local testing exports: python -m scripts.merge_production_feedback *.zip --output report-dir."""

import json
from argparse import ArgumentParser
from pathlib import Path

from lib.production_feedback.export import merge_packages


def main():
    parser = ArgumentParser(description="Merge XM_Studio production testing evidence")
    parser.add_argument("packages", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(merge_packages(args.packages, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
