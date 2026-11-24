#!/usr/bin/env bash
# boot-test-kernel.sh <kernel.pkg.tar.zst>: boot a built kernel package headless in QEMU, load the modules
# that matter, and fail on a module that does not load, an oops, BUG, panic or warning splat, or no
# success line within the timeout. The initramfs is made from the package itself plus this machine's
# bash, kmod and util-linux tools.
#
# Env: BOOT_TEST_LOG (serial log, default next to the package), BOOT_TEST_TIMEOUT (seconds; default 300
#      with KVM and 1500 without), BOOT_TEST_MODULES (space-separated, replaces the default list),
#      BOOT_TEST_ACCEL (kvm or tcg; default kvm when /dev/kvm opens), BOOT_TEST_PRE (shell run in the
#      guest before the checks, to test this script).

set -euo pipefail

MODULES=(amdgpu i915 xe nouveau nvme ahci btrfs zram v4l2loopback vhba
         virtio_pci virtio_blk virtio_net virtio_scsi virtio_gpu)
[ -z "${BOOT_TEST_MODULES:-}" ] || read -ra MODULES <<<"$BOOT_TEST_MODULES"
TOOLS=(bash cat dmesg grep mount sleep uname kmod)
MARK=MAINSTREAM-BOOT-TEST

die() { echo "boot-test: $*" >&2; exit 1; }
[ $# -eq 1 ] && [ -f "$1" ] || die "usage: $0 <kernel.pkg.tar.zst>"
pkg="$(realpath "$1")"
log="${BOOT_TEST_LOG:-${pkg%.pkg.tar.zst}.boot.log}"
for t in qemu-system-x86_64 cpio depmod zstd ldd timeout; do
    command -v "$t" >/dev/null || die "$t is not installed"
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -I zstd -xf "$pkg" -C "$work" usr/lib/modules
# kmod looks under <base>/lib/modules or <base>/usr/lib/modules, depending on how it was built.
ln -s usr/lib "$work/lib"
mapfile -t releases < <(find "$work/usr/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
[ ${#releases[@]} -eq 1 ] || die "the package does not hold exactly one kernel: ${releases[*]}"
release="${releases[0]}"
moddir="$work/usr/lib/modules/$release"
[ -f "$moddir/vmlinuz" ] || die "the package has no vmlinuz"

# The initramfs: merged /usr layout, the listed modules with their dependencies, the tools and their libraries.
root="$work/root"
mkdir -p "$root"/{usr/bin,usr/lib/modules/"$release",dev,proc,sys,run,tmp}
ln -s usr/bin "$root/bin"; ln -s usr/bin "$root/sbin"; ln -s usr/lib "$root/lib"; ln -s usr/lib "$root/lib64"
ln -s bin "$root/usr/sbin"; ln -s lib "$root/usr/lib64"
cp "$moddir"/modules.{builtin,builtin.modinfo,order} "$root/usr/lib/modules/$release/"
depmod -b "$work" "$release" || die "depmod failed on the package's modules"

missing=()
for m in "${MODULES[@]}"; do
    if ! deps="$(modprobe -d "$work" -S "$release" --show-depends "$m" 2>/dev/null)"; then
        missing+=("$m"); continue
    fi
    while read -r kind path _; do
        [ "$kind" = insmod ] || continue
        rel="${path#"$work"/}"
        mkdir -p "$root/$(dirname "$rel")"
        cp -n "$path" "$root/$rel"
    done <<<"$deps"
done
[ ${#missing[@]} -eq 0 ] || die "the package has no module ${missing[*]}"
depmod -b "$root" "$release" || die "depmod failed on the initramfs modules"

copy_with_libs() {
    local file="$1" lib
    [ -e "$root$file" ] || { mkdir -p "$root$(dirname "$file")"; cp -L "$file" "$root$file"; }
    while read -r lib; do
        [ -e "$root$lib" ] || { mkdir -p "$root$(dirname "$lib")"; cp -L "$lib" "$root$lib"; }
    done < <(ldd "$file" 2>/dev/null | grep -oE '/[^ ]+' || true)
}
for t in "${TOOLS[@]}"; do
    src="$(command -v "$t")" || die "$t is not installed"
    copy_with_libs "$(realpath "$src")"
    [ -e "$root/usr/bin/$t" ] || ln -s "$(realpath "$src")" "$root/usr/bin/$t"
done
for t in modprobe insmod lsmod; do ln -sf kmod "$root/usr/bin/$t"; done

cat > "$root/init" <<INITEOF
#!/usr/bin/bash
export PATH=/usr/bin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
echo 1 > /proc/sys/kernel/sysrq
result=PASSED
for m in ${MODULES[*]}; do
    if modprobe "\$m"; then echo "$MARK: loaded \$m"; else echo "$MARK: could not load \$m"; result=FAILED; fi
done
${BOOT_TEST_PRE:-}
sleep 3
if dmesg | grep -E 'Oops|BUG:|Kernel panic|WARNING:|cut here|Call Trace:|general protection fault|UBSAN:|KASAN:|refcount_t:|list_(add|del) corruption|soft lockup|hung_task|Unable to handle'; then
    echo "$MARK: the kernel log has the splat above"; result=FAILED
fi
taint=\$(cat /proc/sys/kernel/tainted)
# B bad page, D died, W warning, L soft lockup
if (( taint & (32 | 128 | 512 | 16384) )); then echo "$MARK: tainted \$taint"; result=FAILED; fi
echo "$MARK \$result \$(uname -r) taint=\$taint"
sleep 1
echo o > /proc/sysrq-trigger
sleep 30
INITEOF
chmod 755 "$root/init"
(cd "$root" && find . -mindepth 1 -print0 | LC_ALL=C sort -z | cpio --quiet -o -H newc -0 -R 0:0) > "$work/initramfs.cpio"

if [ "${BOOT_TEST_ACCEL:-}" != tcg ] && { [ "${BOOT_TEST_ACCEL:-}" = kvm ] || (exec 3<>/dev/kvm) 2>/dev/null; }; then
    accel=(-accel kvm -cpu host); limit="${BOOT_TEST_TIMEOUT:-300}"
else
    # The performance kernels need x86-64-v3; QEMU's "max" CPU has AVX2 under TCG.
    accel=(-accel tcg -cpu max); limit="${BOOT_TEST_TIMEOUT:-1500}"
fi
echo "boot-test: $release with ${accel[1]}, ${limit}s, modules: ${MODULES[*]}"
rc=0
timeout --foreground -k 10 "$limit" qemu-system-x86_64 "${accel[@]}" -machine q35 -smp 2 -m 3072 \
    -nodefaults -no-user-config -display none -no-reboot -serial "file:$log" \
    -kernel "$moddir/vmlinuz" -initrd "$work/initramfs.cpio" \
    -append "console=ttyS0 panic=-1 loglevel=4 rdinit=/init" || rc=$?
grep -a "^$MARK" "$log" || true
if grep -aqx "$MARK PASSED $release taint=[0-9]*" <(tr -d '\r' < "$log"); then
    echo "boot-test: $release booted and passed"
    exit 0
fi
[ "$rc" -ne 124 ] || echo "boot-test: no result within ${limit}s" >&2
echo "boot-test: $release failed; last lines of the serial log:" >&2
tr -d '\r' < "$log" | tail -n 60 >&2
exit 1
