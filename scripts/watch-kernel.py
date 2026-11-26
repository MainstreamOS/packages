#!/usr/bin/env python3
"""Start the kernel update when Arch's linux is newer than the Arch package the mainstream kernel is built on.

A newer linux of the same series in core starts kernel-update.yml in update mode; one in core-testing starts it
in check mode (build and boot only), so a pick that no longer applies shows up before Arch ships. Each
version is tried once per mode; a failed run opens its own issue, and a run is repeated by hand. A new series
is never attempted: it is reported in one issue, since its picks are taken again by hand."""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ARCH = "https://archlinux.org/packages/{repo}/x86_64/linux/json/"
RECIPE = "pkgbuilds/mainstream/PKGBUILD"
CACHYOS = "https://github.com/CachyOS/linux"
OMARCHY = "repos/omacom/omarchy-pkgs/contents/pkgbuilds/linux-omarchy/PKGBUILD"
MARK = "<!-- kernel-watch "
WORKFLOW = "kernel-update.yml"
# Arch writes X.Y.Z.archN, and X.Y.archN for a .0 release (tag vX.Y-archN).
ARCH_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?\.arch(\d+)")
PKGREL = re.compile(r"[1-9]\d*(?:\.\d+)?")


def fetch_json(url):
    """Arch's package JSON, or None when Arch has no such package (404)."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def gh(*args):
    return subprocess.run(["gh", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def cachyos_tags(stable):
    return subprocess.run(["git", "ls-remote", "--tags", CACHYOS, f"refs/tags/cachyos-{stable}-*"],
                          stdout=subprocess.PIPE, text=True, check=True, timeout=120).stdout


def omarchy_recipe():
    return base64.b64decode(json.loads(gh("api", OMARCHY))["content"]).decode()


def parse(pkgver):
    match = ARCH_VER.fullmatch(pkgver)
    if not match:
        raise ValueError(f"unrecognized Arch kernel version: {pkgver}")
    major, minor, patch, arch = match.groups()
    return int(major), int(minor), int(patch or 0), int(arch)


def release(pkgrel):
    """Arch's pkgrel as a sortable tuple: 2 -> (2,), 1.1 -> (1, 1)."""
    if not PKGREL.fullmatch(pkgrel):
        raise ValueError(f"unrecognized Arch pkgrel: {pkgrel}")
    return tuple(int(part) for part in pkgrel.split("."))


def stable(key):
    """The kernel.org version: 7.3.6, or 7.4 for a .0 release."""
    return f"{key[0]}.{key[1]}" + (f".{key[2]}" if key[2] else "")


def tag(key):
    return f"v{stable(key)}-arch{key[3]}"


def recipe_version(text):
    """The Arch package the recipe is built on: (pkgver, pkgrel), pkgrel None when the recipe does not say.

    _archpkg=X.Y.Z.archN-R names it exactly. Older recipes give only the pkgver, as pkgver=X.Y.Z.archN or as
    pkgver=X.Y.Z with _forkpatch=vX.Y.Z-archN.patch."""
    def field(name):
        found = re.findall(rf"""^{name}=["']?([^"'\s]+)["']?\s*$""", text, re.M)
        if len(found) > 1:
            raise ValueError(f"{RECIPE} sets {name} more than once")
        return found[0] if found else None

    pkgver, patch, archpkg = field("pkgver"), field("_forkpatch"), field("_archpkg")
    ours = None
    if pkgver and ARCH_VER.fullmatch(pkgver) and not patch:
        ours = pkgver
    elif pkgver and patch and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", pkgver):
        match = re.fullmatch(r"v(\d+\.\d+(?:\.\d+)?)-arch(\d+)\.patch", patch)
        if match and match[1] == pkgver:
            ours = f"{pkgver}.arch{match[2]}"
    if not ours:
        raise ValueError(f"{RECIPE} has neither pkgver=X.Y.Z.archN nor pkgver=X.Y.Z with "
                         f"_forkpatch=vX.Y.Z-archN.patch (pkgver={pkgver}, _forkpatch={patch})")
    if archpkg is None:
        return ours, None
    match = re.fullmatch(r"(.+)-([^-]+)", archpkg)
    if not match or match[1] != ours or not PKGREL.fullmatch(match[2]):
        raise ValueError(f"{RECIPE} has _archpkg={archpkg}, which is not {ours}-<pkgrel>")
    return ours, match[2]


def cachyos_line(key):
    try:
        refs = cachyos_tags(stable(key))
    except (OSError, subprocess.SubprocessError) as error:
        print(f"::warning::CachyOS's tags could not be read: {error}")
        return "- CachyOS: could not be read this run."
    names = {m[1] for m in re.finditer(r"refs/tags/(cachyos-[\d.]+-(\d+))$", refs, re.M)
             if m[1].startswith(f"cachyos-{stable(key)}-")}
    if not names:
        return f"- CachyOS: no `cachyos-{stable(key)}-*` tag yet."
    newest = max(names, key=lambda n: int(n.rsplit("-", 1)[1]))
    return f"- CachyOS: [`{newest}`]({CACHYOS}/tree/{newest})"


def omarchy_line():
    try:
        text = omarchy_recipe()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"::warning::Omarchy's linux-omarchy recipe could not be read: {error}")
        return "- Omarchy: could not be read this run."
    ver = re.search(r"^pkgver=([0-9][^\s$]*)\s*$", text, re.M)
    rel = re.search(r"^pkgrel=([0-9][^\s$]*)\s*$", text, re.M)
    if not ver:
        print("::warning::Omarchy's linux-omarchy recipe has no plain pkgver.")
        return "- Omarchy: its recipe has no plain pkgver this run."
    version = ver[1] + (f"-{rel[1]}" if rel else "")
    return (f"- Omarchy: `linux-omarchy` {version} "
            "([recipe](https://github.com/omacom/omarchy-pkgs/blob/master/pkgbuilds/linux-omarchy/PKGBUILD))")


def body(core, ours, ours_rel, testing, extra):
    """The issue for a new series, which is taken by hand."""
    key, base = parse(core["pkgver"]), parse(ours)
    full = f"{core['pkgver']}-{core['pkgrel']}"
    ours_full = f"{ours}-{ours_rel}" if ours_rel else ours
    date = (core.get("last_update") or core.get("build_date") or "")[:10] or "an unknown date"
    changelog = (f"https://cdn.kernel.org/pub/linux/kernel/v{key[0]}.x/ChangeLog-{stable(key)}" if key[2]
                 else f"https://kernelnewbies.org/Linux_{key[0]}.{key[1]}")
    lines = [f"Arch's `linux` {full} is in `core` since {date}. `mainstream` is built on Arch's "
             f"{ours_full} (`{tag(base)}`)."]
    if testing:
        lines.append(f"`core-testing` already has {testing}, so another update is close.")
    lines += ["", f"**This is a new series ({base[0]}.{base[1]} to {key[0]}.{key[1]}).** The automatic update "
              "only follows a series, so this one is taken by hand: the CachyOS and Omarchy picks have to be "
              "taken again from their new series, not rebased from the old one.",
              "", "### Links",
              f"- Arch: [package](https://archlinux.org/packages/core/x86_64/linux/), "
              f"[`{tag(key)}`](https://github.com/archlinux/linux/releases/tag/{tag(key)}), "
              f"[ChangeLog]({changelog})",
              *extra,
              "", "### Update",
              f"- [ ] Make `mainstream/default-{stable(key)}` on `{tag(key)}` and take the picks again from the new "
              f"series, then `mainstream/performance-{stable(key)}` on it.",
              "- [ ] Tag both (`mainstream-<flavor>-<pkgver>-1`) and push the branches and tags.",
              "- [ ] Point the kernel recipes at the new tags with "
              f"`scripts/update-kernel-recipe.sh <tag> {full}`, and take Arch's `config.x86_64` of {full}.",
              "- [ ] Build them with the **Build kernels** workflow and boot the result.",
              "", f"{MARK}{full} -->"]
    return "\n".join(lines)


def full(package):
    return f"{package['pkgver']}-{package['pkgrel']}"


def decide(core, testing, ours, ours_rel):
    """What to do, as (action, Arch package): ("level", None), ("update", core), ("check", core-testing)
    or ("series", core)."""
    base = parse(ours)

    def ahead(package):
        key = parse(package["pkgver"]), release(package["pkgrel"])
        # Without a recorded pkgrel only the pkgver is compared, as before _archpkg existed.
        return key[0] > base or (key[0] == base and ours_rel is not None and key[1] > release(ours_rel))

    if ahead(core):
        return ("update" if parse(core["pkgver"])[:2] == base[:2] else "series"), full(core)
    if testing and ahead(testing) and (parse(testing["pkgver"]), release(testing["pkgrel"])) > \
            (parse(core["pkgver"]), release(core["pkgrel"])) and parse(testing["pkgver"])[:2] == base[:2]:
        return "check", full(testing)
    return "level", None


def report_series(repository, core, ours, ours_rel, testing):
    issues = json.loads(gh("issue", "list", "--repo", repository, "--state", "all", "--limit", "1000",
                           "--json", "number,state,body"))
    watch_issues = [i for i in issues if MARK in (i.get("body") or "")]
    version = full(core)
    mark = f"{MARK}{version} -->"
    # A closed issue counts too, so closing it settles that release.
    if any(mark in i["body"] for i in watch_issues):
        print(f"Already reported: linux {version}.")
        return

    key = parse(core["pkgver"])
    title = f"kernel: Arch released linux {version}, a new series"
    text = body(core, ours, ours_rel, testing, [cachyos_line(key), omarchy_line()])
    open_issues = sorted((i for i in watch_issues if i["state"] == "OPEN"), key=lambda i: i["number"])
    if open_issues:
        number = str(open_issues[0]["number"])
        gh("issue", "edit", number, "--repo", repository, "--title", title, "--body", text)
        gh("issue", "comment", number, "--repo", repository,
           "--body", f"Arch released linux {version}; this issue now tracks it.")
        print(f"Updated #{number}: {title}")
        return
    gh("issue", "create", "--repo", repository, "--title", title, "--body", text)
    print(f"Opened: {title}")


def start(repository, action, version):
    """Run kernel-update.yml once per mode and version; its run name is "Kernel <mode> <version>"."""
    runs = json.loads(gh("run", "list", "--repo", repository, "--workflow", WORKFLOW, "--limit", "200",
                         "--json", "displayTitle,status,conclusion"))
    if any(r.get("status") != "completed" for r in runs):
        print("A kernel update run is queued or running; trying again on the next schedule.")
        return
    title = f"Kernel {action} {version}"
    # A cancelled run (a newer one replaced it while waiting) is tried again; anything else is repeated by hand.
    if any(r.get("displayTitle") == title and r.get("conclusion") != "cancelled" for r in runs):
        print(f"Already tried: {title}. Run the Update kernels workflow by hand to try again.")
        return
    gh("workflow", "run", WORKFLOW, "--repo", repository, "-f", f"mode={action}", "-f", f"archpkg={version}")
    print(f"Started: {title}")


def watch(repository, recipe_path=RECIPE):
    core = fetch_json(ARCH.format(repo="core"))
    if not core:
        raise RuntimeError("Arch's core has no linux package")
    parse(core["pkgver"]), release(core["pkgrel"])
    with open(recipe_path) as f:
        ours, ours_rel = recipe_version(f.read())
    testing = fetch_json(ARCH.format(repo="core-testing"))
    if testing:
        parse(testing["pkgver"]), release(testing["pkgrel"])
    print(f"Arch core: linux {full(core)}" + (f", core-testing: {full(testing)}" if testing else "")
          + f". mainstream is built on {ours}" + (f"-{ours_rel}." if ours_rel else "."))

    action, version = decide(core, testing, ours, ours_rel)
    if action == "level":
        print("Level with Arch; nothing to do.")
    elif action == "series":
        newer = full(testing) if testing and (parse(testing["pkgver"]), release(testing["pkgrel"])) > \
            (parse(core["pkgver"]), release(core["pkgrel"])) else None
        report_series(repository, core, ours, ours_rel, newer)
    else:
        start(repository, action, version)


if __name__ == "__main__":
    try:
        watch(os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"::error::kernel watch failed: {error}")
