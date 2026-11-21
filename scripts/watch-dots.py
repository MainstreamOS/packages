#!/usr/bin/env python3
"""Request a signed rebuild when a package built from dots-hyprland has a newer build file."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from repo_safety import GitHub

DOTS_REPOSITORY = "MainstreamOS/dots-hyprland"
DOTS_BRANCH = "mainstream"


VERSION = re.compile(r"(?:(\d+):)?(.+)-(\d+)(?:\.(\d+))?")


def is_newer(wanted, published):
    """Whether a build file's version is ahead of the published one."""
    new, old = VERSION.fullmatch(wanted), VERSION.fullmatch(published)
    if not new or not old:
        raise ValueError(f"unrecognized version: {wanted} or {published}")
    epochs = int(new[1] or "0"), int(old[1] or "0")
    if epochs[0] == epochs[1] and new[2] == old[2]:
        return (int(new[3]), int(new[4] or "0")) > (int(old[3]), int(old[4] or "0"))
    if all(re.fullmatch(r"\d+(?:\.\d+)*", match[2]) for match in (new, old)):
        return ((epochs[0], tuple(map(int, new[2].split("."))))
                > (epochs[1], tuple(map(int, old[2].split(".")))))
    # A version named after a commit cannot be ordered, and the published one can
    # only have come from an earlier build file.
    return True


def dots_packages(list_path):
    names = []
    for line in Path(list_path).read_text().splitlines():
        name, _, source = line.partition("#")[0].strip().partition("::")
        if name and source == "dots":
            names.append(name)
    return names


def build_file_version(pkgbuild):
    """The version a build file names, or None when it works its version out while building."""
    text = Path(pkgbuild).read_text()
    if re.search(r"^pkgver\(\)", text, re.M):
        return None
    fields = {key: value.strip("'\"") for key, value in re.findall(r"^(epoch|pkgver|pkgrel)=(\S+)$", text, re.M)}
    version = f"{fields['pkgver']}-{fields['pkgrel']}"
    return f"{fields['epoch']}:{version}" if "epoch" in fields else version


def published_versions(current):
    published = {}
    for filename in current:
        name, pkgver, pkgrel, _ = filename.rsplit("-", 3)
        # A file name cannot hold the colon after an epoch, so it is published as "1_".
        published[name] = re.sub(r"^(\d+)_", r"\1:", pkgver) + f"-{pkgrel}"
    return published


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def dots_changed():
    """When the dots-hyprland branch last moved."""
    activity = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{DOTS_REPOSITORY}/activity?ref={DOTS_BRANCH}&per_page=1",
    ], text=True))
    return stamp(activity[0]["timestamp"])


def read_build_files(repository, run):
    """Whether a finished build got as far as building, which is where it reads dots-hyprland."""
    jobs = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}/actions/runs/{run['id']}/jobs",
    ], text=True))["jobs"]
    return any(step["name"] == "Build signed repo" and step["conclusion"] in ("success", "failure")
               for job in jobs for step in job.get("steps", []))


def watch(repository, dots_dir=None, dry_run=False):
    names = dots_packages("packages.list")
    published = published_versions(GitHub(repository).snapshot()["current"])
    behind = []
    with tempfile.TemporaryDirectory() as folder:
        if dots_dir is None:
            dots_dir = Path(folder) / "dots"
            subprocess.run(["git", "clone", "--quiet", "--depth=1", "--filter=blob:none", "--sparse",
                            "--branch", DOTS_BRANCH, f"https://github.com/{DOTS_REPOSITORY}.git", str(dots_dir)], check=True)
            subprocess.run(["git", "-C", str(dots_dir), "sparse-checkout", "set", "sdata/dist-arch"], check=True)
        for name in names:
            pkgbuild = Path(dots_dir) / "sdata/dist-arch" / name / "PKGBUILD"
            if not pkgbuild.is_file():
                print(f"::warning::{name} is listed here but has no build file in dots-hyprland.")
                continue
            wanted = build_file_version(pkgbuild)
            if wanted is None:
                print(f"{name}: versioned from its sources while building; its own watcher covers it.")
            elif name not in published:
                behind.append(f"{name} {wanted} is not published")
            elif is_newer(wanted, published[name]):
                behind.append(f"{name} {published[name]} -> {wanted}")
            else:
                print(f"{name}: published {published[name]} covers build file {wanted}.")
    if not behind:
        print("Every package built from dots-hyprland is up to date. No rebuild needed.")
        return

    print("Rebuild required:")
    for reason in behind:
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
    # A build that started after dots-hyprland last changed has already seen these build
    # files. Asking again would only repeat whatever kept it from publishing them.
    if runs and stamp(runs[0]["created_at"]) > dots_changed() and read_build_files(repository, runs[0]):
        print("::warning::The last build already had these build files and did not publish them. "
              "Not asking again until dots-hyprland changes.")
        return

    default_branch = json.loads(subprocess.check_output([
        "gh", "api", f"repos/{repository}",
    ], text=True))["default_branch"]
    subprocess.run(["gh", "workflow", "run", "build.yml", "--repo", repository,
                    "--ref", default_branch], check=True)
    print(f"Requested a signed repository build on branch {default_branch}.")


def main():
    parser = argparse.ArgumentParser(description="Watch the packages built from dots-hyprland.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect state without dispatching build workflows")
    parser.add_argument("--dots-dir", help="Read build files from this dots-hyprland checkout instead of cloning it")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"),
                        help="GitHub repository (default: GITHUB_REPOSITORY or MainstreamOS/packages)")
    args = parser.parse_args()
    watch(args.repo, dots_dir=args.dots_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
