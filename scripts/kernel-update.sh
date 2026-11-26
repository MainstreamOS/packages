#!/usr/bin/env bash
# kernel-update.sh: move the kernel recipes to a newer Arch linux of the same series. Used by
# .github/workflows/kernel-update.yml, and by hand the same way.
#
#   kernel-update.sh prepare <archpkg>   rebase the fork (rebase-kernel.sh), take Arch's config, update the
#                                        recipes in this checkout; writes OUT/update.env, OUT/recipes.patch,
#                                        OUT/summary.md, and for a new release OUT/src/*.tar.gz, OUT/fork.bundle
#   kernel-update.sh push                push the new fork branches and tags from OUT/fork.bundle, then check
#                                        that GitHub's tag archives are the trees the recipes name
#   kernel-update.sh commit              commit the recipe changes in this checkout and push them
#
# A failure writes OUT/failure.md, with the stage first, for the issue the workflow opens.
#
# Env: OUT (required), FORK (default OUT/linux), FORK_URL, ARCH_URL, BRANCH_PREFIX (rebase-kernel.sh),
#      ARCH_PACKAGING (Arch's linux packaging repository), KERNEL_FORK_TOKEN (push only),
#      BRANCH and GH_TOKEN (commit only: the branch to push to, default master, and the token to push with).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${ARCH_PACKAGING:=https://gitlab.archlinux.org/archlinux/packaging/packages/linux}"
: "${FORK_URL:=https://github.com/MainstreamOS/linux.git}"
RECIPES=(mainstream mainstream-performance mainstream-performance-eac)

[ -n "${OUT:-}" ] || { echo "kernel-update: OUT is not set" >&2; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
: "${FORK:=$OUT/linux}"
export OUT FORK FORK_URL

stage=decide
fail() {
    {
        echo "stage: $stage"
        echo
        printf '%s\n' "$@"
    } > "$OUT/failure.md"
    echo "kernel-update ($stage): $*" >&2
    exit 1
}
field() { sed -nE "s/^$1=(.*)$/\1/p" "$ROOT/pkgbuilds/$2/PKGBUILD"; }

version_key() {
    # 7.3.6.arch1-2 -> "7 3 6 1 2 0", sortable by sort -n on each column
    [[ "$1" =~ ^([0-9]+)\.([0-9]+)(\.([0-9]+))?\.arch([0-9]+)-([0-9]+)(\.([0-9]+))?$ ]] || return 1
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[4]:-0} ${BASH_REMATCH[5]} ${BASH_REMATCH[6]} ${BASH_REMATCH[8]:-0}"
}
newer() {
    # newer A B: A is a later Arch package than B
    local a b
    a="$(version_key "$1")" b="$(version_key "$2")"
    [ "$a" != "$b" ] && [ "$(printf '%s\n%s\n' "$a" "$b" | sort -k1,1n -k2,2n -k3,3n -k4,4n -k5,5n -k6,6n | tail -1)" = "$a" ]
}

arch_file() {
    # arch_file <archpkg> <file> <dest>: a file of Arch's linux recipe at the package's tag
    curl -fsSL --retry 3 --retry-delay 5 -o "$3" "$ARCH_PACKAGING/-/raw/$1/$2"
}

arch_config() {
    # The config.x86_64 of an Arch package, checked against the b2sum in that package's own PKGBUILD.
    local archpkg="$1" dest="$2" work sum
    work="$(mktemp -d)"
    arch_file "$archpkg" PKGBUILD "$work/PKGBUILD" || { rm -rf "$work"; return 1; }
    arch_file "$archpkg" config.x86_64 "$dest" || { rm -rf "$work"; return 1; }
    sum="$(sed -nE "s/^b2sums_x86_64=\('([0-9a-f]{128})'\)$/\1/p" "$work/PKGBUILD")"
    rm -rf "$work"
    [ -n "$sum" ] || { echo "Arch's $archpkg PKGBUILD has no single b2sums_x86_64 entry" >&2; return 1; }
    [ "$(b2sum "$dest" | cut -d' ' -f1)" = "$sum" ] \
        || { echo "Arch's $archpkg config.x86_64 does not match its PKGBUILD's b2sum" >&2; return 1; }
}

prepare() {
    local archpkg="$1" new_ver old_ver old_arch kind name old_sum new_sum count rc
    new_ver="${archpkg%-*}"
    version_key "$archpkg" >/dev/null || fail "'$archpkg' is not an Arch kernel package version (7.3.6.arch1-1)."

    old_ver="$(field pkgver mainstream)" old_arch="$(field _archpkg mainstream)"
    for name in "${RECIPES[@]}"; do
        [ "$(field pkgver "$name")" = "$old_ver" ] && [ "$(field _archpkg "$name")" = "$old_arch" ] \
            || fail "pkgbuilds/$name is not on Arch's $old_arch like pkgbuilds/mainstream; the recipes have to move together."
    done
    version_key "$old_arch" >/dev/null || fail "pkgbuilds/mainstream has _archpkg=$old_arch, not <pkgver>-<pkgrel>."

    if ! newer "$archpkg" "$old_arch"; then
        echo "The recipes are on Arch's $old_arch; $archpkg is not newer, nothing to do."
        printf 'KIND=none\nARCHPKG=%s\nOLD_ARCHPKG=%s\n' "$archpkg" "$old_arch" > "$OUT/update.env"
        return 0
    fi
    [ "$(cut -d' ' -f1-2 <<<"$(version_key "$archpkg")")" = "$(cut -d' ' -f1-2 <<<"$(version_key "$old_arch")")" ] \
        || fail "$old_arch to $archpkg is a new series; its picks are taken again by hand, not rebased."
    if [ "$new_ver" = "$old_ver" ]; then kind=rebuild; else kind=release; fi
    echo "Moving the kernel recipes from Arch's $old_arch to $archpkg ($kind)."

    stage=config
    local cfg="$OUT/config" old_default old_perf
    mkdir -p "$cfg"
    arch_config "$old_arch" "$cfg/old" 2>"$cfg/err" || fail "Arch's config for $old_arch could not be read:" "$(cat "$cfg/err")"
    for name in "${RECIPES[@]}"; do
        cmp -s "$cfg/old" "$ROOT/pkgbuilds/$name/config" \
            || fail "pkgbuilds/$name/config is not Arch's config for $old_arch, so taking Arch's new config would drop a change made here. Move it by hand."
    done
    arch_config "$archpkg" "$cfg/new" 2>"$cfg/err" || fail "Arch's config for $archpkg could not be read:" "$(cat "$cfg/err")"

    old_default="$(field _tag mainstream)" old_perf="$(field _tag mainstream-performance)"
    local default_tag="$old_default" perf_tag="$old_perf" srcurl=""
    if [ "$kind" = release ]; then
        stage=rebase
        set +e
        bash "$ROOT/scripts/rebase-kernel.sh" "$archpkg" 2>&1 | tee "$OUT/rebase.log"
        rc=${PIPESTATUS[0]}
        set -e
        if [ "$rc" -eq 2 ]; then
            fail "$(cat "$OUT/rebase.md")"
        elif [ "$rc" -ne 0 ]; then
            fail "The rebase script failed:" '```' "$(tail -n 30 "$OUT/rebase.log")" '```'
        fi
        # shellcheck source=/dev/null
        . "$OUT/rebase.env"
        default_tag="$DEFAULT_TAG" perf_tag="$PERF_TAG" srcurl="file://$OUT/src"
    else
        stage=recipe
        # An Arch rebuild keeps the source; the recipe script still checks the tags' trees in a fork clone.
        if [ ! -e "$FORK" ]; then
            git init -q "$FORK"
            git -C "$FORK" remote add origin "$FORK_URL"
            git -C "$FORK" config remote.origin.promisor true
            git -C "$FORK" config remote.origin.partialclonefilter blob:none
        fi
        git -C "$FORK" fetch -q --no-tags origin "refs/tags/$old_default:refs/tags/$old_default" \
            "refs/tags/$old_perf:refs/tags/$old_perf" || fail "Could not fetch $old_default and $old_perf from the fork."
    fi

    stage=recipe
    for name in "${RECIPES[@]}"; do
        old_sum="$(sha256sum "$ROOT/pkgbuilds/$name/config" | cut -d' ' -f1)"
        new_sum="$(sha256sum "$cfg/new" | cut -d' ' -f1)"
        count="$(grep -c "'$old_sum'" "$ROOT/pkgbuilds/$name/PKGBUILD" || true)"
        [ "$count" = 1 ] || fail "pkgbuilds/$name/PKGBUILD does not list the config's sha256 once."
        cp "$cfg/new" "$ROOT/pkgbuilds/$name/config"
        sed -i "s/'$old_sum'/'$new_sum'/" "$ROOT/pkgbuilds/$name/PKGBUILD"
    done
    for tag in "$default_tag" "$perf_tag"; do
        if [ -n "$srcurl" ]; then
            _srcurl="$srcurl" bash "$ROOT/scripts/update-kernel-recipe.sh" "$tag" "$archpkg" 2>&1 | tee -a "$OUT/recipe.log" \
                || fail "update-kernel-recipe.sh $tag failed:" '```' "$(tail -n 20 "$OUT/recipe.log")" '```'
        else
            bash "$ROOT/scripts/update-kernel-recipe.sh" "$tag" "$archpkg" 2>&1 | tee -a "$OUT/recipe.log" \
                || fail "update-kernel-recipe.sh $tag failed:" '```' "$(tail -n 20 "$OUT/recipe.log")" '```'
        fi
    done
    if command -v makepkg >/dev/null; then
        for name in "${RECIPES[@]}"; do
            (cd "$ROOT/pkgbuilds/$name" && makepkg --printsrcinfo >/dev/null) || fail "pkgbuilds/$name does not parse after the update."
        done
    fi
    git -C "$ROOT" diff --binary -- "${RECIPES[@]/#/pkgbuilds/}" > "$OUT/recipes.patch"

    {
        echo "KIND=$kind"
        echo "ARCHPKG=$archpkg"
        echo "OLD_ARCHPKG=$old_arch"
        echo "PKGVER=$(field pkgver mainstream)"
        echo "PKGREL=$(field pkgrel mainstream)"
        echo "OLD_DEFAULT_TAG=$old_default"
        echo "OLD_PERF_TAG=$old_perf"
        echo "DEFAULT_TAG=$default_tag"
        echo "PERF_TAG=$perf_tag"
        echo "SRCURL=$srcurl"
        if [ "$kind" = release ]; then grep -E '^(DEFAULT_BRANCH|PERF_BRANCH|NEW_BASE|REUSED)=' "$OUT/rebase.env"; fi
    } > "$OUT/update.env"

    {
        echo "Arch's linux $old_arch to $archpkg ($([ "$kind" = release ] && echo "new release" || echo "Arch rebuild, same source"))."
        echo
        echo "- \`mainstream\`: \`$default_tag\`"
        echo "- \`mainstream-performance\` and \`-eac\`: \`$perf_tag\`"
        echo "- Config: Arch's \`config.x86_64\` of $archpkg."
        if [ "$kind" = release ]; then
            echo
            echo "### Rebase"
            cat "$OUT/rebase.md"
        fi
    } > "$OUT/summary.md"
    cat "$OUT/summary.md"
}

push() {
    stage=publish
    # shellcheck source=/dev/null
    . "$OUT/update.env"
    [ "$KIND" = release ] || { echo "An Arch rebuild has nothing to push to the fork."; return 0; }
    [ -n "${KERNEL_FORK_TOKEN:-}" ] || fail "KERNEL_FORK_TOKEN is not set; the fork tags cannot be pushed."
    if [ ! -e "$FORK" ]; then
        git init -q "$FORK"
        git -C "$FORK" remote add origin "$FORK_URL"
        git -C "$FORK" config remote.origin.promisor true
        git -C "$FORK" config remote.origin.partialclonefilter blob:none
    fi
    local g=(git -C "$FORK") refs=() ref reused auth
    "${g[@]}" fetch -q --no-tags origin "refs/tags/$OLD_DEFAULT_TAG:refs/tags/$OLD_DEFAULT_TAG" \
        "refs/tags/$OLD_PERF_TAG:refs/tags/$OLD_PERF_TAG" || fail "Could not fetch $OLD_DEFAULT_TAG and $OLD_PERF_TAG from the fork."
    "${g[@]}" bundle verify -q "$OUT/fork.bundle" || fail "The fork bundle does not apply to the fork's $OLD_DEFAULT_TAG and $OLD_PERF_TAG."
    "${g[@]}" fetch -q --no-tags "$OUT/fork.bundle" 'refs/tags/*:refs/tags/*' 'refs/heads/*:refs/heads/*' \
        || fail "Could not read the fork bundle."
    for ref in "refs/tags/$DEFAULT_TAG" "refs/tags/$PERF_TAG" "refs/heads/$DEFAULT_BRANCH" "refs/heads/$PERF_BRANCH"; do
        reused=no
        for r in $REUSED; do [ "$ref" = "refs/tags/$r" ] && reused=yes; done
        [ "$reused" = no ] || continue
        "${g[@]}" rev-parse -q --verify "$ref" >/dev/null && refs+=("$ref:$ref")
    done
    if [ ${#refs[@]} -gt 0 ]; then
        auth="$(printf 'x-access-token:%s' "$KERNEL_FORK_TOKEN" | base64 -w0)"
        "${g[@]}" -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $auth" push --atomic origin "${refs[@]}" \
            || fail "Pushing ${refs[*]} to the fork failed (a branch or tag of that name may already be there)."
    fi
    # GitHub's archive of each tag must be the tree that was built; a different sum is written into the recipe.
    for tag in "$DEFAULT_TAG" "$PERF_TAG"; do
        bash "$ROOT/scripts/update-kernel-recipe.sh" "$tag" 2>&1 | tee -a "$OUT/github.log" \
            || fail "GitHub's archive of $tag does not match the built tree:" '```' "$(tail -n 20 "$OUT/github.log")" '```'
    done
}

commit() {
    stage=publish
    # shellcheck source=/dev/null
    . "$OUT/update.env"
    local branch="${BRANCH:-master}" paths=("${RECIPES[@]/#/pkgbuilds/}") try
    cd "$ROOT"
    if git diff --quiet -- "${paths[@]}"; then
        echo "The recipes already name $ARCHPKG."
        return 0
    fi
    git add -- "${paths[@]}"
    git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -q -m "kernels: move to Arch linux $ARCHPKG" \
        -m "Built from $DEFAULT_TAG and $PERF_TAG with Arch's config for $ARCHPKG." -- "${paths[@]}"
    local auth=()
    if [ -n "${GH_TOKEN:-}" ]; then
        auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)")
    fi
    for try in 1 2 3; do
        git "${auth[@]}" push -q origin "HEAD:refs/heads/$branch" && return 0
        echo "push $try was refused; taking $branch again"
        git "${auth[@]}" fetch -q origin "$branch" && git rebase -q FETCH_HEAD \
            || { git rebase --abort 2>/dev/null || true; fail "The recipe commit no longer applies to $branch."; }
    done
    fail "The recipe commit could not be pushed to $branch."
}

case "${1:-}" in
prepare) [ -n "${2:-}" ] || { echo "usage: $0 prepare <archpkg>" >&2; exit 1; }; prepare "$2" ;;
push) push ;;
commit) commit ;;
*) echo "usage: $0 prepare <archpkg> | push | commit" >&2; exit 1 ;;
esac
