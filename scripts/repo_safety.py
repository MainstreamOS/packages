#!/usr/bin/env python3
"""Version rebuilds and publish pacman repositories without replacing packages."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time


TAG = "mainstream-repo"
LEDGER = "retained-packages.json"
MUTABLE = {"mainstream.db", "mainstream.db.sig", "mainstream.files", "mainstream.files.sig", LEDGER}
RETENTION = 30 * 24 * 60 * 60
ASSET_LIMIT = 1000
PACKAGE = re.compile(r"[A-Za-z0-9@_+.-]+\.pkg\.tar\.zst(?:\.sig)?\Z")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def database(path):
    """Read package references without extracting any archive paths."""
    packages = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith("/desc"):
                continue
            if member.size > 4 * 1024 * 1024:
                raise ValueError("oversized repository description")
            lines = archive.extractfile(member).read().decode().splitlines()
            fields = dict(zip(lines, lines[1:]))
            name = fields["%FILENAME%"]
            if not PACKAGE.fullmatch(name) or name.endswith(".sig"):
                raise ValueError(f"unsafe package filename: {name}")
            digest = fields["%SHA256SUM%"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or name in packages:
                raise ValueError(f"invalid or duplicate database entry: {name}")
            packages[name] = {"sha256": digest, "size": int(fields["%CSIZE%"])}
    if not packages:
        raise ValueError("empty repository database")
    return packages


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GitHub repository")
        self.repository = repository
        self.api = f"repos/{repository}"

    def snapshot(self):
        result = subprocess.run(
            ["gh", "api", f"{self.api}/releases/tags/{TAG}"],
            capture_output=True, text=True,
        )
        if result.returncode:
            # Authentication, rate limiting and network failures must never
            # masquerade as an empty repository with reusable filenames.
            if "(HTTP 404)" not in result.stderr:
                raise RuntimeError(result.stderr.strip())
            return {"release": None, "assets": [], "current": {}, "retired": {}}
        release = json.loads(result.stdout)
        pages = json.loads(run(
            "gh", "api", "--paginate", "--slurp",
            f"{self.api}/releases/{release['id']}/assets?per_page=100",
            capture_output=True, text=True,
        ).stdout)
        assets = [asset for page in pages for asset in page]
        by_name = {asset["name"]: asset for asset in assets}
        if len(by_name) != len(assets):
            raise ValueError("duplicate release asset names")
        current, retired = {}, {}
        with tempfile.TemporaryDirectory() as folder:
            if "mainstream.db" in by_name:
                path = Path(folder) / "mainstream.db"
                self.download(by_name["mainstream.db"], path)
                current = database(path)
            if LEDGER in by_name:
                path = Path(folder) / LEDGER
                self.download(by_name[LEDGER], path)
                ledger = read_json(path)
                if ledger.get("version") != 1 or not isinstance(ledger.get("retired"), dict):
                    raise ValueError("invalid package retention ledger")
                retired = ledger["retired"]
                for name, timestamp in retired.items():
                    if (not PACKAGE.fullmatch(name) or name.endswith(".sig")
                            or type(timestamp) is not int or timestamp < 0):
                        raise ValueError("invalid package retirement record")
        return {"release": release["id"], "assets": assets,
                "current": current, "retired": retired}

    def download(self, asset, path):
        with Path(path).open("wb") as stream:
            run("gh", "api", f"{self.api}/releases/assets/{asset['id']}",
                "-H", "Accept: application/octet-stream", stdout=stream)
        digest = asset.get("digest")
        if digest and digest != "sha256:" + sha256(path):
            raise ValueError(f"download digest mismatch: {asset['name']}")

    def matches(self, asset, path):
        digest = asset.get("digest")
        if digest:
            return digest == "sha256:" + sha256(path)
        # Older release assets have no API digest. Hash the original bytes;
        # absence of a digest is never permission to overwrite them.
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "asset"
            self.download(asset, original)
            return sha256(original) == sha256(path)

    def upload(self, path, mutable=False):
        if mutable and path.name not in MUTABLE:
            raise ValueError(f"not mutable repository metadata: {path.name}")
        for attempt in range(5):
            # Also handles an upload that succeeded remotely before its local
            # connection timed out. Never retry it using --clobber.
            state = self.snapshot_assets()
            asset = state.get(path.name)
            if asset and self.matches(asset, path):
                print(f"unchanged: {path.name}", flush=True)
                return
            if asset and not mutable:
                raise ValueError(f"refusing to replace published package/signature: {path.name}")
            command = ["gh", "release", "upload", TAG, str(path), "--repo", self.repository]
            if mutable:
                command.append("--clobber")
            result = subprocess.run(command)
            if result.returncode == 0:
                time.sleep(2)
                return
            if attempt < 4:
                time.sleep((attempt + 1) * 60)
        # One final read catches an ambiguous last upload result.
        asset = self.snapshot_assets().get(path.name)
        if not asset or not self.matches(asset, path):
            raise RuntimeError(f"upload failed: {path.name}")

    def snapshot_assets(self):
        release_id = int(run(
            "gh", "api",
            f"{self.api}/releases/tags/{TAG}",
            "--jq", ".id", capture_output=True, text=True,
        ).stdout)
        pages = json.loads(run(
            "gh", "api", "--paginate", "--slurp",
            f"{self.api}/releases/{release_id}/assets?per_page=100",
            capture_output=True, text=True,
        ).stdout)
        return {asset["name"]: asset for page in pages for asset in page}

    def delete(self, name):
        run("gh", "release", "delete-asset", TAG, name,
            "--repo", self.repository, "--yes")


def bump_pkgrel(pkgbuild, srcinfo, snapshot):
    state = read_json(snapshot)
    if not isinstance(state.get("assets"), list):
        raise ValueError("missing published asset inventory")
    fields = {}
    for line in Path(srcinfo).read_text().splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep:
            fields.setdefault(key, []).append(value)
    version = fields["pkgver"][0]
    release = fields["pkgrel"][0]
    epoch = fields.get("epoch", ["0"])[0]
    variants = {version}
    if epoch != "0":
        variants.update({f"{epoch}_{version}", f"{epoch}:{version}"})
    prefixes = tuple(f"{name}-{variant}-" for name in fields["pkgname"] for variant in variants)
    arches = "|".join(re.escape(arch) for arch in set(fields["arch"] + ["any"]))
    suffix = re.compile(rf"(\d+(?:\.\d+)?)-(?:{arches})\.pkg\.tar\.zst(?:\.sig)?\Z")

    def parts(value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError(f"unsupported pkgrel: {value}")
        major, _, minor = value.partition(".")
        return int(major), int(minor or "0")

    published = []
    for asset in state["assets"]:
        name = asset["name"]
        for prefix in prefixes:
            if name.startswith(prefix) and (match := suffix.fullmatch(name[len(prefix):])):
                published.append(parts(match[1]))
    if not published or parts(release) > max(published):
        return
    major, minor = max(published)
    new_release = f"{major}.{minor + 1}"
    # Override the assignment in the temporary build copy, before any global
    # depends=("sibling=$pkgver-$pkgrel") expressions are evaluated. Appending
    # an override would leave those dependencies pointing to the old release.
    # Do this AFTER prepare()/pkgver(), which resets pkgrel on VCS updates.
    recipe, count = re.subn(r"(?m)^pkgrel=.*$", f"pkgrel={new_release}", Path(pkgbuild).read_text())
    if count != 1:
        raise ValueError("expected one top-level pkgrel assignment in PKGBUILD")
    Path(pkgbuild).write_text(recipe)
    print(f"  rebuild pkgrel: {release} -> {new_release}")


def validate_output(out):
    current = database(out / "mainstream.db")
    if database(out / "mainstream.files") != current:
        raise ValueError("package and file databases disagree")
    packages = []
    for name, record in current.items():
        path = out / name
        if path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
            raise ValueError(f"package does not match database: {name}")
        run("gpg", "--batch", "--verify", str(path) + ".sig", str(path))
        packages.extend([path, out / (name + ".sig")])
    metadata = [out / name for name in ("mainstream.files.sig", "mainstream.files",
                                        "mainstream.db.sig", "mainstream.db")]
    for path in (out / "mainstream.files", out / "mainstream.db"):
        run("gpg", "--batch", "--verify", str(path) + ".sig", str(path))
    return current, packages, metadata


def retention_plan(state, current, now):
    retired = {}
    expired = []
    for asset in state["assets"]:
        name = asset["name"]
        if not PACKAGE.fullmatch(name):
            continue
        package = name.removesuffix(".sig")
        if package in current:
            continue
        # Start the grace period at retirement, NOT at upload time: an old
        # build may have remained current until this very publication.
        timestamp = now if package in state["current"] else state["retired"].get(package, now)
        if timestamp > now:
            timestamp = now
        if now - timestamp >= RETENTION:
            expired.append(name)
        else:
            retired[package] = timestamp
    return {"version": 1, "retired": retired}, expired


def publish(github, out):
    current, packages, metadata = validate_output(out)
    state = github.snapshot()
    assets = {asset["name"]: asset for asset in state["assets"]}
    # Complete collision preflight before uploading or changing any metadata.
    for path in packages:
        if path.name in assets and not github.matches(assets[path.name], path):
            raise ValueError(f"refusing to replace published package/signature: {path.name}; bump pkgrel")
    now = int(time.time())
    ledger, expired = retention_plan(state, current, now)
    names = set(assets) - set(expired)
    names.update(path.name for path in packages + metadata)
    names.add(LEDGER)
    if len(names) > ASSET_LIMIT:
        raise ValueError(f"release would need {len(names)}/{ASSET_LIMIT} assets; "
                         "publication stopped without shortening the 30-day retention period")
    if state["release"] is None:
        run("gh", "release", "create", TAG, "--repo", github.repository,
            "--title", "[mainstream] pacman repo", "--notes", "Signed [mainstream] pacman repository.")
    # Expired files may be removed to make room, but never delete a package
    # referenced by either the live database or the proposed database.
    for name in expired:
        if name.removesuffix(".sig") in state["current"]:
            raise ValueError(f"refusing to delete a live package: {name}")
        github.delete(name)
    for path in packages:
        github.upload(path)
    uploaded = github.snapshot_assets()
    for path in packages:
        if path.name not in uploaded or not github.matches(uploaded[path.name], path):
            raise ValueError(f"published package verification failed: {path.name}")
    # DB is last: no failure above can publish references to missing packages.
    for path in metadata:
        github.upload(path, mutable=True)
    ledger_path = out / LEDGER
    write_json(ledger_path, ledger)
    github.upload(ledger_path, mutable=True)
    uploaded = github.snapshot_assets()
    for path in metadata + [ledger_path]:
        if path.name not in uploaded or not github.matches(uploaded[path.name], path):
            raise ValueError(f"published metadata verification failed: {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("output", type=Path)
    bump = commands.add_parser("bump-pkgrel")
    bump.add_argument("pkgbuild", type=Path)
    bump.add_argument("srcinfo", type=Path)
    bump.add_argument("snapshot", type=Path)
    publication = commands.add_parser("publish")
    publication.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.command == "bump-pkgrel":
        bump_pkgrel(args.pkgbuild, args.srcinfo, args.snapshot)
    else:
        github = GitHub(os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"))
        if args.command == "snapshot":
            write_json(args.output, github.snapshot())
        else:
            publish(github, args.out)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, RuntimeError) as error:
        sys.exit(f"repository safety check failed: {error}")
