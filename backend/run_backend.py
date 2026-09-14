from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "backend"))

    from gme_agent.api.server import run_server

    parser = argparse.ArgumentParser(description="Run the GME Test Agent backend.")
    parser.add_argument("--config", default=str(repo_root / "config.local.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--api-token", default=os.environ.get("GME_AGENT_API_TOKEN", ""))
    args = parser.parse_args()

    run_server(config_path=Path(args.config), host=args.host, port=args.port, api_token=args.api_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
