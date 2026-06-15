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
    echo "════ $name  (aur repo: $base) ════"
    dir="$WORK/$base"
    if ! git clone --depth=1 "https://aur.archlinux.org/$base.git" "$dir" 2>&1; then
        echo "!! clone failed: $base"; failed=$((failed+1)); failures+=("$name(clone)"); continue
    fi

    # Install only OFFICIAL depends/makedepends. Any AUR makedepend (rare here,
    # e.g. 38c3-styles' html2markdown) is left out — it isn't needed for the
    # package we want and makepkg --nodeps tolerates its absence.
    deps="$(cd "$dir" && makepkg --printsrcinfo 2>/dev/null \
        | sed -nE 's/^[[:space:]]*(depends|makedepends) = //p' | sed -E 's/[<>=:].*//' | sort -u)"
    official=()
    for d in $deps; do pacman -Si "$d" >/dev/null 2>&1 && official+=("$d"); done
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

shopt -s nullglob
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
