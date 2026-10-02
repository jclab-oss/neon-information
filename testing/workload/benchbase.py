"""
BenchBase's TPC-C (https://github.com/cmu-db/benchbase) as a workload: loaded on a tenant's main branch, then run
for `duration` seconds at a time. Besides the run itself, its data must keep TPC-C's consistency conditions.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .common import Problems, Target, Workload, java, run_logged

if TYPE_CHECKING:
    from fixtures.neon_fixtures import Endpoint

DATABASE = "tpcc"

CONFIG = """<?xml version="1.0"?>
<parameters>
    <type>POSTGRES</type>
    <driver>org.postgresql.Driver</driver>
    <url>jdbc:postgresql://127.0.0.1:{port}/{database}?sslmode=disable&amp;ApplicationName=tpcc&amp;reWriteBatchedInserts=true</url>
    <username>cloud_admin</username>
    <password></password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <isolation>TRANSACTION_SERIALIZABLE</isolation>
    <batchsize>128</batchsize>
    <scalefactor>{warehouses}</scalefactor>
    <terminals>{terminals}</terminals>
    <randomSeed>{seed}</randomSeed>
    <works>
        <work>
            <time>{duration}</time>
            <rate>unlimited</rate>
            <weights>45,43,4,4,4</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>NewOrder</name></transactiontype>
        <transactiontype><name>Payment</name></transactiontype>
        <transactiontype><name>OrderStatus</name></transactiontype>
        <transactiontype><name>Delivery</name></transactiontype>
        <transactiontype><name>StockLevel</name></transactiontype>
    </transactiontypes>
</parameters>
"""

# TPC-C's consistency conditions (TPC-C 3.3.2.1-4), each counting the warehouses or districts that violate it
CONSISTENCY = {
    "1: W_YTD = sum(D_YTD)": """
        SELECT count(*) FROM warehouse w
        WHERE w.w_ytd <> (SELECT sum(d.d_ytd) FROM district d WHERE d.d_w_id = w.w_id)""",
    "2: D_NEXT_O_ID - 1 = max(O_ID) = max(NO_O_ID)": """
        SELECT count(*) FROM district d
        WHERE d.d_next_o_id - 1 <> (SELECT max(o.o_id) FROM oorder o WHERE o.o_w_id = d.d_w_id AND o.o_d_id = d.d_id)
           OR d.d_next_o_id - 1 <> coalesce(
                (SELECT max(no.no_o_id) FROM new_order no WHERE no.no_w_id = d.d_w_id AND no.no_d_id = d.d_id),
                d.d_next_o_id - 1)""",
    "3: max(NO_O_ID) - min(NO_O_ID) + 1 = count(NEW-ORDER)": """
        SELECT count(*) FROM (
            SELECT max(no_o_id) - min(no_o_id) + 1 AS span, count(*) AS n
            FROM new_order GROUP BY no_w_id, no_d_id) s
        WHERE span <> n""",
    "4: sum(O_OL_CNT) = count(ORDER-LINE)": """
        SELECT count(*) FROM (
            SELECT o_w_id, o_d_id, sum(o_ol_cnt) AS lines FROM oorder GROUP BY o_w_id, o_d_id) o
        JOIN (
            SELECT ol_w_id, ol_d_id, count(*) AS lines FROM order_line GROUP BY ol_w_id, ol_d_id) ol
          ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id
        WHERE o.lines <> ol.lines""",
}


class BenchBase(Workload):
    name = "benchbase"

    def __init__(self, work_dir: Path, duration: int):
        super().__init__(work_dir, duration)
        self.home = Path(os.environ.get("BENCHBASE_HOME", "/opt/benchbase"))
        self.warehouses = int(os.environ.get("BENCHBASE_WAREHOUSES", "1"))
        self.terminals = int(os.environ.get("BENCHBASE_TERMINALS", "2"))
        # For the report: a JSON line per run (see `metrics`)
        self.metrics_file = Path(os.environ.get("METRICS_FILE", work_dir / "metrics.jsonl"))
        self.metrics_file.write_text("")
        self._metrics_lock = threading.Lock()

    def _benchbase(self, target: Target, step: str, seed: int, **steps: bool) -> Problems:
        run_dir = self.work_dir / f"benchbase-{target.label}-{step}"
        run_dir.mkdir(parents=True)
        config = run_dir / "tpcc.xml"
        config.write_text(
            CONFIG.format(
                port=target.port,
                database=DATABASE,
                warehouses=self.warehouses,
                terminals=self.terminals,
                seed=seed,
                duration=self.duration,
            )
        )
        flags = [f"--{k}={str(v).lower()}" for k, v in steps.items()]
        histograms = run_dir / "results" / "histograms.json"
        cmd = [*java(), "-jar", str(self.home / "benchbase.jar"), "-b", "tpcc", "-c", str(config), *flags,
               "-d", str(run_dir / "results"), "--json-histograms", str(histograms)]  # fmt: skip
        problems = Problems()
        rc = run_logged(cmd, run_dir / "benchbase.log", cwd=self.home, timeout=self.duration + 600)
        if rc != 0:
            problems.failures.append(
                f"BenchBase {step} on {target.label} exited with {rc} (see {run_dir}/benchbase.log)"
            )
        if steps.get("execute"):
            summaries = list((run_dir / "results").glob("*.summary.json"))
            if not summaries:
                problems.failures.append(f"BenchBase {step} on {target.label} wrote no results")
            else:
                summary = json.loads(summaries[0].read_text())
                requests = summary.get("Measured Requests", 0)
                if not requests:
                    problems.failures.append(f"BenchBase {step} on {target.label} completed no transactions")
                counts = json.loads(histograms.read_text()) if histograms.exists() else {}
                line = json.dumps(self.metrics(target, step, summary, counts))
                with self._metrics_lock, self.metrics_file.open("a") as f:
                    f.write(line + "\n")
        return problems

    def metrics(self, target: Target, phase: str, summary: dict, histograms: dict) -> dict:
        """A run's results: its throughput, latencies (ms) and transactions by outcome (BenchBase's histograms)."""
        seconds = summary.get("Elapsed Time (nanoseconds)", 0) / 1e9
        latency = summary.get("Latency Distribution", {})

        def transactions(outcome: str) -> dict[str, int]:
            # {"HISTOGRAM": {"com.oltpbenchmark.benchmarks.tpcc.procedures.NewOrder/01": 1406, ...}, ...}
            counts = histograms.get(outcome, {}).get("HISTOGRAM", {})
            return {name.rsplit(".", 1)[-1].split("/")[0]: n for name, n in counts.items()}

        completed = transactions("completed")
        return {
            "workload": "tpcc",
            "phase": phase,
            "target": target.label,
            "warehouses": self.warehouses,
            "terminals": self.terminals,
            "seconds": round(seconds, 1),
            "throughput": summary.get("Throughput (requests/second)"),
            "goodput": summary.get("Goodput (requests/second)"),
            # TPC-C's tpmC, but without the keying and think times: not comparable to audited results
            "new_orders_per_minute": completed.get("NewOrder", 0) * 60 / seconds if seconds else None,
            "latency_ms": {
                key: latency.get(f"{name} Latency (microseconds)", 0) / 1000
                for key, name in (
                    ("avg", "Average"),
                    ("p50", "Median"),
                    ("p95", "95th Percentile"),
                    ("p99", "99th Percentile"),
                    ("max", "Maximum"),
                )
            },
            "completed": completed,
            "aborted": transactions("aborted"),
            "retried": transactions("rejected"),
            "errors": transactions("unexpected"),
            "dbms_version": summary.get("DBMS Version"),
        }

    def prepare(self, target: Target):
        target.endpoint.safe_psql(f"CREATE DATABASE {DATABASE}")
        problems = self._benchbase(target, "load", 0, create=True, load=True, execute=False)
        assert not problems.failures, problems.failures

    def run(self, target: Target, phase: str, seed: int) -> Problems:
        return self._benchbase(target, phase, seed, execute=True)

    def check(self, target: Target) -> Problems:
        return check_consistency(target.endpoint, target.label)


def check_consistency(endpoint: Endpoint, label: str) -> Problems:
    problems = Problems()
    for condition, query in CONSISTENCY.items():
        violations = endpoint.safe_psql(query, dbname=DATABASE)[0][0]
        if violations:
            problems.failures.append(f"TPC-C consistency condition {condition} fails on {label} ({violations} rows)")
    return problems
