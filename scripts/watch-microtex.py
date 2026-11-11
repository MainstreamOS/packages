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
from pathlib import Path

# Allow importing repo_safety when run from repo root or scripts dir
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_safety import GitHub


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


def get_upstream_commit():
    try:
        out = subprocess.check_output(
            ["gh", "api", "repos/end-4/MicroTeX/commits/master"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return json.loads(out)["sha"]
    except Exception:
        url = "https://api.github.com/repos/end-4/MicroTeX/commits/master"
        req = urllib.request.Request(url, headers={"User-Agent": "watch-microtex/1.0"})
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["sha"]


def watch(repository, dry_run=False):
    # 1. Check Arch Linux package metadata
    arch_packages = {}
    for pkg in ["tinyxml2", "gtkmm3", "cairomm", "gtksourceviewmm"]:
        arch_packages[pkg] = fetch_json(f"https://archlinux.org/packages/extra/x86_64/{pkg}/json/")

    tinyxml2 = arch_packages["tinyxml2"]
    tinyxml2_ver = tinyxml2["pkgver"]
    tinyxml2_rel = tinyxml2["pkgrel"]
    tinyxml2_major = int(tinyxml2_ver.split(".")[0])
    print(f"Arch tinyxml2: {tinyxml2_ver}-{tinyxml2_rel} (major: {tinyxml2_major})")

    gtkmm3 = arch_packages["gtkmm3"]
    print(f"Arch gtkmm3: {gtkmm3['pkgver']}-{gtkmm3['pkgrel']}")

    cairomm = arch_packages["cairomm"]
    print(f"Arch cairomm: {cairomm['pkgver']}-{cairomm['pkgrel']}")

    gtksourceviewmm = arch_packages["gtksourceviewmm"]
    print(f"Arch gtksourceviewmm: {gtksourceviewmm['pkgver']}-{gtksourceviewmm['pkgrel']}")

    # 2. Check upstream MicroTeX commit
    upstream_sha = get_upstream_commit()
    print(f"Upstream end-4/MicroTeX master commit: {upstream_sha}")

    # 3. Check published mainstream-microtex-git asset and database
    gh = GitHub(repository)
    state = gh.snapshot()

    published_name = None
    for name in state.get("current", {}):
        if name.startswith("mainstream-microtex-git-"):
            published_name = name
            break

    if not published_name:
        for asset in state.get("assets", []):
            name = asset["name"]
            if name.startswith("mainstream-microtex-git-") and name.endswith(".pkg.tar.zst"):
                published_name = name
                break

    rebuild_reasons = []

    if not published_name:
        rebuild_reasons.append("No published mainstream-microtex-git package found in repository")
        pub_rev = None
        pub_commit = None
        pub_pkgrel = None
    else:
        print(f"Published package asset: {published_name}")
        match = re.fullmatch(r"mainstream-microtex-git-r(\d+)\.([0-9a-fA-F]+)-(\d+)-x86_64\.pkg\.tar\.zst", published_name)
        if not match:
            raise ValueError(f"unrecognized published microtex filename: {published_name}")
        pub_rev = match.group(1)
        pub_commit = match.group(2)
        pub_pkgrel = int(match.group(3))
        print(f"Published microtex rev: r{pub_rev}, commit: {pub_commit}, pkgrel: {pub_pkgrel}")

        # Check commit match
        if not upstream_sha.startswith(pub_commit):
            rebuild_reasons.append(
                f"Upstream MicroTeX commit ({upstream_sha[:7]}) differs from published commit ({pub_commit})"
            )

    # 4. Check dependencies in mainstream.db
    by_name = {a["name"]: a for a in state.get("assets", [])}
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

    # Check for soname pins and tinyxml2 version bounds
    has_tinyxml2_pin = "tinyxml2>=11" in published_depends or any(d.startswith("tinyxml2>=") for d in published_depends)
    has_cairomm_so = "libcairomm-1.0.so" in published_depends
    has_gtkmm_so = "libgtkmm-3.0.so" in published_depends

    if pub_pkgrel is not None:
        missing_pins = []
        if not has_tinyxml2_pin:
            missing_pins.append("tinyxml2>=11")
        if not has_cairomm_so:
            missing_pins.append("libcairomm-1.0.so")
        if not has_gtkmm_so:
            missing_pins.append("libgtkmm-3.0.so")

        if missing_pins or pub_pkgrel < 5:
            rebuild_reasons.append(
                f"Published package is at pkgrel {pub_pkgrel} and lacks required pins: {', '.join(missing_pins) if missing_pins else 'none'}"
            )

    # Check Arch tinyxml2 major version
    if tinyxml2_major != 11:
        rebuild_reasons.append(
            f"Arch tinyxml2 major version changed: {tinyxml2_ver} (major {tinyxml2_major} != 11)"
        )

    # Decision
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
