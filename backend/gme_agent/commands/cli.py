from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import json
import time

from ..services.orchestrator import Orchestrator
from ..settings.config import load_config
from ..settings.validation import validate_config
from ..storage.db import AgentDb


TERMINAL_STATUSES = {"needs_review", "failed", "pr_created"}


def main(repo_root: Path) -> int:
    parser = argparse.ArgumentParser(description="GME Test Agent CLI")
    parser.add_argument("--config", default=str(repo_root / "config.local.json"))

    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-test", help="Create and run a test-generation job")
    create.add_argument("--module", required=True)
    create.add_argument("--api", default="")

    run_tests = sub.add_parser("run-tests", help="Run tests for an existing job")
    run_tests.add_argument("job_id")
    run_tests.add_argument("--filter", default="*")

    build = sub.add_parser("build", help="Configure and build an existing job")
    build.add_argument("job_id")

    create_pr = sub.add_parser("create-pr", help="Create a draft PR for an existing job")
    create_pr.add_argument("job_id")

    cleanup = sub.add_parser("cleanup", help="Remove an existing job worktree")
    cleanup.add_argument("job_id")

    fix = sub.add_parser("fix", help="Create and run a bug-fix job for a failure")
    fix.add_argument("failure_id")

    mark = sub.add_parser("mark-failure", help="Update a failure status")
    mark.add_argument("failure_id")
    mark.add_argument("status", choices=["open", "fixing", "fix_ready", "fixed", "ignored", "fix_failed"])

    sub.add_parser("jobs", help="List jobs")
    sub.add_parser("failures", help="List failures")
    sub.add_parser("validate", help="Validate config and local tools")

    args = parser.parse_args()
    config = load_config(Path(args.config))
    db = AgentDb(config.database_path)
    orchestrator = Orchestrator(config, db)

    try:
        if args.command == "create-test":
            job = orchestrator.create_test_generation_job(args.module, args.api)
            return _print_and_wait(db, job)
        if args.command == "run-tests":
            job = orchestrator.run_tests_for_job(args.job_id, args.filter)
            return _print_and_wait(db, job)
        if args.command == "build":
            job = orchestrator.build_job(args.job_id)
            return _print_and_wait(db, job)
        if args.command == "create-pr":
            job = orchestrator.create_pr_for_job(args.job_id)
            return _print_and_wait(db, job)
        if args.command == "cleanup":
            job = orchestrator.cleanup_job_worktree(args.job_id)
            return _print_and_wait(db, job)
        if args.command == "fix":
            job = orchestrator.create_fix_job(args.failure_id)
            return _print_and_wait(db, job)
        if args.command == "mark-failure":
            print(json.dumps(orchestrator.update_failure_status(args.failure_id, args.status), indent=2, ensure_ascii=False))
            return 0
        if args.command == "jobs":
            print(json.dumps(db.list_jobs(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "failures":
            print(json.dumps(db.list_failures(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "validate":
            result = validate_config(config)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 1
    finally:
        db.close()

    parser.error(f"Unknown command: {args.command}")
    return 2


def _print_and_wait(db: AgentDb, job: dict[str, Any]) -> int:
    print(json.dumps(job, indent=2, ensure_ascii=False))
    job_id = job["id"]
    seen_event = 0
    while True:
        for event in db.list_events(job_id, after_id=seen_event):
            seen_event = max(seen_event, int(event["id"]))
            print(f"[{event['ts']}] {event['level']}: {event['message']}")

        current = db.get_job(job_id)
        if current["status"] in TERMINAL_STATUSES:
            print(json.dumps(current, indent=2, ensure_ascii=False))
            return 1 if current["status"] == "failed" else 0
        time.sleep(1.0)
