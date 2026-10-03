# Devario ISO builder

A distribution-policy-independent x86_64 live image assembler. The caller
supplies a composed profile, a verified package repository configuration, Shelly,
and an isolated public keyring. The builder never selects repository servers or
executes profile shell code. Boot generation belongs to the target's
`/usr/lib/devario/initramfs-update` command.

Run `python3 builder.py --manifest manifest.json --validate` to check the version
1 JSON contract, and omit `--validate` to build inside a privileged isolated
environment. The supported firmware pair is BIOS Syslinux and UEFI systemd-boot.
GRUB loopback and Syslinux network templates are rendered from the profile.
Unsigned network media cannot satisfy templates requiring CMS verification;
supply the optional signing certificate/key pair when deploying those modes.

The manifest's exact fields are documented by `FIELDS` and `load_manifest` in
`builder.py`. Inputs are canonical absolute paths; work and output trees are
separate. `permissions` maps target absolute paths to octal mode strings;
`signing` is null or an object with `certificate` and `key` paths outside the
profile. `schemaVersion` is 1, `architecture` is `x86_64`, and `epoch` is a
positive Unix timestamp. Metadata fields are `name`, `version`, `label`,
`publisher`, and `application`. Paths are `profile`, `work`, `output`, `shelly`,
`shellyConfig`, and `keyring`; `kernelId` selects Devario kernel metadata.

Host tools: Python 3.11+, util-linux, coreutils, squashfs-tools, dosfstools,
mtools, xorriso, OpenSSL, and the caller-provided Shelly binary. Target packages
must supply Syslinux, systemd-boot, Devario kernel metadata and dracut integration.
Run in a private mount namespace/container. Work directories must be fresh.
Failed builds preserve their work tree for diagnosis and remove partial ISOs.

The builder passes its generated transaction file through Shelly's `--config`
option for synchronization, installation, and inventory. Configuration, state,
cache, and logs use Shelly paths. `HookDirMode = Replace` selects only target
`/usr/share/rlpm/hooks` followed by `/etc/shelly.d/hooks`, so administrator masks
retain precedence. No host configuration mountpoint or hook masking is needed.
The private mount namespace remains necessary for target API filesystems.
Supply the executable from the `shelly-cli` package, not the UI package.

This assembler does not supply an operating-system bootstrap filesystem.
Running it on a provisioned native build host requires no bootstrap archive.
A caller using a fresh systemd-nspawn container must provision that container
separately before invoking the assembler.

The ISO is accompanied by SHA-256 and JSON inventory/provenance reports. Private
keys never enter the target filesystem. Source archives are pinned by the
consumer, with corresponding source retained through its release procedure.

Run `python3 -m unittest discover -s tests` for unprivileged contract tests.
Test an actual Shelly CLI against a disposable local repository and target:

```sh
unshare --user --map-root-user --mount --propagation private \
  python3 tests/rlpm_transaction_smoke.py /path/to/shelly
```

This needs `repo-add`, `zstd`, and user namespaces. It checks synchronization,
installation, target inventory, and preservation of the host configuration.
Full acceptance additionally requires a signed package closure, BIOS/UEFI live
boots and installation, copy-to-RAM, network/loopback, and installed update tests.

## Provenance

The hybrid BIOS/UEFI media layout and selected assembly operations are adapted
from Archiso v89's `archiso/mkarchiso`, GPL-3.0-or-later, copyright its respective
contributors. Original source:
https://gitlab.archlinux.org/archlinux/archiso/-/archive/v89/archiso-v89.tar.gz
(SHA-256 `d4e98484aaa4880888765cdc8ee9bcabed98320c3e6372b5d9699e32d1a33040`).
Devario's Shelly transaction verification and late-overlay semantics originate
in the Devario patches to that version. This project does not invoke Archiso.

License: GPL-3.0-or-later; see LICENSE.
