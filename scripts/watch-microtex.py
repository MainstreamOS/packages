#!/usr/bin/env python3
"""Request a signed rebuild when MicroTeX dependencies change or upstream commits arrive."""

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

# Allow importing repo_safety when run from repo root or scripts dir
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_safety import GitHub


DOTS_REPOSITORY = "MainstreamOS/dots-hyprland"
PKGBUILD_PATH = "sdata/dist-arch/mainstream-microtex-git/PKGBUILD"
SONAME_PINS = ("libcairomm-1.0.so", "libgtkmm-3.0.so")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "watch-microtex/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def parse_desc(desc_text):
    """Parse pacman database desc file format."""
    fields = {}
    current_key = None
    for line in desc_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("%") and line.endswith("%"):
            current_key = line.strip("%")
            fields[current_key] = []
        elif current_key:
            fields[current_key].append(line)
    return fields


def stamp(text):
    """A time from GitHub or archlinux.org, or None when it cannot be read."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_upstream_commit():
    """The sha of upstream's newest commit and when it was committed."""
    try:
        out = subprocess.check_output(
            ["gh", "api", "repos/end-4/MicroTeX/commits/master"],
            text=True, stderr=subprocess.DEVNULL,
        )
        commit = json.loads(out)
    except Exception:
        url = "https://api.github.com/repos/end-4/MicroTeX/commits/master"
        req = urllib.request.Request(url, headers={"User-Agent": "watch-microtex/1.0"})
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            commit = json.load(resp)
    return commit["sha"], stamp(commit["commit"]["committer"]["date"])


def get_packaging_time():
    """When the build file in the dotfiles repository last changed, or None."""
    try:
        out = subprocess.check_output(
            ["gh", "api", f"repos/{DOTS_REPOSITORY}/commits?path={PKGBUILD_PATH}&per_page=1"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return stamp(json.loads(out)[0]["commit"]["committer"]["date"])
    except Exception:
        return None


def watch(repository, dry_run=False):
    # A rebuild is asked for only when something it is built from is newer than the published
    # build. The package name cannot tell: its version is fixed in the build file, so the commit
    # it names stays the same however often it is rebuilt.
    changed = {}

    # 1. Check Arch Linux package metadata
    arch_packages = {}
    for pkg in ["tinyxml2", "gtkmm3", "cairomm", "gtksourceviewmm"]:
        arch_packages[pkg] = fetch_json(f"https://archlinux.org/packages/extra/x86_64/{pkg}/json/")
        version = f"{arch_packages[pkg]['pkgver']}-{arch_packages[pkg]['pkgrel']}"
        print(f"Arch {pkg}: {version}")
        changed[f"Arch {pkg} {version}"] = stamp(arch_packages[pkg].get("last_update"))
    tinyxml2_ver = arch_packages["tinyxml2"]["pkgver"]
    tinyxml2_major = int(tinyxml2_ver.split(".")[0])

    # 2. Check upstream MicroTeX commit
    upstream_sha, upstream_time = get_upstream_commit()
    print(f"Upstream end-4/MicroTeX master commit: {upstream_sha}")
    changed[f"upstream MicroTeX commit {upstream_sha[:7]}"] = upstream_time
    changed["the package's build file"] = get_packaging_time()

    # 3. Check published mainstream-microtex-git asset and database
    gh = GitHub(repository)
    state = gh.snapshot()
    by_name = {a["name"]: a for a in state.get("assets", [])}

    published_name = None
    for name in list(state.get("current", {})) + list(by_name):
        if name.startswith("mainstream-microtex-git-") and name.endswith(".pkg.tar.zst"):
            published_name = name
            break

    rebuild_reasons = []
    problems = []

    if not published_name:
        rebuild_reasons.append("No published mainstream-microtex-git package found in repository")
    else:
        print(f"Published package asset: {published_name}")
        # pkgrel may be dotted (e.g. "5.1" for a rebuild), so accept 1+ numeric parts.
        match = re.fullmatch(
            r"mainstream-microtex-git-r(\d+)\.([0-9a-fA-F]+)-(\d+(?:\.\d+)*)-x86_64\.pkg\.tar\.zst",
            published_name,
        )
        if not match:
            raise ValueError(f"unrecognized published microtex filename: {published_name}")
        built = stamp(by_name.get(published_name, {}).get("updated_at"))
        print(f"Published microtex rev: r{match.group(1)}, commit: {match.group(2)}, "
              f"pkgrel: {match.group(3)}, built: {built}")
        if built is None:
            rebuild_reasons.append("The published package's build time is unknown")
        else:
            rebuild_reasons += [f"{what} is newer than the published build ({when} > {built})"
                                for what, when in changed.items() if when and when > built]

    # 4. Check dependencies in mainstream.db
    published_depends = []
    if "mainstream.db" in by_name:
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "mainstream.db"
            gh.download(by_name["mainstream.db"], db_path)
            with tarfile.open(db_path, "r:*") as archive:
                for member in archive:
                    if member.name.startswith("mainstream-microtex-git-") and member.name.endswith("/desc"):
                        content = archive.extractfile(member).read().decode()
                        desc = parse_desc(content)
                        published_depends = desc.get("DEPENDS", [])
                        break

    print(f"Published dependencies in mainstream.db: {published_depends}")

    if published_name:
        # makepkg completes a soname pin with the version it built against ("libfoo.so=1-64").
        missing_pins = [pin for pin in SONAME_PINS
                        if not any(d == pin or d.startswith(pin + "=") for d in published_depends)]
        if not any(d.startswith("tinyxml2>=") for d in published_depends):
            missing_pins.append("tinyxml2>=")
        # The range the published package accepts, from its own pins.
        accepted = [int(m.group(2)) if m.group(1) == "<" else int(m.group(2)) + 1
                    for m in (re.fullmatch(r"tinyxml2(<|<=)(\d+)(?:\..*)?", d) for d in published_depends) if m]
        if accepted and tinyxml2_major >= min(accepted):
            missing_pins.append(f"a tinyxml2 range that takes Arch's {tinyxml2_ver}")
        # A rebuild only helps when the build file changed since; otherwise it would be asked
        # for again on every run and publish the same package under a new release number.
        if missing_pins and not rebuild_reasons:
            problems.append("The published package lacks " + ", ".join(missing_pins)
                            + f", and nothing it is built from changed since. Fix {PKGBUILD_PATH} in {DOTS_REPOSITORY}.")

    # Decision
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        raise SystemExit(1)
    if not rebuild_reasons:
        print("Published mainstream-microtex-git is up to date and satisfies all dependency pins. No rebuild needed.")
        return

    print("Rebuild required:")
    for reason in rebuild_reasons:
        print(f"  - {reason}")

    if dry_run:
        print("[dry-run] Dry run enabled; skipping workflow dispatch.")
        return

    runs = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}/actions/workflows/build.yml/runs?per_page=100",
    ], text=True))["workflow_runs"]
    if any(run["status"] != "completed" for run in runs):
        print("A signed repository build is already active; waiting for its result.")
        return

    default_branch = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}",
    ], text=True))["default_branch"]

    subprocess.run([
        "gh", "workflow", "run", "build.yml",
        "--repo", repository,
        "--ref", default_branch,
        "-f", "force_rebuild=mainstream-microtex-git",
    ], check=True)
    print(f"Requested a signed repository rebuild for mainstream-microtex-git on branch {default_branch}.")


def main():
    parser = argparse.ArgumentParser(description="Watch MicroTeX dependencies and upstream commits.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect state without dispatching build workflows")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"),
                        help="GitHub repository (default: GITHUB_REPOSITORY or MainstreamOS/packages)")
    args = parser.parse_args()

    watch(args.repo, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
