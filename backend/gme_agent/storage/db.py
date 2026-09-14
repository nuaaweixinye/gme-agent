from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import threading
import time


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class AgentDb:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                create table if not exists jobs (
                    id text primary key,
                    type text not null,
                    status text not null,
                    title text not null,
                    module text,
                    api_name text,
                    branch text,
                    worktree_path text,
                    harness_session_id text,
                    created_at text not null,
                    updated_at text not null,
                    metadata_json text not null default '{}',
                    error text
                );

                create table if not exists events (
                    id integer primary key autoincrement,
                    job_id text not null,
                    ts text not null,
                    level text not null,
                    message text not null
                );

                create table if not exists failures (
                    id text primary key,
                    job_id text not null,
                    status text not null,
                    test_suite text,
                    test_name text,
                    file text,
                    line integer,
                    reason text,
                    reproduce_command text,
                    skip_id text,
                    created_at text not null,
                    updated_at text not null,
                    metadata_json text not null default '{}'
                );

                create table if not exists failure_observations (
                    id integer primary key autoincrement,
                    run_id text not null,
                    failure_id text not null,
                    job_id text not null,
                    outcome text not null,
                    test_suite text,
                    test_name text,
                    file text,
                    line integer,
                    reason text,
                    gtest_filter text,
                    observed_at text not null,
                    metadata_json text not null default '{}'
                );

                create table if not exists test_case_results (
                    job_id text not null,
                    test_suite text not null,
                    test_name text not null,
                    status text not null,
                    run_id text not null,
                    gtest_filter text,
                    updated_at text not null,
                    primary key (job_id, test_suite, test_name)
                );
                """
            )
            self._deduplicate_failures()
            self._conn.execute(
                "create unique index if not exists failures_test_identity "
                "on failures (job_id, test_suite, test_name)"
            )
            self._conn.execute(
                "create index if not exists failure_observations_failure_id "
                "on failure_observations (failure_id, observed_at)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_job(
        self,
        *,
        job_id: str,
        job_type: str,
        title: str,
        module: str | None = None,
        api_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = now_ts()
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into jobs (
                    id, type, status, title, module, api_name, created_at,
                    updated_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    "queued",
                    title,
                    module,
                    api_name,
                    ts,
                    ts,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_job(job_id)

        fields["updated_at"] = now_ts()
        assignments = []
        values = []
        for key, value in fields.items():
            if key == "metadata":
                assignments.append("metadata_json = ?")
                values.append(json.dumps(value, ensure_ascii=False))
            elif key in {
                "status",
                "title",
                "module",
                "api_name",
                "branch",
                "worktree_path",
                "harness_session_id",
                "error",
                "updated_at",
            }:
                assignments.append(f"{key} = ?")
                values.append(value)
        values.append(job_id)
        with self._lock, self._conn:
            self._conn.execute(f"update jobs set {', '.join(assignments)} where id = ?", values)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("select * from jobs where id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_row(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("select * from jobs order by created_at desc, id desc").fetchall()
        return [self._job_row(row) for row in rows]

    def delete_job(self, job_id: str) -> dict[str, int]:
        with self._lock, self._conn:
            deleted_events = self._conn.execute("delete from events where job_id = ?", (job_id,)).rowcount
            deleted_observations = self._conn.execute(
                "delete from failure_observations where job_id = ?", (job_id,)
            ).rowcount
            deleted_test_results = self._conn.execute(
                "delete from test_case_results where job_id = ?", (job_id,)
            ).rowcount
            deleted_failures = self._conn.execute("delete from failures where job_id = ?", (job_id,)).rowcount
            deleted_jobs = self._conn.execute("delete from jobs where id = ?", (job_id,)).rowcount
        return {
            "jobs": deleted_jobs,
            "events": deleted_events,
            "failures": deleted_failures,
            "failure_observations": deleted_observations,
            "test_case_results": deleted_test_results,
        }

    def delete_open_failures_for_job(self, job_id: str) -> int:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "select id from failures where job_id = ? and status = 'open'", (job_id,)
            ).fetchall()
            return self._delete_failure_ids([str(row["id"]) for row in rows])

    def delete_open_failures_for_tests(self, job_id: str, test_keys: list[tuple[str, str]]) -> int:
        if not test_keys:
            return 0
        deleted = 0
        with self._lock, self._conn:
            for suite, test in test_keys:
                rows = self._conn.execute(
                    """
                    select id from failures
                    where job_id = ? and status = 'open' and test_suite = ? and test_name = ?
                    """,
                    (job_id, suite, test),
                ).fetchall()
                deleted += self._delete_failure_ids([str(row["id"]) for row in rows])
        return deleted

    def delete_failures_for_tests(self, job_id: str, test_keys: list[tuple[str, str]]) -> int:
        if not test_keys:
            return 0
        deleted = 0
        with self._lock, self._conn:
            for suite, test in test_keys:
                rows = self._conn.execute(
                    """
                    select id from failures
                    where job_id = ? and test_suite = ? and test_name = ?
                    """,
                    (job_id, suite, test),
                ).fetchall()
                deleted += self._delete_failure_ids([str(row["id"]) for row in rows])
        return deleted

    def _delete_failure_ids(self, failure_ids: list[str]) -> int:
        ids = list(dict.fromkeys(failure_ids))
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        self._conn.execute(
            f"delete from failure_observations where failure_id in ({placeholders})", ids
        )
        return self._conn.execute(
            f"delete from failures where id in ({placeholders})", ids
        ).rowcount

    def add_event(self, job_id: str, level: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "insert into events (job_id, ts, level, message) values (?, ?, ?, ?)",
                (job_id, now_ts(), level, message),
            )

    def list_events(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from events where job_id = ? and id > ? order by id asc",
                (job_id, after_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_failure(
        self,
        *,
        failure_id: str,
        job_id: str,
        test_suite: str = "",
        test_name: str = "",
        file: str = "",
        line: int | None = None,
        reason: str = "",
        reproduce_command: str = "",
        skip_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.upsert_failure(
            failure_id=failure_id,
            job_id=job_id,
            test_suite=test_suite,
            test_name=test_name,
            file=file,
            line=line,
            reason=reason,
            reproduce_command=reproduce_command,
            skip_id=skip_id,
            metadata=metadata,
        )

    def upsert_failure(
        self,
        *,
        failure_id: str,
        job_id: str,
        test_suite: str = "",
        test_name: str = "",
        file: str = "",
        line: int | None = None,
        reason: str = "",
        reproduce_command: str = "",
        skip_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = now_ts()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                select * from failures
                where job_id = ? and test_suite = ? and test_name = ?
                """,
                (job_id, test_suite, test_name),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    insert into failures (
                        id, job_id, status, test_suite, test_name, file, line, reason,
                        reproduce_command, skip_id, created_at, updated_at, metadata_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        failure_id,
                        job_id,
                        "open",
                        test_suite,
                        test_name,
                        file,
                        line,
                        reason,
                        reproduce_command,
                        skip_id or failure_id,
                        ts,
                        ts,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                stable_id = failure_id
            else:
                stable_id = str(row["id"])
                existing_metadata = json.loads(row["metadata_json"] or "{}")
                existing_metadata.update(metadata or {})
                status = "open" if row["status"] == "resolved" else row["status"]
                self._conn.execute(
                    """
                    update failures
                    set status = ?, file = ?, line = ?, reason = ?, reproduce_command = ?,
                        skip_id = ?, updated_at = ?, metadata_json = ?
                    where id = ?
                    """,
                    (
                        status,
                        file,
                        line,
                        reason,
                        reproduce_command,
                        row["skip_id"] or skip_id or stable_id,
                        ts,
                        json.dumps(existing_metadata, ensure_ascii=False),
                        stable_id,
                    ),
                )
        return self.get_failure(stable_id)

    def add_failure_observation(
        self,
        *,
        run_id: str,
        failure_id: str,
        job_id: str,
        outcome: str,
        test_suite: str = "",
        test_name: str = "",
        file: str = "",
        line: int | None = None,
        reason: str = "",
        gtest_filter: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        observed_at = now_ts()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                insert into failure_observations (
                    run_id, failure_id, job_id, outcome, test_suite, test_name,
                    file, line, reason, gtest_filter, observed_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    failure_id,
                    job_id,
                    outcome,
                    test_suite,
                    test_name,
                    file,
                    line,
                    reason,
                    gtest_filter,
                    observed_at,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            observation_id = int(cursor.lastrowid)
            row = self._conn.execute(
                "select * from failure_observations where id = ?", (observation_id,)
            ).fetchone()
        return self._observation_row(row)

    def list_failure_observations(self, failure_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                select * from failure_observations
                where failure_id = ?
                order by observed_at desc, id desc
                """,
                (failure_id,),
            ).fetchall()
        return [self._observation_row(row) for row in rows]

    def upsert_test_case_result(
        self,
        *,
        job_id: str,
        test_suite: str,
        test_name: str,
        status: str,
        run_id: str,
        gtest_filter: str = "",
    ) -> dict[str, Any]:
        if status not in {"passed", "failed", "skipped"}:
            raise ValueError(f"Unsupported test result status: {status}")
        updated_at = now_ts()
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into test_case_results (
                    job_id, test_suite, test_name, status, run_id, gtest_filter, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(job_id, test_suite, test_name) do update set
                    status = excluded.status,
                    run_id = excluded.run_id,
                    gtest_filter = excluded.gtest_filter,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    test_suite,
                    test_name,
                    status,
                    run_id,
                    gtest_filter,
                    updated_at,
                ),
            )
            row = self._conn.execute(
                """
                select * from test_case_results
                where job_id = ? and test_suite = ? and test_name = ?
                """,
                (job_id, test_suite, test_name),
            ).fetchone()
        return dict(row)

    def list_test_case_results(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                select * from test_case_results
                where job_id = ?
                order by test_suite, test_name
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_test_case_results_for_tests(
        self,
        job_id: str,
        test_keys: list[tuple[str, str]],
    ) -> int:
        deleted = 0
        with self._lock, self._conn:
            for suite, test in test_keys:
                deleted += self._conn.execute(
                    """
                    delete from test_case_results
                    where job_id = ? and test_suite = ? and test_name = ?
                    """,
                    (job_id, suite, test),
                ).rowcount
        return deleted

    def update_failure(self, failure_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_ts()
        assignments = []
        values = []
        for key, value in fields.items():
            if key == "metadata":
                assignments.append("metadata_json = ?")
                values.append(json.dumps(value, ensure_ascii=False))
            elif key in {
                "status",
                "test_suite",
                "test_name",
                "file",
                "line",
                "reason",
                "reproduce_command",
                "skip_id",
                "updated_at",
            }:
                assignments.append(f"{key} = ?")
                values.append(value)
        values.append(failure_id)
        with self._lock, self._conn:
            self._conn.execute(f"update failures set {', '.join(assignments)} where id = ?", values)
        return self.get_failure(failure_id)

    def get_failure(self, failure_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("select * from failures where id = ?", (failure_id,)).fetchone()
        if row is None:
            raise KeyError(failure_id)
        return self._failure_row(row)

    def list_failures(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("select * from failures order by created_at desc, id desc").fetchall()
        return [self._failure_row(row) for row in rows]

    def _deduplicate_failures(self) -> None:
        groups = self._conn.execute(
            """
            select job_id, test_suite, test_name
            from failures
            group by job_id, test_suite, test_name
            having count(*) > 1
            """
        ).fetchall()
        replacements: dict[str, str] = {}
        status_priority = {
            "fixed": 60,
            "fix_ready": 50,
            "fixing": 40,
            "fix_failed": 30,
            "ignored": 20,
            "resolved": 15,
            "open": 10,
        }
        for group in groups:
            rows = self._conn.execute(
                """
                select * from failures
                where job_id = ? and test_suite is ? and test_name is ?
                """,
                (group["job_id"], group["test_suite"], group["test_name"]),
            ).fetchall()
            canonical = max(
                rows,
                key=lambda row: (
                    bool(json.loads(row["metadata_json"] or "{}").get("fix_job_id")),
                    status_priority.get(str(row["status"]), 0),
                    str(row["updated_at"]),
                ),
            )
            latest = max(rows, key=lambda row: str(row["updated_at"]))
            metadata: dict[str, Any] = {}
            for row in sorted(rows, key=lambda item: str(item["updated_at"])):
                metadata.update(json.loads(row["metadata_json"] or "{}"))
            canonical_id = str(canonical["id"])
            duplicate_ids = [str(row["id"]) for row in rows if row["id"] != canonical_id]
            self._conn.execute(
                """
                update failures
                set file = ?, line = ?, reason = ?, reproduce_command = ?, skip_id = ?,
                    created_at = ?, updated_at = ?, metadata_json = ?
                where id = ?
                """,
                (
                    latest["file"],
                    latest["line"],
                    latest["reason"],
                    latest["reproduce_command"],
                    canonical["skip_id"] or canonical_id,
                    min(str(row["created_at"]) for row in rows),
                    max(str(row["updated_at"]) for row in rows),
                    json.dumps(metadata, ensure_ascii=False),
                    canonical_id,
                ),
            )
            for duplicate_id in duplicate_ids:
                replacements[duplicate_id] = canonical_id
                self._conn.execute("delete from failures where id = ?", (duplicate_id,))
        if replacements:
            self._replace_failure_ids_in_job_metadata(replacements)

    def _replace_failure_ids_in_job_metadata(self, replacements: dict[str, str]) -> None:
        def replace(value: Any) -> Any:
            if isinstance(value, str):
                return replacements.get(value, value)
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        rows = self._conn.execute("select id, metadata_json from jobs").fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            updated = replace(metadata)
            if updated != metadata:
                self._conn.execute(
                    "update jobs set metadata_json = ? where id = ?",
                    (json.dumps(updated, ensure_ascii=False), row["id"]),
                )

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    @staticmethod
    def _failure_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    @staticmethod
    def _observation_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data
