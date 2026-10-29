# MainstreamOS `[mainstream]` package repo

A signed pacman repository of FOSS packages that would otherwise come from the
AUR. It exists so MainstreamOS installs **without an AUR helper** (no yay/paru):

- the **dots-hyprland** installer adds this repo and uses `pacman -S`
- **archiso** can consume the same outputs instead of rebuilding from the AUR

GitHub Actions builds the packages in an Arch container, signs them with the
maintainer key, and publishes the repo database + packages as **Release assets**
under a fixed tag, giving pacman a stable `Server` URL.

## Layout

| File | Purpose |
|------|---------|
| `packages.list` | Source of truth: one AUR package per line (`name` or `name::pkgbase`). **FOSS only** — no proprietary/non-redistributable packages. |
| `build-repo.sh` | Builds every listed package (no AUR helper), signs them, and assembles `out/mainstream.db` + `*.pkg.tar.zst`. |
| `scripts/repo_safety.py` | Assigns rebuild subreleases, verifies signed outputs, and publishes immutable packages with a retention ledger. |
| `scripts/mirror-repo.sh` | Checks existing mirror files, uploads missing packages, verifies them, then updates databases. |
| `.github/workflows/build.yml` | CI: build → sign → publish to the `mainstream-repo` Release. |
| `keys/mainstream.pub` | Public half of the signing key (committed; clients import it). |

## How clients consume it

The repo's `Server` URL is the Release download base:

```
https://github.com/MainstreamOS/packages/releases/download/mainstream-repo
```

Each client trusts the key once, then adds the repo (`SigLevel = Required`):

```bash
sudo pacman-key --add /path/to/keys/mainstream.pub
sudo pacman-key --lsign-key <KEY_ID>

# append to /etc/pacman.conf (idempotently)
[mainstream]
SigLevel = Required
Server = https://github.com/MainstreamOS/packages/releases/download/mainstream-repo
```

Then `pacman -Sy` and the listed packages install with plain `pacman -S`.

## Publishing safety

CI snapshots **all** published asset names before building, including files left
by interrupted runs. After source preparation and VCS `pkgver()` updates, a
rebuild of an existing version gets the next available `pkgrel` subrelease:
`1` → `1.1` → `1.2`. Split packages share that subrelease. A newer upstream
version or higher upstream `pkgrel` keeps its original release number if its
filename has never been published. These overrides affect temporary build
copies, not the upstream PKGBUILDs. This follows Arch's
[pkgrel convention](https://man.archlinux.org/man/PKGBUILD.5.en#pkgrel).

Package archives **and their detached signatures are immutable** on GitHub and
SourceForge. An identical file can be reused; different contents under an
existing name stop publication. The publisher checks package sizes, SHA-256
checksums, and GPG signatures against the generated databases. It then uploads
and verifies every payload before updating metadata, with the package database
last. Workflow runs are serialized rather than cancelled midway through uploads.

GitHub retains superseded packages for **at least 30 days after they leave the
database**, rather than 30 days after their original upload. The
`retained-packages.json` release asset records retirement times. Older files
without a retirement record get a full grace period when first encountered.
Current packages are never pruned, even if their build is months old. SourceForge
keeps superseded packages without automatic deletion, so a stale fallback
database continues to work. Neither publisher uses a blanket mirror deletion.

GitHub allows [1,000 assets per release](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
If publication would exceed that limit
after pruning eligible retired files, it stops before changing the database;
it never shortens retention to make space. Review storage/release organization
if this guard triggers. Do not manually replace an archive or its signature:
publish a rebuild with a new `pkgrel` instead. For manual signed builds, first
obtain an inventory and pass it to the build:

```bash
python scripts/repo_safety.py snapshot /tmp/mainstream-published-assets.json
PUBLISHED_ASSETS=/tmp/mainstream-published-assets.json GPGKEY=<KEY_ID> ./build-repo.sh
python scripts/repo_safety.py publish out
```

These commands require authenticated `gh` access to this repository. Published
files must be signed with the repository key; unsigned local builds cannot be
published by this helper. Users affected by an older overwritten build can
refresh their stale repository metadata with `sudo pacman -Syyu`. Retention
protects cached databases within the grace period, not indefinitely offline
GitHub clients.

## Local test build (unsigned)

```bash
./build-repo.sh          # GPGKEY unset → unsigned repo in ./out (testing only)
```

Run inside an Arch environment as a non-root user with passwordless `sudo pacman`.

## Adding a package

Append it to `packages.list` (use `name::pkgbase` if the AUR git repo name
differs from the package name) and push — CI rebuilds and republishes. **Verify
the license is FOSS first**: proprietary fonts/apps (e.g. `ttf-google-sans`,
DaVinci Resolve, Plex) must not be hosted/redistributed.

Use `name::mainstream-git` for a standalone recipe maintained in
`MainstreamOS/name` on its `mainstream` branch. The builder clones the recipe,
including its patches and hook files, and applies the same signing, rebuild
versioning and retention rules. `gtk4-live-css` uses this path; its fork tracks
Arch's stable GTK packaging and tests live CSS reloads before updating the
recipe. A six-hour GTK watcher requests a signed repository build once a newer
stable GTK recipe has passed the fork's tests, avoiding duplicate active builds.
The package replaces stock `gtk4` during a normal full system upgrade.
The live-CSS concept and original patch are by
[saneaspect](https://github.com/saneaspect), from
[gtk4-live-css](https://github.com/saneaspect/gtk4-live-css).
