#!/usr/bin/env python3
"""Request a signed rebuild after the GTK fork has tested a new Arch release."""

import base64
import json
import os
import re
import subprocess
import urllib.request

from repo_safety import GitHub


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def version_key(version):
    # Arch's stable GTK releases use numeric dotted versions. Refuse an
    # unfamiliar format rather than guessing an upgrade/downgrade ordering.
    match = re.fullmatch(r"(\d+):(\d+(?:\.\d+)+)-(\d+)(?:\.(\d+))?", version)
    if not match:
        raise ValueError(f"unrecognized stable GTK version: {version}")
    return (int(match[1]), tuple(map(int, match[2].split("."))),
            int(match[3]), int(match[4] or "0"))


def watch(repository):
    arch = fetch_json("https://archlinux.org/packages/extra/x86_64/gtk4/json/")
    stable = f"{arch['epoch']}:{arch['pkgver']}-{arch['pkgrel']}"
    desired = version_key(stable)
    recipe = json.loads(subprocess.check_output([
        "gh", "api", "repos/MainstreamOS/gtk4-live-css/contents/upstream-version?ref=mainstream",
    ], text=True))
    tested = base64.b64decode(recipe["content"]).decode().strip()
    if tested != stable:
        print(f"Arch has {stable}; the fork's tested recipe is {tested}. Waiting for its GTK build/tests.")
        return
    state = GitHub(repository).snapshot()
    for name in state["current"]:
        if not name.startswith("gtk4-live-css-"):
            continue
        match = re.fullmatch(r"gtk4-live-css-(\d+)_(.+)-([0-9]+(?:\.[0-9]+)?)-x86_64\.pkg\.tar\.zst", name)
        if not match:
            raise ValueError(f"unrecognized published GTK filename: {name}")
        installed = f"{match[1]}:{match[2]}-{match[3]}"
        if version_key(installed) >= desired:
            print(f"Published GTK {installed} already covers Arch {stable}.")
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
    subprocess.run(["gh", "workflow", "run", "build.yml", "--repo", repository,
                    "--ref", default_branch], check=True)
    print(f"Requested a signed repository rebuild for tested GTK {stable}.")


if __name__ == "__main__":
    watch(os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"))
