#!/usr/bin/env python3
"""
Summarizes a pytest JUnit XML report as Markdown: `report.py <junit.xml> <title> [<findings>...] [--runner <file>]
[--metrics <file>]` prints a heading with the outcome counts, the runner's description (runner-info.sh), a table of the
test cases (with the failure messages, shortened), the lines of the findings files (if they exist) as a list and the
workload's results (a JSON line per run, see testing/workload/benchbase.py) as a table.
"""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET

MAX_MESSAGE = 300


PHASES = {"p1": "1: main", "p3": "3: main and branch"}


def read(path: str | None) -> str:
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def main(junit: str, title: str, findings: list[str], runner: str | None, metrics: str | None) -> None:
    runner_line = read(runner)
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as e:
        print(f"#### ❌ {title}\n\nNo test report: `{e}`")
        if runner_line:
            print(f"\n{runner_line}")
        return

    rows = []
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for case in cases:
        outcome, icon, message = "passed", "✅", ""
        for tag, o, i in (("failure", "failed", "❌"), ("error", "failed", "❌"), ("skipped", "skipped", "⏭️")):
            element = case.find(tag)
            if element is not None:
                outcome, icon = o, i
                message = (element.get("message") or "").strip()
                break
        counts[outcome] += 1
        message = " ".join(message.split()).replace("|", "\\|")
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE] + "…"
        duration = float(case.get("time") or 0)
        rows.append(f"| {icon} | `{case.get('name')}` | {duration:.0f}s | {message} |")

    # The compatibility tests skip themselves when data is missing
    status = "❌" if counts["failed"] or not cases else "⚠️" if counts["skipped"] else "✅"
    print(f"#### {status} {title}\n\n{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped")
    if runner_line:
        print(f"\n{runner_line}")
    print("\n| | Test | Duration | Message |\n|---|---|---|---|")
    print("\n".join(rows))

    lines = [line for path in findings for line in read(path).splitlines() if line]
    if lines:
        print()
        print("\n".join(f"- {line}" for line in lines))

    runs = [json.loads(line) for line in read(metrics).splitlines() if line.strip()]
    if runs:
        print()
        print_metrics(runs)


def print_metrics(runs: list[dict]) -> None:
    """BenchBase's results, a row per run and the total of each phase."""

    def total(counts: dict[str, int]) -> int:
        return sum(counts.values())

    def row(phase: str, target: str, runs: list[dict]) -> str:
        tps = sum(r["throughput"] or 0 for r in runs)
        new_orders = sum(r["new_orders_per_minute"] or 0 for r in runs)
        cells = [phase, target, f"{tps:,.1f}", f"{new_orders:,.0f}"]
        if len(runs) == 1:
            ms = runs[0]["latency_ms"]
            cells.append(" / ".join(f"{ms[k]:,.1f}" for k in ("avg", "p50", "p95", "p99", "max")))
        else:
            cells.append("")
        for outcome in ("completed", "aborted", "retried", "errors"):
            cells.append(f"{sum(total(r[outcome]) for r in runs):,}")
        return "| " + " | ".join(cells) + " |"

    first = runs[0]
    warehouses = f"{first['warehouses']} warehouse" + ("s" if first["warehouses"] != 1 else "")
    print(
        f"**TPC-C** ({warehouses}, {first['terminals']} terminals per branch, about "
        f"{first['seconds']:.0f} s per run, the branches of a phase at the same time on this runner)\n\n"
        "| Phase | Branch | TPS | NewOrder/min | Latency avg / p50 / p95 / p99 / max (ms) "
        "| Completed | Aborted | Retried | Errors |\n"
        "|---|---|--:|--:|--:|--:|--:|--:|--:|"
    )
    phases = sorted({r["phase"] for r in runs})
    for phase in phases:
        phase_runs = sorted((r for r in runs if r["phase"] == phase), key=lambda r: r["target"])
        name = PHASES.get(phase, phase)
        for r in phase_runs:
            print(row(name, f"`{r['target']}`", [r]))
        if len(phase_runs) > 1:
            print(row(f"**{name}**", "**all**", phase_runs))
    print(
        "\nTPS counts the measured transactions; NewOrder/min is TPC-C's tpmC without the keying and think times "
        "(not comparable to audited results). Aborted are NewOrder's intended rollbacks (1%), retried the "
        "serialization failures BenchBase retried."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit")
    parser.add_argument("title")
    parser.add_argument("findings", nargs="*")
    parser.add_argument("--runner")
    parser.add_argument("--metrics")
    args = parser.parse_args()
    main(args.junit, args.title, args.findings, args.runner, args.metrics)
