#!/usr/bin/env python3
"""
Parses and resolves a request to test a Neon build (see .github/workflows/issue.yml), from REQUEST (an issue's or a
comment's body; untrusted) and DEFAULTS (testing/version.yaml as JSON). Writes the inputs of neon-test.yml to
GITHUB_OUTPUT (`valid=true`), or `valid=false` and the reason (`error`).

The request is `key: value` lines (after `/neon-test`), or the `### key` sections of the issue form:
- what to test, one of:
  - `run`: a Build and Test run of jclab-oss/neon (ID or URL), which pushed the build's images tagged with its ID,
  - `pr`: a pull request of jclab-oss/neon (number or URL), for its head commit's latest such run,
  - `ref`: a branch, tag or commit of jclab-oss/neon, for its commit's latest such run,
  - `neon-ref`, `neon-image` and `compute-<version>` (e.g. `compute-v17`): the commit and the images themselves;
- `pg-versions`, `arches`, `tests`: lists (`v16, v17`), testing/version.yaml's if missing;
- `duration`: of each run of a workload, in seconds.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

NEON_REPOSITORY = "jclab-oss/neon"
IMAGE_OWNER = "ghcr.io/jclab-oss"
BUILD_AND_TEST = "Build and Test"

KEYS = {"run", "pr", "ref", "neon-ref", "neon-image", "pg-versions", "arches", "tests", "duration"}
COMPUTE_KEY = re.compile(r"^compute-(v1[4-7])$")
PG_VERSION = re.compile(r"^v1[4-7]$")
ARCHES = {"x64", "arm64"}
TESTS = {"compatibility", "sqlancer", "benchbase"}
RUN = re.compile(rf"^(?:https://github\.com/{re.escape(NEON_REPOSITORY)}/actions/runs/)?(\d{{1,20}})(?:/.*)?$")
PR = re.compile(rf"^(?:https://github\.com/{re.escape(NEON_REPOSITORY)}/pull/)?(\d{{1,10}})(?:/.*)?$")
REF = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
IMAGE = re.compile(
    r"^ghcr\.io/(?:jclab-oss|neondatabase)/[a-z0-9._-]+(?::[A-Za-z0-9._-]{1,128})?(?:@sha256:[0-9a-f]{64})?$"
)


class RequestError(Exception):
    pass


def parse(text: str) -> dict[str, str]:
    """The request's fields: `key: value` lines, or the issue form's `### key` sections."""
    fields: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    section = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            section = stripped[4:].strip().lower()
            continue
        if section is not None:
            if stripped and stripped != "_No response_" and section not in fields:
                fields[section] = stripped
            continue
        m = re.match(r"^([A-Za-z0-9-]+)\s*:\s*(.*)$", stripped)
        if m:
            fields[m.group(1).lower()] = m.group(2).strip()
    unknown = [k for k in fields if k not in KEYS and not COMPUTE_KEY.match(k)]
    if unknown:
        raise RequestError(f"unknown fields: {', '.join(sorted(unknown))}")
    return {k: v for k, v in fields.items() if v}


def as_list(value: str | None, default: list[str], allowed: re.Pattern | set[str], what: str) -> list[str]:
    if not value:
        return default
    items = [i for i in re.split(r"[\s,]+", value) if i]
    for item in items:
        ok = item in allowed if isinstance(allowed, set) else allowed.match(item)
        if not ok:
            raise RequestError(f"invalid {what}: `{item[:40]}`")
    return list(dict.fromkeys(items))


def github(path: str):
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RequestError(f"GitHub API {path}: {e.code}") from e


def image_exists(image: str) -> bool:
    """Whether a public image (`ghcr.io/<owner>/<name>:<tag>`) exists, from the registry's API."""
    name, tag = image.removeprefix("ghcr.io/").split(":", 1)
    try:
        with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{name}:pull", timeout=30) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{name}/manifests/{tag}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ", ".join(
                    [
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                    ]
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False


def run_images(run_id: str, pg_versions: list[str]) -> dict[str, str] | None:
    """The images a Build and Test run pushed (tagged with its ID), if all of them exist."""
    images = {"neon": f"{IMAGE_OWNER}/neon:{run_id}"}
    images.update({v: f"{IMAGE_OWNER}/compute-node-{v}:{run_id}" for v in pg_versions})
    return images if all(image_exists(i) for i in images.values()) else None


def latest_run_of(sha: str, pg_versions: list[str]) -> tuple[dict, dict[str, str]]:
    runs = github(f"repos/{NEON_REPOSITORY}/actions/runs?head_sha={sha}&per_page=50")["workflow_runs"]
    for run in sorted((r for r in runs if r["name"] == BUILD_AND_TEST), key=lambda r: r["id"], reverse=True):
        images = run_images(str(run["id"]), pg_versions)
        if images:
            return run, images
    raise RequestError(f"no {BUILD_AND_TEST} run of `{sha[:12]}` pushed the images (of {', '.join(pg_versions)})")


def resolve(fields: dict[str, str], defaults: dict) -> dict[str, str]:
    pg_versions = as_list(
        fields.get("pg-versions"), [v["name"] for v in defaults["versions"]], PG_VERSION, "Postgres version"
    )
    arches = as_list(fields.get("arches"), defaults.get("arches", sorted(ARCHES)), ARCHES, "architecture")
    tests = as_list(fields.get("tests"), defaults.get("tests", sorted(TESTS)), TESTS, "test")
    duration = fields.get("duration", "120")
    if not duration.isdigit() or not 30 <= int(duration) <= 600:
        raise RequestError("`duration` must be 30 to 600 (seconds)")

    sources = [k for k in ("run", "pr", "ref", "neon-image") if k in fields]
    if len(sources) != 1:
        raise RequestError("give one of `run`, `pr`, `ref` or `neon-image` (with `neon-ref` and `compute-<version>`)")
    source = sources[0]
    if source == "neon-image":
        neon_ref = fields.get("neon-ref", "")
        if not REF.match(neon_ref):
            raise RequestError("`neon-image` needs a valid `neon-ref`, the commit it was built from")
        images = {"neon": fields["neon-image"]}
        if not IMAGE.match(images["neon"]):
            raise RequestError("invalid `neon-image` (images of ghcr.io/jclab-oss or ghcr.io/neondatabase)")
        for v in pg_versions:
            if f"compute-{v}" not in fields:
                raise RequestError(f"`neon-image` needs `compute-{v}`")
            images[v] = fields[f"compute-{v}"]
        for image in images.values():
            if not IMAGE.match(image):
                raise RequestError(
                    f"invalid image `{image[:80]}` (images of ghcr.io/jclab-oss or ghcr.io/neondatabase)"
                )
        title = f"`{images['neon']}`"
    elif source == "run":
        m = RUN.match(fields["run"])
        if not m:
            raise RequestError("invalid `run`: a run ID or URL of jclab-oss/neon")
        run = github(f"repos/{NEON_REPOSITORY}/actions/runs/{m.group(1)}")
        if run["name"] != BUILD_AND_TEST:
            raise RequestError(f"run {m.group(1)} is not a {BUILD_AND_TEST} run")
        images = run_images(m.group(1), pg_versions)
        if not images:
            raise RequestError(
                f"run {m.group(1)} didn't push the images (of {', '.join(pg_versions)}) tagged with its ID"
            )
        neon_ref = run["head_sha"]
        title = f"{BUILD_AND_TEST} [run {run['id']}]({run['html_url']})"
    else:
        if source == "pr":
            m = PR.match(fields["pr"])
            if not m:
                raise RequestError("invalid `pr`: a pull request number or URL of jclab-oss/neon")
            pr = github(f"repos/{NEON_REPOSITORY}/pulls/{m.group(1)}")
            sha = pr["head"]["sha"]
            what = f"{NEON_REPOSITORY}#{m.group(1)}"
        else:
            if not REF.match(fields["ref"]):
                raise RequestError("invalid `ref`")
            sha = github(f"repos/{NEON_REPOSITORY}/commits/{fields['ref']}")["sha"]
            what = f"`{fields['ref']}`"
        run, images = latest_run_of(sha, pg_versions)
        neon_ref = sha
        title = f"{what}, {BUILD_AND_TEST} [run {run['id']}]({run['html_url']})"

    return {
        "neon-ref": neon_ref,
        "neon-image": images["neon"],
        "compute-images": json.dumps({v: images[v] for v in pg_versions}),
        "arches": json.dumps(arches),
        "tests": json.dumps(tests),
        "duration": duration,
        "title": title,
    }


def main() -> None:
    text = os.environ["REQUEST"]
    # A comment's request follows the command
    if "/neon-test" in text:
        text = text.split("/neon-test", 1)[1]
    try:
        outputs = {"valid": "true", **resolve(parse(text), json.loads(os.environ["DEFAULTS"]))}
    except RequestError as e:
        outputs = {"valid": "false", "error": str(e)}
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        for key, value in outputs.items():
            assert "\n" not in value
            f.write(f"{key}={value}\n")
    json.dump(outputs, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
