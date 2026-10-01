"""
SQLancer (https://github.com/sqlancer/sqlancer) as a workload: it generates random databases and queries, and checks
the query results with its test oracles (NoREC by default).

What SQLancer reports is classified by replaying it on a vanilla Postgres (the same binaries, on local storage): its
log holds the statements that built the database and, as comments, the queries the oracle compared. Both are replayed
on Neon and on the vanilla Postgres. When the queries give the same results on both, the report is about Postgres
itself and only a warning; when they differ, Neon's storage changed the results, which fails the test.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg2

from .common import Problems, Target, Workload, java, run_logged

if TYPE_CHECKING:
    from fixtures.neon_fixtures import PgProtocol

# SQLancer always connects to this database first
ENTRY_DATABASE = "test"
# The queries an oracle compared, logged as comments
ORACLE_QUERY = re.compile(r"^--\s*(?:optimized:|unoptimized:)?\s*((?:SELECT|WITH|VALUES)\b.*;)\s*$", re.IGNORECASE)
REPLAY_STATEMENT_TIMEOUT = "30s"


class SQLancer(Workload):
    name = "sqlancer"

    def __init__(self, work_dir: Path, duration: int, vanilla: PgProtocol):
        super().__init__(work_dir, duration)
        self.jar = os.environ.get("SQLANCER_JAR", "/opt/sqlancer/sqlancer.jar")
        self.oracles = os.environ.get("SQLANCER_ORACLES", "NOREC").split(",")
        self.threads = int(os.environ.get("SQLANCER_THREADS", "2"))
        self.vanilla = vanilla
        self.replays = 0
        self.vanilla.safe_psql(f"CREATE DATABASE {ENTRY_DATABASE}")

    def prepare(self, target: Target):
        target.endpoint.safe_psql(f"CREATE DATABASE {ENTRY_DATABASE}")

    def run(self, target: Target, phase: str, seed: int) -> Problems:
        run_dir = self.work_dir / f"sqlancer-{target.label}-{phase}"
        run_dir.mkdir(parents=True)
        cmd = [
            *java(), "-jar", self.jar,
            "--host", "127.0.0.1", "--port", str(target.port), "--username", "cloud_admin", "--password", "",
            "--num-threads", str(self.threads), "--timeout-seconds", str(self.duration), "--random-seed", str(seed),
            # Each thread keeps (re)creating its database `<prefix><thread>`, the last ones remain
            "--database-prefix", f"sq_{target.label}_{phase}_",
            "--num-tries", "10",
            "postgres", "--test-tablespaces", "false",
            *[arg for oracle in self.oracles for arg in ("--oracle", oracle)],
        ]  # fmt: skip
        rc = run_logged(cmd, run_dir / "sqlancer.log", cwd=run_dir, timeout=self.duration + 300)
        reports = sorted(p for p in (run_dir / "logs").glob("*/*.log") if not p.name.endswith("-cur.log"))
        problems = Problems()
        for report in reports:
            problems.extend(self.classify(target, report))
        if rc != 0 and not reports:
            problems.failures.append(
                f"SQLancer on {target.label} ({phase}) exited with {rc} (see {run_dir}/sqlancer.log)"
            )
        return problems

    def classify(self, target: Target, report: Path) -> Problems:
        """Replays a SQLancer report on Neon and the vanilla Postgres, and compares the oracle's queries' results."""
        problems = Problems()
        text = report.read_text()
        summary = next(
            (line[2:].strip() for line in text.splitlines() if line.startswith("--") and line[2:].strip()), ""
        )
        database = report.stem
        self.replays += 1
        replay_database = f"replay_{self.replays}_{target.label}"
        statements, queries = parse_report(text, database, replay_database)
        what = f"SQLancer report {report} ({summary[:200]})"
        if not queries:
            problems.failures.append(f"{what}: could not find its queries to compare with a vanilla Postgres")
            return problems

        neon_results = replay(target.endpoint, statements, queries, replay_database)
        vanilla_results = replay(self.vanilla, statements, queries, replay_database)
        (report.parent / f"{report.stem}.replay.txt").write_text(
            "".join(f"{q}\n  neon:    {n}\n  vanilla: {v}\n" for q, n, v in zip(queries, neon_results, vanilla_results))
        )
        if neon_results == vanilla_results:
            problems.warnings.append(f"{what}: reproduces on a vanilla Postgres")
        else:
            problems.failures.append(f"{what}: Neon's results differ from a vanilla Postgres's")
        return problems


def parse_report(text: str, database: str, replay_database: str) -> tuple[list[str], list[str]]:
    """The statements (with the database renamed) and the oracle's queries of a SQLancer report."""
    rename = re.compile(rf"\b{re.escape(database)}\b")
    statements, queries = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("--"):
            m = ORACLE_QUERY.match(line)
            if m:
                queries.append(m.group(1))
            continue
        statements.append(rename.sub(replay_database, line))
    return statements, queries


def replay(server: PgProtocol, statements: list[str], queries: list[str], replay_database: str) -> list[str]:
    """Runs the statements (ignoring errors, as SQLancer does) and returns each query's result or error."""
    conn = server.connect(dbname="postgres")
    try:
        for statement in statements:
            if statement.startswith("\\c "):
                conn.close()
                conn = server.connect(dbname=statement[3:].strip().rstrip(";"))
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = '{REPLAY_STATEMENT_TIMEOUT}'")
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(statement)
            except psycopg2.Error:
                pass
        results = []
        for query in queries:
            try:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall() if cur.description else []
                results.append(repr(sorted(map(repr, rows))))
            except psycopg2.Error as e:
                results.append(f"ERROR {e.pgcode}: {str(e).splitlines()[0] if str(e) else ''}")
        return results
    finally:
        conn.close()
        with server.cursor(dbname="postgres") as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {replay_database} WITH (FORCE)")
