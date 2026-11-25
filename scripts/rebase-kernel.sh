#!/usr/bin/env bash
# rebase-kernel.sh <archpkg>: move the MainstreamOS/linux branches the kernel recipes are built from
# onto a newer Arch kernel of the same series, tag them and write their source tarballs.
#
#   <archpkg>  Arch's linux package to move to, as <pkgver>-<pkgrel> (7.3.6.arch1-1).
#
# The old base comes from the recipes: _archpkg names Arch's old tag (v7.3.5-arch1), _tag the fork tags.
# Arch's new tag is fetched from ARCH_URL and its signature checked against the key pinned in
# keys/upstream. The default branch is rebased onto it, then the performance branch onto the new
# default branch. Picks that Arch's tag already carries drop out and are listed. A conflict stops the
# run, names the commit and files in $OUT/rebase.md and exits 2. A fork tag that already exists on
# the fork for the new version (pushed by an earlier run, or by hand after fixing a conflict) is used
# as it is instead of rebasing. Nothing is pushed.
#
# Env:
#   OUT            output directory (required): src/<tag>.tar.gz, rebase.md, rebase.env, fork.bundle
#   FORK           a clone of its own whose origin is the fork (its work tree is overwritten);
#                  made as a partial clone of FORK_URL when missing
#   FORK_URL       https://github.com/MainstreamOS/linux.git
#   ARCH_URL       https://github.com/archlinux/linux.git
#   BRANCH_PREFIX  prefix of the new local branches (default mainstream/)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${FORK_URL:=https://github.com/MainstreamOS/linux.git}"
: "${ARCH_URL:=https://github.com/archlinux/linux.git}"
: "${BRANCH_PREFIX:=mainstream/}"
# Jan Alexander Steffens (heftig), who signs Arch's kernel tags; from Arch's packaging repository,
# keys/pgp/<fingerprint>.asc, and listed in its PKGBUILD's validpgpkeys.
: "${ARCH_KEY:=83BC8889351B5DEBBB68416EB8AC08600F108CDF}"

die() { echo "rebase-kernel: $*" >&2; exit 1; }
field() { sed -nE "s/^$1=(.*)$/\1/p" "$ROOT/pkgbuilds/$2/PKGBUILD"; }
# 7.3.6.arch1 -> v7.3.6-arch1; 7.4.arch1 -> v7.4-arch1
arch_tag() { echo "v${1/.arch/-arch}"; }
stable() { echo "${1%%.arch*}"; }

[ $# -eq 1 ] || die "usage: $0 <archpkg>"
[ -n "${OUT:-}" ] || die "OUT is not set"
archpkg="$1"
[[ "$archpkg" =~ ^(([0-9]+)\.([0-9]+)(\.[0-9]+)?\.arch[0-9]+)-[1-9][0-9]*(\.[0-9]+)?$ ]] \
    || die "'$archpkg' is not an Arch kernel package version (7.3.6.arch1-1)"
new_ver="${BASH_REMATCH[1]}" new_series="${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"

old_ver="$(field pkgver linux-mainstream)"
old_default="$(field _tag linux-mainstream)"
old_perf="$(field _tag linux-mainstream-performance)"
[ "$(field pkgver linux-mainstream-performance)" = "$old_ver" ] \
    || die "linux-mainstream and linux-mainstream-performance are on different Arch versions"
[[ "$old_ver" =~ ^([0-9]+)\.([0-9]+)(\.[0-9]+)?\.arch[0-9]+$ ]] || die "recipe pkgver '$old_ver' is not Arch's form"
[ "${BASH_REMATCH[1]}.${BASH_REMATCH[2]}" = "$new_series" ] \
    || die "$old_ver to $new_ver is a new series; its picks are taken again by hand, not rebased"
[ "$old_ver" != "$new_ver" ] || die "the recipes are already on $new_ver; an Arch rebuild needs no rebase"
[ "$(printf '%s\n%s\n' "$old_ver" "$new_ver" | sort -V | tail -1)" = "$new_ver" ] \
    || die "$new_ver is older than the recipes' $old_ver"

old_base="$(arch_tag "$old_ver")" new_base="$(arch_tag "$new_ver")"
new_default_branch="${BRANCH_PREFIX}default-$(stable "$new_ver")"
new_perf_branch="${BRANCH_PREFIX}performance-$(stable "$new_ver")"

mkdir -p "$OUT/src"
report="$OUT/rebase.md"
: > "$report"

if [ ! -d "${FORK:=$OUT/linux}/.git" ] && [ ! -f "$FORK/HEAD" ]; then
    echo "making a partial clone of $FORK_URL in $FORK"
    git init -q "$FORK"
    git -C "$FORK" remote add origin "$FORK_URL"
    git -C "$FORK" config remote.origin.promisor true
    git -C "$FORK" config remote.origin.partialclonefilter blob:none
fi
g() { git -C "$FORK" -c core.hooksPath=/dev/null -c advice.detachedHead=false "$@"; }
# A fresh CI clone has no identity; a person's clone keeps theirs.
if [ -z "$(g config user.email || true)" ]; then
    export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-Mainstream OS kernel update}"
    export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
    export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}" GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
fi

have() { g rev-parse -q --verify "refs/tags/$1" >/dev/null; }
fetch_tags() {
    local url="$1" t specs=()
    shift
    for t in "$@"; do have "$t" || specs+=("refs/tags/$t:refs/tags/$t"); done
    [ ${#specs[@]} -eq 0 ] || g fetch -q --no-tags "$url" "${specs[@]}" || die "could not fetch ${specs[*]} from $url"
}

fetch_tags origin "$old_default" "$old_perf"
fetch_tags "$ARCH_URL" "$old_base" "$new_base"

work="$(mktemp -d)" gnupg="$(mktemp -d)"
trap 'rm -rf "$work" "$gnupg"' EXIT
GNUPGHOME="$gnupg" gpg --batch --quiet --import "$ROOT/keys/upstream/$ARCH_KEY.asc" 2>/dev/null \
    || die "could not import keys/upstream/$ARCH_KEY.asc"
for t in "$old_base" "$new_base"; do
    [ "$(g cat-file -t "refs/tags/$t")" = tag ] || die "$t is not an annotated tag"
    [ "$(g cat-file tag "refs/tags/$t" | sed -n 's/^tag //p' | head -1)" = "$t" ] || die "$t carries another tag name"
    GNUPGHOME="$gnupg" g verify-tag --raw "refs/tags/$t" 2>&1 | grep -q "^\[GNUPG:\] VALIDSIG $ARCH_KEY " \
        || die "$t is not signed by Arch's kernel maintainer key $ARCH_KEY"
done
echo "$old_base and $new_base are signed by $ARCH_KEY"

g merge-base --is-ancestor "$old_base" "$old_default" || die "$old_default is not based on $old_base"
g merge-base --is-ancestor "$old_default" "$old_perf" || die "$old_perf is not based on $old_default"

# The newest revision of a fork tag for this version on the fork, if any.
existing() {
    local flavor="$1"
    g ls-remote --tags origin "refs/tags/mainstream-$flavor-$new_ver-*" \
        | sed -nE "s@.*refs/tags/(mainstream-$flavor-${new_ver//./\\.}-([1-9][0-9]*))\$@\2 \1@p" \
        | sort -n | tail -1 | cut -d' ' -f2
}

subjects() { g log --reverse --format=%s "$1"; }
# Commits of the old range whose subject is missing from the new range: what the rebase dropped.
dropped() {
    local old="$1" new="$2"
    awk 'NR == FNR { have[$0]++; next } !(have[$0]-- > 0)' <(subjects "$new") <(subjects "$old")
}

# Arch's new commits, and the mainline commits their stable backports name.
g log --format='%H%x09%an%x09%s' "refs/tags/$old_base..refs/tags/$new_base" > "$work/upstream.tsv"
g log --format='%x1e%H%x09%B' "refs/tags/$old_base..refs/tags/$new_base" | awk -v RS='\036' -F'\t' '
    { body = $0
      while (match(body, /(commit [0-9a-f]+ upstream|Upstream commit [0-9a-f]+)/)) {
          n = split(substr(body, RSTART, RLENGTH), w, " ")
          id = (w[1] == "commit") ? w[2] : w[3]
          if (length(id) == 40) print id, $1
          body = substr(body, RSTART + RLENGTH)
      } }' > "$work/upstream-ids"

# Picks Arch's new tag carries in its own form, as "<pick> <Arch's commit>": a stable backport with the
# same author and subject, or one naming the mainline commit the pick was cherry-picked from. Applying
# such a pick again conflicts or, worse, applies twice.
carried() {
    g log --format='%x1e%H%x09%an%x09%s%x09%B' "$1" | awk -F'\t' -v up="$work/upstream.tsv" -v ids="$work/upstream-ids" '
        BEGIN {
            while ((getline l < up) > 0) { split(l, f, "\t"); bykey[f[2] "\t" f[3]] = f[1] }
            while ((getline l < ids) > 0) { split(l, f, " "); byid[f[1]] = f[2] }
            RS = "\036"
        }
        NF >= 3 {
            key = $2 "\t" $3
            if (key in bykey) { print $1, bykey[key]; next }
            body = $0
            while (match(body, /cherry picked from commit [0-9a-f]+/)) {
                id = substr(body, RSTART + 26, RLENGTH - 26)
                if (id in byid) { print $1, byid[id]; next }
                body = substr(body, RSTART + RLENGTH)
            }
        }'
}

cat > "$work/drop-picks.sh" <<'DROPEOF'
#!/usr/bin/env bash
awk -v list="$DROP_LIST" '
    BEGIN { while ((getline l < list) > 0) { split(l, f, " "); drop[f[1]] = 1 } }
    $1 == "pick" { for (c in drop) if (index(c, $2) == 1) { $1 = "drop"; break } }
    { print }' "$1" > "$1.new" && mv "$1.new" "$1"
DROPEOF

rebase() {
    local onto="$1" upstream="$2" from="$3" branch="$4" label="$5" stop files mode=()
    g branch -f "$branch" "$from"
    carried "$upstream..$from" > "$OUT/carried-$label.txt"
    [ -s "$OUT/carried-$label.txt" ] && mode=(-i)
    if ! DROP_LIST="$OUT/carried-$label.txt" GIT_SEQUENCE_EDITOR="bash $work/drop-picks.sh" GIT_EDITOR=true \
            g -c rebase.autoSquash=false -c rebase.updateRefs=false -c rebase.missingCommitsCheck=ignore \
            -c merge.conflictStyle=diff3 \
            rebase -q "${mode[@]}" --empty=drop --no-reapply-cherry-picks --no-autostash \
            --onto "$onto" "$upstream" "$branch" >"$OUT/rebase-$label.log" 2>&1; then
        stop="$(g rev-parse -q --verify REBASE_HEAD || true)"
        files="$(g diff --name-only --diff-filter=U || true)"
        {
            echo "### Rebase of the $label branch onto \`$onto\` stopped"
            echo
            if [ -n "$stop" ]; then
                echo "Commit \`$(g rev-parse --short=12 "$stop")\` $(g log -1 --format='"%s" (%an)' "$stop") does not apply."
            else
                echo "The rebase failed before a commit could be named."
            fi
            if [ -n "$files" ]; then
                echo
                echo "Conflicting files:"
                echo '```'
                echo "$files"
                echo '```'
            fi
            echo
            echo "Last lines of the rebase:"
            echo '```'
            tail -n 20 "$OUT/rebase-$label.log"
            echo '```'
        } >> "$report"
        g rebase --abort >/dev/null 2>&1 || true
        g checkout -q -f --detach "refs/tags/$new_base"
        g branch -D "$branch" >/dev/null 2>&1 || true
        cat "$report" >&2
        exit 2
    fi
}

tag_message() {
    local flavor="$1" tag="$2"
    case "$flavor" in
    default)
        printf 'linux-mainstream %s, fork revision %s\n\nBase: %s (Arch package %s).\nBranch: %s.\n' \
            "$new_ver" "${tag##*-}" "$new_base" "$archpkg" "$new_default_branch" ;;
    performance)
        printf 'linux-mainstream-performance and linux-mainstream-performance-eac %s, fork revision %s\n\nBase: %s plus the speed layer, BORE and dkms-clang.\nBranch: %s.\n' \
            "$new_ver" "${tag##*-}" "$default_tag" "$new_perf_branch" ;;
    esac
}

summary() {
    local label="$1" old="$2" new="$3" carried_list list c up
    carried_list="$OUT/carried-$label.txt"
    {
        echo "- $label: $(g rev-list --count "$old") commits before, $(g rev-list --count "$new") after."
        if [ -s "$carried_list" ]; then
            echo "  Dropped, because \`$new_base\` carries them:"
            while read -r c up; do
                echo "  - \`$(g rev-parse --short=12 "$c")\` $(g log -1 --format=%s "$c") (Arch: \`$(g rev-parse --short=12 "$up")\`)"
            done < "$carried_list"
        fi
        list="$(dropped "$old" "$new" | grep -vxF -f <(while read -r c up; do g log -1 --format=%s "$c"; done < "$carried_list") || true)"
        if [ -n "$list" ]; then
            echo "  Dropped by git, because their changes are already in \`$new_base\`:"
            while IFS= read -r c; do echo "  - $c"; done <<<"$list"
        fi
    } >> "$report"
}

# The rebase starts from a clean work tree at Arch's new tag; this also fetches its files in one batch.
g checkout -q -f --detach "refs/tags/$new_base"

reused=()
default_tag="$(existing default)"
if [ -n "$default_tag" ]; then
    fetch_tags origin "$default_tag"
    g merge-base --is-ancestor "$new_base" "$default_tag" || die "$default_tag on the fork is not based on $new_base"
    reused+=("$default_tag")
    echo "- default: \`$default_tag\` is already on the fork and is used as it is." >> "$report"
else
    rebase "$new_base" "$old_base" "$old_default" "$new_default_branch" default
    default_tag="mainstream-default-$new_ver-1"
    have "$default_tag" && die "$default_tag exists locally but not on the fork; delete it or push it first"
    g tag -a -m "$(tag_message default "$default_tag")" "$default_tag" "$new_default_branch"
    summary default "$old_base..$old_default" "$new_base..$default_tag"
fi

perf_tag="$(existing performance)"
if [ -n "$perf_tag" ]; then
    fetch_tags origin "$perf_tag"
    g merge-base --is-ancestor "$default_tag" "$perf_tag" || die "$perf_tag on the fork is not based on $default_tag"
    reused+=("$perf_tag")
    echo "- performance: \`$perf_tag\` is already on the fork and is used as it is." >> "$report"
else
    rebase "$default_tag^{commit}" "$old_default" "$old_perf" "$new_perf_branch" performance
    perf_tag="mainstream-performance-$new_ver-1"
    have "$perf_tag" && die "$perf_tag exists locally but not on the fork; delete it or push it first"
    g tag -a -m "$(tag_message performance "$perf_tag")" "$perf_tag" "$new_perf_branch"
    summary performance "$old_default..$old_perf" "$default_tag..$perf_tag"
fi

for t in "$default_tag" "$perf_tag"; do
    # A checkout fetches a partial clone's missing files in one batch; git archive would fetch them one by one.
    g checkout -q --detach "refs/tags/$t"
    g archive --format=tar --prefix="linux-$t/" "refs/tags/$t" | gzip -n > "$OUT/src/$t.tar.gz"
    echo "$t: $(sha256sum "$OUT/src/$t.tar.gz" | cut -d' ' -f1)"
done

# Everything the fork lacks, for the publish step to push once the kernels are built and booted.
refs=("refs/tags/$default_tag" "refs/tags/$perf_tag")
g rev-parse -q --verify "refs/heads/$new_default_branch" >/dev/null && refs+=("refs/heads/$new_default_branch")
g rev-parse -q --verify "refs/heads/$new_perf_branch" >/dev/null && refs+=("refs/heads/$new_perf_branch")
g bundle create -q "$OUT/fork.bundle" "${refs[@]}" --not "refs/tags/$old_default" "refs/tags/$old_perf" \
    || die "could not write the fork bundle"

{
    echo "DEFAULT_TAG=$default_tag"
    echo "PERF_TAG=$perf_tag"
    echo "DEFAULT_BRANCH=$new_default_branch"
    echo "PERF_BRANCH=$new_perf_branch"
    echo "NEW_BASE=$new_base"
    # A list, quoted: the readers source this file.
    printf 'REUSED=%q\n' "${reused[*]}"
} > "$OUT/rebase.env"
echo "rebased onto $new_base: $default_tag, $perf_tag"
cat "$report"
