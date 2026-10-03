"""
A Postgres workload (WORKLOAD: `benchbase` or `sqlancer`) on Neon, checking that branches and tenants don't interfere
with each other and that the data survives a crash of the storage:

1. main: the workload runs on the main branches of tenants t1 and t2 at the same time; t3 only loads data (canary).
2. branch: `new_branch` of main, at the LSN where phase 1 ended, has main's data, and so has main after a restart.
3. concurrent: the workload runs on main and new_branch of t1 and t2, all at the same time.
4. interference: each branch has only its own writes, main's past (a branch at phase 1's LSN) is unchanged, the
   tenants have only their own writes and t3 is unchanged; the storage checks pass.
5. durability: after the pageserver and the safekeepers are killed and restarted, all the data is the same.

If the build supports local branches (computes that keep their changes on their own disk, and only read from the
pageserver at a fixed LSN: its test fixtures take `local_branch`), each active tenant also has one, `local`:

2. its compute starts on `local_anchor`, a branch of main at phase 1's LSN that nothing writes to, with main's data.
3. the workload runs on it too, at the same time as on main and new_branch.
4. it has only its own writes, the other branches don't see them and the pageserver never saw them (the anchor's last
   record LSN didn't move); pg_amcheck passes, then its compute crashes and recovers.
5. its data is the same after the storage restarted.

LOCAL_BRANCHES (`auto`, the default, `true` or `false`) overrides the detection.
Phase durations: WORKLOAD_DURATION seconds (default 120) for each run of the workload.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from fixtures.common_types import Lsn
from fixtures.log_helper import log
from fixtures.neon_fixtures import EndpointFactory, check_restored_datadir_content, wait_for_last_flush_lsn
from fixtures.remote_storage import RemoteStorageKind

from .common import (
    REFERENCE_ROWS,
    Problems,
    Target,
    create_scenario_tables,
    databases,
    diff_fingerprints,
    fingerprints,
    in_parallel,
    mark,
    markers,
    reference_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fixtures.neon_fixtures import Endpoint, NeonEnv, NeonEnvBuilder, PgBin, VanillaPostgres

    from .common import Workload

WORKLOAD = os.environ.get("WORKLOAD", "benchbase")
DURATION = int(os.environ.get("WORKLOAD_DURATION", "120"))
ACTIVE_TENANTS = ["t1", "t2"]
CANARY_TENANT = "t3"
LOCAL_BRANCH = "local"
LOCAL_ANCHOR = "local_anchor"
CANARY_DATABASE = "canary"
# Rather than the tests' tiny defaults (1MB), for the workloads to run at a reasonable pace
ENDPOINT_CONFIG = [
    "shared_buffers = 128MB",
    "neon.max_file_cache_size = 64MB",
    "neon.file_cache_size_limit = 64MB",
]


def local_branches_supported() -> bool:
    setting = os.environ.get("LOCAL_BRANCHES", "auto")
    if setting != "auto":
        return setting == "true"
    # The fixtures come from the same commit as the binaries
    return "local_branch" in inspect.signature(EndpointFactory.create_start).parameters


class Scenario:
    def __init__(self, env: NeonEnv, workload: Workload, pg_bin: PgBin, test_output_dir: Path):
        self.env = env
        self.workload = workload
        self.pg_bin = pg_bin
        self.test_output_dir = test_output_dir
        self.tenants = {}  # label -> TenantId
        self.endpoints: dict[tuple[str, str], Endpoint] = {}  # (tenant, branch) -> endpoint
        self.problems = Problems()

    def target(self, tenant: str, branch: str) -> Target:
        return Target(tenant, branch, self.endpoints[(tenant, branch)])

    def start_endpoint(self, tenant: str, branch: str) -> Endpoint:
        local = {}
        timeline_branch = branch
        if branch == LOCAL_BRANCH:
            local = {"local_branch": True}
            timeline_branch = LOCAL_ANCHOR
        endpoint = self.env.endpoints.create_start(
            timeline_branch,
            endpoint_id=f"ep-{tenant}-{branch}".replace("_", "-"),
            tenant_id=self.tenants[tenant],
            config_lines=ENDPOINT_CONFIG,
            **local,
        )
        self.endpoints[(tenant, branch)] = endpoint
        return endpoint

    def last_record_lsn(self, tenant: str, timeline_id) -> Lsn:
        detail = self.env.pageserver.http_client().timeline_detail(self.tenants[tenant], timeline_id)
        return Lsn(detail["last_record_lsn"])

    def restart_all(self):
        """Restarts every endpoint, so that the reads come from the pageserver rather than a compute's caches."""
        for endpoint in self.endpoints.values():
            endpoint.stop()
        for endpoint in self.endpoints.values():
            endpoint.start()

    def flush_lsn(self, tenant: str, branch: str) -> Lsn:
        endpoint = self.endpoints[(tenant, branch)]
        lsn = Lsn(endpoint.safe_psql("SELECT pg_current_wal_flush_lsn()")[0][0])
        wait_for_last_flush_lsn(self.env, endpoint, self.tenants[tenant], endpoint.show_timeline_id())
        return lsn

    def run_workload(self, targets: list[Target], phase: str):
        seeds = {t.label: 1000 * (i + 1) + len(phase) for i, t in enumerate(targets)}
        log.info(f"Phase {phase}: running {self.workload.name} on {[t.label for t in targets]}")
        for problems in in_parallel(lambda t: self.workload.run(t, phase, seeds[t.label]), targets):
            self.problems.extend(problems)
        for problems in in_parallel(self.workload.check, targets):
            self.problems.extend(problems)

    def fail(self, message: str):
        log.error(message)
        self.problems.failures.append(message)

    def expect_fingerprint(self, what: str, expected: dict[str, str], actual: dict[str, str]):
        diffs = diff_fingerprints(expected, actual)
        if diffs:
            self.fail(f"{what}: the data differs in {len(diffs)} tables: " + "; ".join(diffs[:10]))

    def storage_checks(self, endpoint: Endpoint, what: str, local: bool = False):
        """
        pg_amcheck, and the compute's files are the pageserver's basebackup (which stops the endpoint). A local
        branch's data isn't on the pageserver: its compute crashes instead, to recover from its own WAL when it starts.
        """
        try:
            self.pg_bin.run_capture(
                [
                    "pg_amcheck",
                    "--all",
                    "--install-missing",
                    f"--host={endpoint.default_options['host']}",
                    f"--port={endpoint.default_options['port']}",
                    f"--username={endpoint.default_options['user']}",
                ]  # fmt: skip
            )
        except subprocess.CalledProcessError as e:
            self.fail(f"pg_amcheck found corruption on {what} (exit code {e.returncode})")
        if local:
            endpoint.stop(mode="immediate")
            return
        # Unlogged relations are reset in basebackups
        unlogged = [
            r[0]
            for db in databases(endpoint)
            for r in endpoint.safe_psql(
                "SELECT pg_relation_filepath(c.oid) FROM pg_class c WHERE c.relpersistence = 'u'", dbname=db
            )
        ]
        check_restored_datadir_content(self.test_output_dir, self.env, endpoint, ignored_files=unlogged)


@pytest.mark.timeout(3600)
def test_workload(neon_env_builder: NeonEnvBuilder, pg_bin: PgBin, test_output_dir: Path, vanilla_pg: VanillaPostgres):
    # One safekeeper: without WAL offloading, each would keep all the WAL, which takes the runners' disk
    neon_env_builder.num_safekeepers = 1
    neon_env_builder.enable_pageserver_remote_storage(RemoteStorageKind.LOCAL_FS)
    env = neon_env_builder.init_start()
    # The storage is killed in phase 5: the pageserver then drops the layers it hadn't uploaded and ingests the WAL
    # again, with old timestamps
    env.pageserver.allowed_errors.extend(
        [
            ".*connection reset.*",
            ".*Connection reset.*",
            ".*removing local file .*",
            ".*ingesting record with timestamp lagging more than wait_lsn_timeout.*",
        ]
    )

    work_dir = test_output_dir / "workload"
    work_dir.mkdir()
    workload: Workload
    if WORKLOAD == "benchbase":
        from .benchbase import BenchBase

        workload = BenchBase(work_dir, DURATION)
    elif WORKLOAD == "sqlancer":
        from .sqlancer import SQLancer

        vanilla_pg.start()
        workload = SQLancer(work_dir, DURATION, vanilla_pg)
    else:
        raise AssertionError(f"unknown WORKLOAD {WORKLOAD}")

    s = Scenario(env, workload, pg_bin, test_output_dir)
    local = local_branches_supported()
    branches = ["main", "new_branch", *([LOCAL_BRANCH] if local else [])]
    notes = [
        f"ℹ️ Local branches: tested ({', '.join(f'{t}_{LOCAL_BRANCH}' for t in ACTIVE_TENANTS)})"
        if local
        else "ℹ️ Local branches: not supported by this build, not tested"
    ]
    log.info(notes[0])
    for tenant in [*ACTIVE_TENANTS, CANARY_TENANT]:
        s.tenants[tenant], _ = env.create_tenant()
        s.start_endpoint(tenant, "main")
        create_scenario_tables(s.endpoints[(tenant, "main")])
        mark(s.target(tenant, "main"), "p1")

    # 1. main
    canary = s.endpoints[(CANARY_TENANT, "main")]
    canary.safe_psql(f"CREATE DATABASE {CANARY_DATABASE}")
    pg_bin.run_capture(["pgbench", "--initialize", "--scale=5", canary.connstr(dbname=CANARY_DATABASE)])
    main = [s.target(t, "main") for t in ACTIVE_TENANTS]
    in_parallel(workload.prepare, main)
    s.run_workload(main, "p1")
    lsn1 = {t: s.flush_lsn(t, "main") for t in ACTIVE_TENANTS}
    *s1_list, canary_fingerprint = fingerprints([*(s.endpoints[(t, "main")] for t in ACTIVE_TENANTS), canary])
    s1 = dict(zip(ACTIVE_TENANTS, s1_list))
    reference = reference_fingerprint(canary)
    log.info(f"Phase 1 done at {lsn1}, {sum(map(len, s1.values()))} tables")

    # 2. branch (and the local branches, on their anchors)
    anchors = {}  # tenant -> (timeline, its last record LSN)
    for t in ACTIVE_TENANTS:
        env.create_branch("new_branch", tenant_id=s.tenants[t], ancestor_branch_name="main", ancestor_start_lsn=lsn1[t])
        s.start_endpoint(t, "new_branch")
        if local:
            anchor = env.create_branch(
                LOCAL_ANCHOR, tenant_id=s.tenants[t], ancestor_branch_name="main", ancestor_start_lsn=lsn1[t]
            )
            anchors[t] = (anchor, s.last_record_lsn(t, anchor))
            s.start_endpoint(t, LOCAL_BRANCH)
        s.endpoints[(t, "main")].stop().start()
    phase2 = dict(zip(branches, zip(*[fingerprints([s.endpoints[(t, b)] for b in branches]) for t in ACTIVE_TENANTS])))
    for i, t in enumerate(ACTIVE_TENANTS):
        for branch in branches:
            what = f"{t} main after a restart" if branch == "main" else f"{t} {branch} at phase 1's end"
            s.expect_fingerprint(what, s1[t], phase2[branch][i])

    # 3. concurrent
    targets = [s.target(t, b) for t in ACTIVE_TENANTS for b in branches]
    for target in targets:
        mark(target, "p3")
    s.run_workload(targets, "p3")

    # 4. interference, from fresh computes. The storage checks stop the endpoints, then they start again (and the
    # branches of main at phase 1's LSN start).
    for t in ACTIVE_TENANTS:
        env.create_branch("verify", tenant_id=s.tenants[t], ancestor_branch_name="main", ancestor_start_lsn=lsn1[t])
        s.start_endpoint(t, "verify").stop()
    s.restart_all()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda kv: s.storage_checks(kv[1], f"{kv[0][0]} {kv[0][1]}", local=kv[0][1] == LOCAL_BRANCH),
                list(s.endpoints.items()),
            )
        )
    for endpoint in s.endpoints.values():
        endpoint.start()
    keys = list(s.endpoints)
    final = dict(zip(keys, fingerprints([s.endpoints[k] for k in keys])))
    for t in ACTIVE_TENANTS:
        for branch in branches:
            if final[(t, branch)] == s1[t]:
                s.fail(f"{t} {branch}: the data didn't change in phase 3, did the workload run?")
            expected_markers = {(t, "main", "p1"), (t, branch, "p3")}
            actual_markers = markers(s.endpoints[(t, branch)])
            if actual_markers != expected_markers:
                s.fail(f"{t} {branch}: markers {sorted(actual_markers)}, expected {sorted(expected_markers)}")
        for i, a in enumerate(branches):
            for b in branches[i + 1 :]:
                if final[(t, a)] == final[(t, b)]:
                    s.fail(f"{t}: {a} and {b} have the same data after phase 3")
        # main's past is unchanged
        s.expect_fingerprint(f"{t} branch of main at phase 1's LSN, after phase 3", s1[t], final[(t, "verify")])
    s.expect_fingerprint(f"{CANARY_TENANT} (idle canary)", canary_fingerprint, final[(CANARY_TENANT, "main")])
    check_anchors(s, anchors, "after phase 3")
    for (tenant, branch), endpoint in s.endpoints.items():
        if reference_fingerprint(endpoint) != reference:
            s.fail(f"{tenant} {branch}: the reference table ({REFERENCE_ROWS} rows) changed")

    # 5. durability: kill the storage, then everything must be there again
    for endpoint in s.endpoints.values():
        endpoint.stop()
    env.pageserver.stop(immediate=True)
    for sk in env.safekeepers:
        sk.stop(immediate=True)
    for sk in env.safekeepers:
        sk.start()
    env.pageserver.start()
    for endpoint in s.endpoints.values():
        endpoint.start()
    after_restart = dict(zip(keys, fingerprints([s.endpoints[k] for k in keys])))
    for tenant, branch in keys:
        s.expect_fingerprint(
            f"{tenant} {branch} after the storage restarted", final[(tenant, branch)], after_restart[(tenant, branch)]
        )
    check_anchors(s, anchors, "after the storage restarted")
    for t in ACTIVE_TENANTS:
        for branch in branches:
            s.problems.extend(workload.check(s.target(t, branch)))

    for warning in s.problems.warnings:
        log.warning(warning)
    # For the report (FINDINGS_FILE): what failed and what is only reported
    findings = [f"❌ {f}" for f in s.problems.failures] + [f"⚠️ {w}" for w in s.problems.warnings] + notes
    with open(os.environ.get("FINDINGS_FILE", test_output_dir / "findings.txt"), "w") as f:
        f.write("".join(f"{line}\n" for line in findings))
    assert not s.problems.failures, "\n".join(s.problems.failures)


def check_anchors(s: Scenario, anchors: dict, when: str):
    """The pageserver never saw the local branches' writes: their anchors' last record LSN didn't move."""
    for t, (timeline, lsn) in anchors.items():
        now = s.last_record_lsn(t, timeline)
        if now != lsn:
            s.fail(f"{t} {LOCAL_ANCHOR}: its last record LSN moved from {lsn} to {now} {when}")
