from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.interface_catalog import generate_catalogs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate static module interface catalogs from existing GME tests."
    )
    parser.add_argument("--gme-root", required=True, help="Path to the GME superproject checkout")
    parser.add_argument(
        "--acis-symbol-dir",
        required=True,
        help="Directory containing the official *_acis_symbol.csv files",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="Module to generate; repeat for multiple modules. Omit to discover all test modules.",
    )
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "backend" / "gme_agent" / "interface_catalog" / "catalogs"),
        help="Directory for generated <module>.json files",
    )
    args = parser.parse_args()

    generated = generate_catalogs(
        args.gme_root,
        args.acis_symbol_dir,
        args.module,
        args.output_root,
    )
    print(
        json.dumps(
            [
                {
                    "module": catalog["module"],
                    "output": str(path),
                    "summary": catalog["summary"],
                }
                for path, catalog in generated
            ],
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
