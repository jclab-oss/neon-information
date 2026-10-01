"""
Shared parts of the workload scenario (test_workload.py): the workload interface and the data checks.
"""

from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fixtures.log_helper import log

if TYPE_CHECKING:
    from collections.abc import Callable

    from fixtures.neon_fixtures import Endpoint

# Tables the scenario owns, in the `postgres` database of every tenant
MARKER_TABLE = "neon_info_marker"
REFERENCE_TABLE = "neon_info_reference"
REFERENCE_ROWS = 100_000


@dataclass
class Target:
    """A place a workload runs: an endpoint on a branch of a tenant."""

    tenant: str  # label, e.g. `t1`
    branch: str  # `main`, `new_branch`, ...
    endpoint: Endpoint

    @property
    def label(self) -> str:
        return f"{self.tenant}_{self.branch}"

    @property
    def port(self) -> int:
        return self.endpoint.default_options["port"]


@dataclass
class Problems:
    """Findings of a check: `failures` fail the test, `warnings` are reported only."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: Problems):
        self.failures.extend(other.failures)
        self.warnings.extend(other.warnings)


class Workload:
    """A workload of the scenario. Each method gets the target to work on; `run` blocks until it's done."""

    name = "workload"

    def __init__(self, work_dir: Path, duration: int):
        self.work_dir = work_dir
        self.duration = duration

    def prepare(self, target: Target):
        """Prepares a tenant's main branch (phase 1), before its first `run`."""

    def run(self, target: Target, phase: str, seed: int) -> Problems:
        raise NotImplementedError

    def check(self, target: Target) -> Problems:
        """Checks the workload's own invariants on the target's data."""
        return Problems()


def java() -> list[str]:
    """The Java command, with a bounded heap: several tools and computes share the machine."""
    path = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if "JAVA_HOME" in os.environ else "java"
    return [path, f"-Xmx{os.environ.get('WORKLOAD_JAVA_HEAP', '768m')}"]


def run_logged(cmd: list[str], log_path: Path, cwd: Path | None = None, timeout: float | None = None) -> int:
    """Runs a command with its output in `log_path`, returns its exit code."""
    log.info(f"Running {cmd} (output in {log_path})")
    with log_path.open("w") as out:
        # The tools are Java programs: JAVA_TOOL_OPTIONS of the environment would only add noise
        env = {k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"}
        try:
            return subprocess.run(
                cmd, stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env, timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            out.write(f"\n*** timed out after {timeout}s\n")
            return -1


def in_parallel(fn: Callable[[Target], Any], targets: list[Target]) -> list[Any]:
    """Runs `fn` for every target at the same time, returns the results in order (re-raising failures)."""
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(fn, targets))


# fingerprints


def databases(endpoint: Endpoint) -> list[str]:
    return [
        r[0]
        for r in endpoint.safe_psql(
            "SELECT datname FROM pg_database WHERE datallowconn AND datname NOT IN ('template0', 'template1') "
            "ORDER BY datname"
        )
    ]


# Permanent user tables (unlogged ones are emptied when the compute restarts) and materialized views
TABLES_QUERY = """
SELECT format('%I.%I', n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm') AND c.relpersistence = 'p'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1
"""


def table_fingerprint_query(table: str) -> str:
    """A table's row count and the sum of its rows' hashes (60 bits of their text's md5), which ignores their order."""
    return f"SELECT count(*), coalesce(sum(('x' || left(md5(t::text), 15))::bit(60)::bigint), 0) FROM {table} t"


def fingerprint(endpoint: Endpoint) -> dict[str, str]:
    """
    The logical content of every database of the endpoint, as `{"<database>/<table>": "<rows> <hash sum>"}`.
    """
    result = {}
    for db in databases(endpoint):
        with endpoint.cursor(dbname=db) as cur:
            cur.execute(TABLES_QUERY)
            for (table,) in cur.fetchall():
                cur.execute(table_fingerprint_query(table))
                count, digest = cur.fetchone()
                result[f"{db}/{table}"] = f"{count} {digest}"
    return result


def fingerprints(endpoints: list[Endpoint]) -> list[dict[str, str]]:
    """The fingerprints of several endpoints, computed at the same time."""
    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
        return list(pool.map(fingerprint, endpoints))


def diff_fingerprints(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    """Human-readable differences between two fingerprints (empty if equal)."""
    diffs = []
    for key in sorted(set(expected) | set(actual)):
        if expected.get(key) != actual.get(key):
            diffs.append(f"{key}: expected {expected.get(key, 'nothing')}, got {actual.get(key, 'nothing')}")
    return diffs


# scenario tables


def create_scenario_tables(endpoint: Endpoint):
    endpoint.safe_psql_many(
        [
            f"CREATE TABLE {MARKER_TABLE} (tenant text, branch text, phase text)",
            f"CREATE TABLE {REFERENCE_TABLE} AS "
            f"SELECT i, md5(i::text) AS v FROM generate_series(1, {REFERENCE_ROWS}) i",
        ]
    )


def mark(target: Target, phase: str):
    target.endpoint.safe_psql(f"INSERT INTO {MARKER_TABLE} VALUES ('{target.tenant}', '{target.branch}', '{phase}')")


def markers(endpoint: Endpoint) -> set[tuple[str, str, str]]:
    return {tuple(r) for r in endpoint.safe_psql(f"SELECT tenant, branch, phase FROM {MARKER_TABLE}")}


def reference_fingerprint(endpoint: Endpoint) -> str:
    return fingerprint_of_table(endpoint, "postgres", f"public.{REFERENCE_TABLE}")


def fingerprint_of_table(endpoint: Endpoint, db: str, table: str) -> str:
    count, digest = endpoint.safe_psql(table_fingerprint_query(table), dbname=db)[0]
    return f"{count} {digest}"


_lock = threading.Lock()


def note(lines: list[str], text: str):
    """Appends a line to a shared list from the workload threads."""
    with _lock:
        lines.append(text)
