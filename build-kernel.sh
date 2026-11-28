#!/usr/bin/env bash
# build-kernel.sh: build the kernels and signed boot files that packages.list marks <pkgbase>::kernel
# and <pkgbase>::signed, and add them to the signed [mainstream] repo. Used by .github/workflows/kernels.yml; build-repo.sh only keeps their
# published builds.
#
#   build-kernel.sh plan              JSON list of listed kernels whose recipe version is not live
#   build-kernel.sh build <pkgbase>   build and sign one kernel into OUTDIR
#   build-kernel.sh assemble          add every live package OUTDIR does not replace, write the db
#
# Env: PUBLISHED_ASSETS (snapshot from scripts/repo_safety.py, required), GPGKEY, REPO, OUTDIR,
#      FORCE_REBUILD (names or "all": plan lists them even when live), BUILDDIR (makepkg's).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-mainstream}"
OUTDIR="${OUTDIR:-$ROOT/out}"
LIST="$ROOT/packages.list"

[ -n "${PUBLISHED_ASSETS:-}" ] && [ -r "$PUBLISHED_ASSETS" ] \
    || { echo "a readable published asset snapshot is required" >&2; exit 1; }

kernels() {
    sed -E 's/#.*//; s/^[[:space:]]+//; s/[[:space:]]+$//' "$LIST" | sed -nE 's/^([^:]+)::(kernel|signed)$/\1/p'
}

srcinfo() {
    local name="$1" dir="$2"
    [ -f "$ROOT/pkgbuilds/$name/PKGBUILD" ] || { echo "missing kernel recipe: pkgbuilds/$name" >&2; return 1; }
    rm -rf "$dir"; cp -r "$ROOT/pkgbuilds/$name" "$dir" || return 1
    ( cd "$dir" && makepkg --printsrcinfo > .SRCINFO )
}

plan() {
    local work name rc first=true
    work="$(mktemp -d)"; trap 'rm -rf "$work"' RETURN
    printf '['
    while IFS= read -r name; do
        srcinfo "$name" "$work/$name" >&2 || return 1
        python "$ROOT/scripts/repo_safety.py" is-published "$work/$name/.SRCINFO" "$PUBLISHED_ASSETS" >&2
        rc=$?
        case $rc in
        0) echo "$name: recipe version is live, nothing to build" >&2 ;;
        3) echo "$name: recipe version is not live, building" >&2
           $first || printf ','
           printf '"%s"' "$name"; first=false ;;
        *) return 1 ;;
        esac
    done < <(kernels)
    printf ']\n'
}

build() {
    local name="$1" dir deps d official=() sign=()
    kernels | grep -qxF "$name" || { echo "$name is not listed as $name::kernel or $name::signed" >&2; return 1; }
    dir="$(mktemp -d)/$name"
    srcinfo "$name" "$dir" || return 1
    deps="$(sed -nE 's/^[[:space:]]*(depends|makedepends|checkdepends) = //p' "$dir/.SRCINFO" \
        | sed -E 's/[<>=:].*//' | sort -u)"
    while IFS= read -r d; do
        [ -n "$d" ] && pacman -Sp "$d" </dev/null >/dev/null 2>&1 && official+=("$d")
    done <<<"$deps"
    [ ${#official[@]} -eq 0 ] || sudo pacman -S --needed --noconfirm --asdeps "${official[@]}" || return 1
    [ -n "${GPGKEY:-}" ] && sign=(--sign --key "$GPGKEY")
    mkdir -p "$OUTDIR"
    (
        cd "$dir" || exit 1
        export PKGDEST="$OUTDIR"
        # Before prepare(), which writes pkgrel into the kernel release string. No pkgver() to resolve.
        python "$ROOT/scripts/repo_safety.py" bump-pkgrel PKGBUILD .SRCINFO "$PUBLISHED_ASSETS" || exit 1
        makepkg -f --noconfirm --nodeps "${sign[@]}"
    )
}

assemble() {
    local pkgs
    python "$ROOT/scripts/repo_safety.py" assemble "$PUBLISHED_ASSETS" "$OUTDIR" || return 1
    shopt -s nullglob
    pkgs=("$OUTDIR"/*.pkg.tar.zst)
    rm -f "$OUTDIR/$REPO".db* "$OUTDIR/$REPO".files*
    if [ -n "${GPGKEY:-}" ]; then
        repo-add --sign --key "$GPGKEY" "$OUTDIR/$REPO.db.tar.gz" "${pkgs[@]}" || return 1
    else
        repo-add "$OUTDIR/$REPO.db.tar.gz" "${pkgs[@]}" || return 1
    fi
    # Real files rather than repo-add's symlinks, as build-repo.sh publishes them.
    cp -f --remove-destination "$OUTDIR/$REPO.db.tar.gz"    "$OUTDIR/$REPO.db"
    cp -f --remove-destination "$OUTDIR/$REPO.files.tar.gz" "$OUTDIR/$REPO.files"
    [ -f "$OUTDIR/$REPO.db.tar.gz.sig" ] && cp -f --remove-destination "$OUTDIR/$REPO.db.tar.gz.sig" "$OUTDIR/$REPO.db.sig"
    [ -f "$OUTDIR/$REPO.files.tar.gz.sig" ] && cp -f --remove-destination "$OUTDIR/$REPO.files.tar.gz.sig" "$OUTDIR/$REPO.files.sig"
    return 0
}

case "${1:-}" in
plan) plan ;;
build) [ -n "${2:-}" ] || { echo "usage: $0 build <pkgbase>" >&2; exit 1; }; build "$2" ;;
assemble) assemble ;;
*) echo "usage: $0 plan | build <pkgbase> | assemble" >&2; exit 1 ;;
esac
