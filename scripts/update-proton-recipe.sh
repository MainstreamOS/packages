#!/usr/bin/env bash
# update-proton-recipe.sh <tag>: point pkgbuilds/proton-mainstream at a
# published proton-mainstream release. Rewrites pkgver and sha512sums from the
# release's .sha512sum asset and lists the package for the repo build. It never
# commits; review the diff and commit it yourself.
#
# Env:
#   _release_url  release download base (default: the GitHub releases of
#                 MainstreamOS/proton-mainstream), the same knob the recipe has.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECIPE="$ROOT/pkgbuilds/proton-mainstream/PKGBUILD"
LIST="$ROOT/packages.list"
ENTRY="proton-mainstream::local"
: "${_release_url:=https://github.com/MainstreamOS/proton-mainstream/releases/download}"

die() { echo "update-proton-recipe: $*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $0 proton-mainstream-<ver>   (e.g. proton-mainstream-11.14 or proton-mainstream-11.14.1)"
tag="$1"
[[ "$tag" =~ ^proton-mainstream-([0-9]+\.[0-9]+(\.[1-9][0-9]*)?)$ ]] \
    || die "'$tag' is not a release tag (proton-mainstream-<major>.<minor> or proton-mainstream-<major>.<minor>.<respin>)"
ver="${BASH_REMATCH[1]}"
asset="proton-mainstream-$ver.tar.gz"
[ -f "$RECIPE" ] || die "missing $RECIPE"

sumfile="$(curl -fsSL --retry 3 --retry-delay 3 "$_release_url/$tag/proton-mainstream-$ver.sha512sum")" \
    || die "could not download $_release_url/$tag/proton-mainstream-$ver.sha512sum"
[ "$(printf '%s\n' "$sumfile" | grep -c .)" -eq 1 ] || die "the .sha512sum asset should hold exactly one line"
[[ "$sumfile" =~ ^([0-9a-f]{128})[[:space:]]+\*?([^[:space:]/]+)[[:space:]]*$ ]] \
    || die "the .sha512sum asset is not '<sha512>  <file>'"
sum="${BASH_REMATCH[1]}"
[ "${BASH_REMATCH[2]}" = "$asset" ] || die "the .sha512sum asset names '${BASH_REMATCH[2]}', expected '$asset'"

old_ver="$(sed -nE 's/^pkgver=(.*)$/\1/p' "$RECIPE")"
old_rel="$(sed -nE 's/^pkgrel=(.*)$/\1/p' "$RECIPE")"
old_sum="$(sed -nE "s/^sha512sums=\('([0-9a-f]+)'\)$/\1/p" "$RECIPE")"
[ -n "$old_ver" ] && [ -n "$old_rel" ] && [ -n "$old_sum" ] \
    || die "the recipe's pkgver, pkgrel or one-line sha512sums=('...') is not where this script expects it"

listed=false
grep -Eq "^[[:space:]]*${ENTRY}[[:space:]]*(#.*)?$" "$LIST" && listed=true

if [ "$old_ver" != "$ver" ]; then
    new_rel=1
elif [ "$old_sum" != "$sum" ] && $listed; then
    # The repo build reuses a published package when pkgver and pkgrel match,
    # so a changed asset under the same version needs a new release number.
    new_rel=$(( ${old_rel%%.*} + 1 ))
    echo "warning: the $tag tarball changed since the recipe was pinned; pkgrel $old_rel -> $new_rel" >&2
else
    new_rel="$old_rel"
fi

if [ "$old_ver" = "$ver" ] && [ "$old_sum" = "$sum" ]; then
    echo "recipe already at $ver ($old_rel)"
else
    sed -i -E \
        -e "s/^pkgver=.*/pkgver=$ver/" \
        -e "s/^pkgrel=.*/pkgrel=$new_rel/" \
        -e "s/^sha512sums=\('[0-9a-f]+'\)$/sha512sums=('$sum')/" \
        "$RECIPE"
    echo "recipe: $old_ver-$old_rel -> $ver-$new_rel"
fi

if ! $listed; then
    anchor='^# ── Gaming '
    grep -q "$anchor" "$LIST" || die "packages.list has no Gaming section to add $ENTRY to"
    # Appends after the last entry of the Gaming section.
    awk -v anchor="$anchor" -v entry="$ENTRY" '
        $0 ~ anchor { in_sec = 1 }
        in_sec && /^$/ && !done { print entry; done = 1; in_sec = 0 }
        { print }
        END { if (in_sec && !done) print entry }
    ' "$LIST" > "$LIST.tmp" && mv "$LIST.tmp" "$LIST"
    echo "packages.list: added $ENTRY"
fi
