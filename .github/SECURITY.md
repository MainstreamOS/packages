# Security Policy

## Supported Versions

The `[mainstream]` package repository is a rolling binary repository built for Mainstream OS on Arch Linux. Only the current published state of the repository and the latest `4.x` Mainstream desktop release series are supported.

| Component | Supported Version | Status |
| :--- | :--- | :--- |
| `[mainstream]` package repository (`MainstreamOS/packages`) | Rolling (`mainstream-repo`) | Supported |
| Mainstream Desktop & Shell (`MainstreamOS/dots-hyprland`) | `4.x` | Supported |
| Mainstream Desktop & Shell (`MainstreamOS/dots-hyprland`) | `< 4.0` | Unsupported |

## Scope: First-Party vs. Upstream Packages

- **Mainstream First-Party Packaging & Build Pipeline** (`MainstreamOS/packages`, `pkgbuilds/`, `build-repo.sh`, `scripts/repo_safety.py`, `MainstreamOS/*` forks, and `dots-hyprland`): Covered directly by this policy.
- **Upstream Arch Linux Packages** (`[core]`, `[extra]`, `[multilib]`): Inherited unmodified from official Arch Linux mirrors and tracked by the [Arch Linux Security Tracker](https://security.archlinux.org/).

## Reporting a Vulnerability

**Please do not report security vulnerabilities in public GitHub issues or public Discord channels.**

1. **GitHub Private Vulnerability Reporting (Preferred):**
   Open a private advisory using [**Report a vulnerability**](https://github.com/MainstreamOS/packages/security/advisories/new) under the **Security** tab of this repository (or on [`MainstreamOS/dots-hyprland`](https://github.com/MainstreamOS/dots-hyprland/security/advisories/new)).
2. **Email:**
   If you cannot use GitHub Security Advisories, email [mainstreamlinuxos@gmail.com](mailto:mainstreamlinuxos@gmail.com) with the subject line `[SECURITY]`.
3. **What to include:**
   - The affected package, script, or workflow and its version or commit SHA
   - Steps to reproduce or a proof-of-concept
   - What an attacker can achieve (for example: privilege escalation, signature bypass, or build-pipeline compromise)

### Response Timeline

- **Initial acknowledgment:** Within 72 hours
- **Triage & status update:** Within 7 days
- **Fix & disclosure:** Coordinated disclosure after a signed replacement package or fix has been published to the repository.

## Build & Package Integrity

- Every recipe built by `build-repo.sh` verifies upstream source checksums (`sha256sums` / `b2sums`) and upstream PGP signatures (`validpgpkeys`, pinned in `keys/upstream/`) before compilation.
- Every published package and repository database (`mainstream.db`) is signed with the Mainstream repository signing key (`D644 BEB9 C1B7 668E 3A6C 16DA 8D56 7345 B265 848E`, published at [`keys/mainstream.pub`](../keys/mainstream.pub) and `https://mainstreamos.org/mainstream.pub`), and clients enforce `SigLevel = Required`.
- Published package archives and their detached `.sig` signatures are immutable on both GitHub Releases and the SourceForge mirror (`scripts/repo_safety.py`).
