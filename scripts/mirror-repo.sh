#!/usr/bin/env bash
# Upload packages immutably, verify them, then publish mutable databases last.
# Keep superseded mirror packages for cached databases; never rsync --delete.
set -euo pipefail

out="${1:-out}"
destination="${SF_DESTINATION:-mainstreamos@frs.sourceforge.net:/home/frs/project/mainstreamos/mainstream-repo/}"
transport="${SF_TRANSPORT:-ssh -i /tmp/sf/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/sf/known_hosts}"
list=$(mktemp)
trap 'rm -f "$list"' EXIT

shopt -s nullglob
packages=("$out"/*.pkg.tar.zst "$out"/*.pkg.tar.zst.sig)
[ "${#packages[@]}" -gt 0 ] || { echo "no packages to mirror" >&2; exit 1; }
for path in "${packages[@]}"; do basename "$path"; done > "$list"

# --existing distinguishes a conflicting existing file from an absent file.
# Checksum mode reads bytes on both hosts rather than trusting size/mtime.
changes=$(rsync -rcn --existing --itemize-changes --out-format='%i %n' \
    -e "$transport" --files-from="$list" "$out/" "$destination")
if [ -n "$changes" ]; then
    printf 'Refusing to overwrite published mirror packages/signatures:\n%s\n' "$changes" >&2
    exit 1
fi
rsync -rt --ignore-existing -e "$transport" --files-from="$list" "$out/" "$destination"
changes=$(rsync -rcn --itemize-changes --out-format='%i %n' \
    -e "$transport" --files-from="$list" "$out/" "$destination")
if [ -n "$changes" ]; then
    printf 'Mirror packages failed verification; database left unchanged:\n%s\n' "$changes" >&2
    exit 1
fi

# Separate invocations guarantee the database goes out after every payload.
# Rsync's default temp-file-and-rename keeps a partial upload out of live URLs.
for name in mainstream.files.sig mainstream.files mainstream.db.sig mainstream.db retained-packages.json; do
    [ -f "$out/$name" ] || { echo "missing mirror metadata: $name" >&2; exit 1; }
done
for name in mainstream.files.sig mainstream.files mainstream.db.sig mainstream.db retained-packages.json; do
    rsync -ct -e "$transport" "$out/$name" "$destination"
done
