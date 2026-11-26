#!/usr/bin/env bash
# update-kernel-recipe.sh <tag> [<archpkg>]: point the kernel recipes at a pushed tag of MainstreamOS/linux.
# Downloads the tag's GitHub tarball, checks that its tree equals the tag in a local clone of the fork,
# and writes pkgver, pkgrel, _archpkg, _tag and _tag_sha256 into the recipes the tag is for. It never
# commits; review the diff and commit it yourself.
#
#   <tag>      mainstream-default-<pkgver>-<rev>      for mainstream
#              mainstream-performance-<pkgver>-<rev>  for mainstream-performance and -eac
#              <pkgver> is Arch's form (7.3.5.arch1), <rev> counts the fork's revisions of that release.
#   <archpkg>  the Arch package the tag is based on (7.3.5.arch1-2); needed when pkgver changes,
#              otherwise the recipe's value is kept.
#
# Env:
#   _srcurl  tag tarball base (default: GitHub's tag archives of MainstreamOS/linux), the recipes' knob.
#   FORK     local clone of the fork holding the tag (default: ../linux next to this repository).
#   TMPDIR   where the tarball is unpacked (about 2 GB).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${_srcurl:=https://github.com/MainstreamOS/linux/archive/refs/tags}"
: "${FORK:=$ROOT/../linux}"

die() { echo "update-kernel-recipe: $*" >&2; exit 1; }

[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: $0 mainstream-<default|performance>-<pkgver>-<rev> [<archpkg>]"
tag="$1" archpkg="${2:-}"
[[ "$tag" =~ ^mainstream-(default|performance)-([0-9]+\.[0-9]+(\.[0-9]+)?\.arch[0-9]+)-([1-9][0-9]*)$ ]] \
    || die "'$tag' is not a kernel tag (mainstream-<default|performance>-<major>.<minor>[.<patch>].arch<N>-<rev>)"
flavor="${BASH_REMATCH[1]}" ver="${BASH_REMATCH[2]}"
if [ -n "$archpkg" ]; then
    [[ "$archpkg" =~ ^${ver//./\\.}-[1-9][0-9]*(\.[0-9]+)?$ ]] || die "'$archpkg' is not an Arch package of $ver (<pkgver>-<pkgrel>)"
fi
case "$flavor" in
    default) recipes=(mainstream) ;;
    performance) recipes=(mainstream-performance mainstream-performance-eac) ;;
esac
for name in "${recipes[@]}"; do
    [ -f "$ROOT/pkgbuilds/$name/PKGBUILD" ] || die "missing pkgbuilds/$name/PKGBUILD"
done

want="$(git -C "$FORK" rev-parse --verify -q "refs/tags/$tag^{tree}")" \
    || die "tag $tag is not in the fork clone at $FORK (fetch the tags first)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "downloading $_srcurl/$tag.tar.gz"
curl -fsSL --retry 3 --retry-delay 3 -o "$work/src.tar.gz" "$_srcurl/$tag.tar.gz" \
    || die "could not download $_srcurl/$tag.tar.gz (is the tag pushed?)"
sum="$(sha256sum "$work/src.tar.gz" | cut -d' ' -f1)"

mkdir "$work/x"
tar -xzf "$work/src.tar.gz" -C "$work/x" || die "the tarball does not unpack"
[ "$(ls -A "$work/x")" = "linux-$tag" ] || die "the tarball's top directory is not linux-$tag"
git init -q --bare "$work/git"
got="$(
    export GIT_DIR="$work/git" GIT_WORK_TREE="$work/x/linux-$tag" GIT_INDEX_FILE="$work/index"
    git config core.autocrlf false; git config core.filemode true; git config core.symlinks true
    cd "$GIT_WORK_TREE" && git add -A -f . && git write-tree
)"
[ "$got" = "$want" ] || die "the tarball's tree $got is not the tree of $tag in the fork ($want)"
echo "tarball tree equals $tag ($got), sha256 $sum"

local_sum="$(git -C "$FORK" archive --format=tar --prefix="linux-$tag/" "refs/tags/$tag" | gzip -n | sha256sum | cut -d' ' -f1)"
[ "$local_sum" = "$sum" ] || echo "note: a local 'git archive | gzip -n' gives $local_sum, not the downloaded sum" >&2

field() { sed -nE "s/^$1=(.*)$/\1/p" "$2"; }
for name in "${recipes[@]}"; do
    recipe="$ROOT/pkgbuilds/$name/PKGBUILD"
    old_ver="$(field pkgver "$recipe")" old_rel="$(field pkgrel "$recipe")"
    old_tag="$(field _tag "$recipe")" old_sum="$(field _tag_sha256 "$recipe")" old_arch="$(field _archpkg "$recipe")"
    [ -n "$old_ver" ] && [ -n "$old_rel" ] && [ -n "$old_tag" ] && [ -n "$old_sum" ] && [ -n "$old_arch" ] \
        || die "pkgbuilds/$name: pkgver, pkgrel, _archpkg, _tag or _tag_sha256 is not where this script expects it"

    new_arch="${archpkg:-$old_arch}"
    if [ "$old_ver" != "$ver" ]; then
        [ -n "$archpkg" ] || die "pkgver changes from $old_ver to $ver: give the Arch package as the second argument"
        new_rel=1
    elif [ "$old_tag" != "$tag" ] || [ "$old_arch" != "$new_arch" ]; then
        # The repo build reuses a published package when pkgver and pkgrel match.
        new_rel=$(( ${old_rel%%.*} + 1 ))
    else
        new_rel="$old_rel"
    fi

    if [ "$old_ver-$old_rel $old_tag $old_sum $old_arch" = "$ver-$new_rel $tag $sum $new_arch" ]; then
        echo "$name: already at $tag ($ver-$old_rel)"
        continue
    fi
    sed -i -E \
        -e "s/^pkgver=.*/pkgver=$ver/" \
        -e "s/^pkgrel=.*/pkgrel=$new_rel/" \
        -e "s/^_archpkg=.*/_archpkg=$new_arch/" \
        -e "s/^_tag=.*/_tag=$tag/" \
        -e "s/^_tag_sha256=.*/_tag_sha256=$sum/" \
        "$recipe"
    echo "$name: $old_tag ($old_ver-$old_rel) -> $tag ($ver-$new_rel), Arch $new_arch"
done
