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
