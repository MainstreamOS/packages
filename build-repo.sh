#!/usr/bin/env bash
# build-repo.sh — build the signed [mainstream] pacman repo from packages.list.
#
# For each listed AUR package: clone its pkgbase, install its OFFICIAL build
# and runtime deps with pacman (no AUR helper — our list has no AUR-only build
# deps that the outputs actually need), build with makepkg, sign it, then
# assemble a pacman repo database in ./out/.
#
# Designed to run in an Arch container (archlinux:base-devel) as a NON-root
# user with passwordless sudo for pacman — makepkg refuses to run as root.
#
# Env:
#   GPGKEY   key id/fingerprint to sign each package and the db with. When set,
#            the client uses SigLevel = Required. If unset, an UNSIGNED repo is
#            built (local testing only — never publish unsigned).
#   REPO     repo name (default: mainstream)
#   OUTDIR   output directory (default: ./out)

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-mainstream}"
OUTDIR="${OUTDIR:-$ROOT/out}"
LIST="$ROOT/packages.list"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -f "$LIST" ] || { echo "missing $LIST" >&2; exit 1; }
rm -rf "$OUTDIR"; mkdir -p "$OUTDIR"

# Refresh the sync dbs once so dependency installs resolve.
sudo pacman -Sy --noconfirm >/dev/null

mapfile -t entries < <(sed -E 's/#.*//; s/^[[:space:]]+//; s/[[:space:]]+$//' "$LIST" | grep -v '^$')

built=0; failed=0; failures=()
for entry in "${entries[@]}"; do
    name="${entry%%::*}"
    base="${entry##*::}"          # equals name when there is no ::
    if [ "$base" = local ]; then
        # Build from a PKGBUILD shipped in this repo (pkgbuilds/<name>/), copied
        # into the work dir so makepkg's src/pkg output doesn't dirty the repo.
        echo "════ $name  (local PKGBUILD) ════"
        if [ ! -f "$ROOT/pkgbuilds/$name/PKGBUILD" ]; then
            echo "!! missing local PKGBUILD: pkgbuilds/$name"; failed=$((failed+1)); failures+=("$name(local)"); continue
        fi
        dir="$WORK/$name"; cp -r "$ROOT/pkgbuilds/$name" "$dir"
    else
        echo "════ $name  (aur repo: $base) ════"
        dir="$WORK/$base"
        if ! git clone --depth=1 "https://aur.archlinux.org/$base.git" "$dir" 2>&1; then
            echo "!! clone failed: $base"; failed=$((failed+1)); failures+=("$name(clone)"); continue
        fi
    fi

    # Install build/runtime deps that resolve from the official repos. Provider-
    # aware (pacman -Sp), so virtuals like cargo (provided by rust, needed by
    # topgrade) install too; AUR-only deps are skipped — we don't pull the AUR.
    deps="$(cd "$dir" && makepkg --printsrcinfo 2>/dev/null \
        | sed -nE 's/^[[:space:]]*(depends|makedepends) = //p' | sed -E 's/[<>=:].*//' | sort -u)"
    official=()
    for d in $deps; do pacman -Sp "$d" </dev/null >/dev/null 2>&1 && official+=("$d"); done
    if [ ${#official[@]} -gt 0 ]; then
        sudo pacman -S --needed --noconfirm --asdeps "${official[@]}" || true
    fi

    sign=(); [ -n "${GPGKEY:-}" ] && sign=(--sign --key "$GPGKEY")
    if ( cd "$dir" && PKGDEST="$OUTDIR" makepkg -f --noconfirm --nodeps --skippgpcheck "${sign[@]}" 2>&1 ); then
        built=$((built+1))
    else
        echo "!! build failed: $name"; failed=$((failed+1)); failures+=("$name(build)")
    fi
done

# Keep only the packages named in packages.list. Split AUR bases emit sibling
# packages (qt5-avif-image-plugin alongside qt6; material-icons/woff2 fonts
# alongside the symbols font) and makepkg's default debug option adds *-debug —
# none of those belong in the published repo.
shopt -s nullglob
wanted=""; for e in "${entries[@]}"; do wanted="$wanted ${e%%::*}"; done; wanted=" $wanted "
for f in "$OUTDIR"/*.pkg.tar.zst; do
    pn="$(pacman -Qpq "$f" 2>/dev/null)"
    case "$wanted" in
        *" $pn "*) ;;
        *) echo "  dropping unlisted package: $(basename "$f")"; rm -f "$f" "$f.sig" ;;
    esac
done

# GitHub Release assets can't carry the ':' epoch separator: the upload mangles
# it (1:1.2.0 -> 1.1.2.0) and the prune step in build.yml then deletes the
# mismatched asset, so any epoch package (e.g. nautilus-admin-gtk4-1:1.2.0-2)
# 404s at install time. Rename epoch packages to a colon-free filename BEFORE
# repo-add so the db's %FILENAME% matches the colon-free asset we publish. The
# epoch stays in the package's internal %VERSION% (pacman orders from there), so
# only the download filename changes; consumers normalise ':'->'_' to match.
for f in "$OUTDIR"/*:*.pkg.tar.zst; do
    [ -e "$f" ] || continue
    safe="$(dirname "$f")/$(basename "$f" | tr ':' '_')"
    echo "  epoch package: $(basename "$f") -> $(basename "$safe")"
    mv -f "$f" "$safe"
    [ -e "$f.sig" ] && mv -f "$f.sig" "$safe.sig"
done

pkgs=("$OUTDIR"/*.pkg.tar.zst)
[ ${#pkgs[@]} -gt 0 ] || { echo "no packages produced" >&2; exit 1; }

rm -f "$OUTDIR/$REPO".db* "$OUTDIR/$REPO".files*
if [ -n "${GPGKEY:-}" ]; then
    repo-add --sign --key "$GPGKEY" "$OUTDIR/$REPO.db.tar.gz" "${pkgs[@]}"
else
    repo-add "$OUTDIR/$REPO.db.tar.gz" "${pkgs[@]}"
fi

# GitHub release assets can't be symlinks, so publish real files named exactly
# as pacman requests them: <repo>.db and <repo>.files (+ .sig).
cp -f "$OUTDIR/$REPO.db.tar.gz"   "$OUTDIR/$REPO.db"
cp -f "$OUTDIR/$REPO.files.tar.gz" "$OUTDIR/$REPO.files" 2>/dev/null || true
[ -f "$OUTDIR/$REPO.db.tar.gz.sig" ] && cp -f "$OUTDIR/$REPO.db.tar.gz.sig" "$OUTDIR/$REPO.db.sig"

echo "────────────────────────────────────────"
echo "built=$built  failed=$failed  →  $OUTDIR"
[ "$failed" -eq 0 ] || { printf 'failures: %s\n' "${failures[*]}" >&2; exit 1; }
