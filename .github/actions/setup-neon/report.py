#!/usr/bin/env python3
"""
Summarizes a pytest JUnit XML report as Markdown: `report.py <junit.xml> <title> [<findings>...]` prints a heading with
the outcome counts, a table of the test cases (with the failure messages, shortened) and the lines of the findings
files (if they exist) as a list.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

MAX_MESSAGE = 300


def main(junit: str, title: str, findings: list[str]) -> None:
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as e:
        print(f"#### ❌ {title}\n\nNo test report: `{e}`")
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
    print(
        f"#### {status} {title}\n\n"
        f"{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped\n\n"
        "| | Test | Duration | Message |\n|---|---|---|---|"
    )
    print("\n".join(rows))

    lines = [line for path in findings if os.path.exists(path) for line in open(path).read().splitlines() if line]
    if lines:
        print()
        print("\n".join(f"- {line}" for line in lines))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
