#!/usr/bin/env python3
"""Request a signed rebuild of every published package that needs a library version Arch no longer ships."""

import argparse
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from repo_safety import GitHub

MIRROR = "https://geo.mirror.pkgbuild.com"
ARCH_REPOSITORIES = ("core", "extra", "multilib")
# makepkg completes a library dependency with the version it built against: "libfoo.so=1-64".
PIN = re.compile(r"(\S+\.so)=\S+")
# A package Arch has just published can take this long to reach the mirror a build uses.
MIRROR_LAG = timedelta(hours=3)


def fetch(url, path=None):
    request = urllib.request.Request(url, headers={"User-Agent": "watch-libraries/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if path is None:
        return data
    Path(path).write_bytes(data)


def read_database(path):
    """Each package's dependencies and provisions, by package name."""
    packages = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            fields, key = {}, None
            for line in archive.extractfile(member).read().decode().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            packages[fields["NAME"][0]] = {"depends": fields.get("DEPENDS", []),
                                           "provides": fields.get("PROVIDES", [])}
    return packages


def stale_pins(published, arch):
    """For each published package, the libraries it needs at a version nothing provides."""
    provided = {entry for packages in list(arch.values()) + [published]
                for package in packages.values() for entry in package["provides"]}
    stale = {}
    for name, package in published.items():
        missing = [pin for pin in package["depends"] if PIN.fullmatch(pin) and pin not in provided]
        if missing:
            stale[name] = missing
    return stale


def current_providers(pins, arch):
    """The Arch packages that ship those libraries now, as (repository, package, provision)."""
    wanted = {PIN.fullmatch(pin)[1] for pin in pins}
    return {(repository, name, entry)
            for repository, packages in arch.items() for name, package in packages.items()
            for entry in package["provides"]
            if (match := PIN.fullmatch(entry)) and match[1] in wanted}


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def published_since(providers):
    """When the newest of those Arch packages reached its repository, or None when unknown."""
    times = []
    for repository, name in sorted({(repository, name) for repository, name, _ in providers}):
        try:
            details = json.loads(fetch(f"https://archlinux.org/packages/{repository}/x86_64/{name}/json/"))
            times.append(stamp(details["last_update"]))
        except (OSError, KeyError, ValueError):
            return None
    return max(times, default=None)


def watch(repository, databases=None, dry_run=False):
    with tempfile.TemporaryDirectory() as folder:
        if databases is None:
            databases = Path(folder)
            github = GitHub(repository)
            assets = {asset["name"]: asset for asset in github.snapshot()["assets"]}
            github.download(assets["mainstream.db"], databases / "mainstream.db")
            for name in ARCH_REPOSITORIES:
                fetch(f"{MIRROR}/{name}/os/x86_64/{name}.db", databases / f"{name}.db")
        published = read_database(Path(databases) / "mainstream.db")
        arch = {name: read_database(Path(databases) / f"{name}.db") for name in ARCH_REPOSITORIES}

    stale = stale_pins(published, arch)
    if not stale:
        print(f"All {len(published)} published packages find their libraries in Arch. No rebuild needed.")
        return

    providers = current_providers([pin for pins in stale.values() for pin in pins], arch)
    shipped = {PIN.fullmatch(entry)[1] for _, _, entry in providers}
    print("Rebuild required:")
    for name, pins in sorted(stale.items()):
        print(f"  - {name} needs {', '.join(pins)}")
    for arch_repository, name, entry in sorted(providers):
        print(f"  Arch [{arch_repository}] {name} provides {entry}")
    # A rebuild picks up a library's new version. It cannot bring back one Arch dropped.
    gone = sorted({PIN.fullmatch(pin)[1] for pins in stale.values() for pin in pins} - shipped)
    if gone:
        print(f"::error::Arch no longer ships {', '.join(gone)} under any version. "
              "The packages that need them cannot be installed until their build files change.")
        raise SystemExit(1)
    if dry_run:
        print("[dry-run] Dry run enabled; skipping workflow dispatch.")
        return

    runs = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}/actions/workflows/build.yml/runs?per_page=100",
    ], text=True))["workflow_runs"]
    if any(run["status"] != "completed" for run in runs):
        print("A signed repository build is already active; waiting for its result.")
        return
    # A build that started well after the libraries changed has already been built against
    # them. Asking again would only repeat whatever kept it from publishing.
    since = published_since(providers)
    if (runs and since and stamp(runs[0]["created_at"]) > since + MIRROR_LAG
            and GitHub(repository).reached_build(runs[0])):
        print("::error::The last build already had these libraries and the packages above still "
              "need the old versions. Their build files need attention; not asking again.")
        raise SystemExit(1)

    default_branch = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}",
    ], text=True))["default_branch"]
    subprocess.run(["gh", "workflow", "run", "build.yml", "--repo", repository, "--ref", default_branch,
                    "-f", "force_rebuild=" + " ".join(sorted(stale))], check=True)
    print(f"Requested a signed rebuild of {', '.join(sorted(stale))} on branch {default_branch}.")


def main():
    parser = argparse.ArgumentParser(description="Watch the libraries published packages are built against.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect state without dispatching build workflows")
    parser.add_argument("--databases", metavar="DIR",
                        help="Read mainstream.db and Arch's core.db, extra.db and multilib.db from DIR "
                             "(for example /var/lib/pacman/sync) instead of downloading them")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"),
                        help="GitHub repository (default: GITHUB_REPOSITORY or MainstreamOS/packages)")
    args = parser.parse_args()

    watch(args.repo, databases=args.databases, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
