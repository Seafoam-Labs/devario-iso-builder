#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Devario image assembler. Distribution and repository policy are caller inputs."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

VERSION = "0.2.0"
FIELDS = {"schemaVersion", "architecture", "profile", "work", "output", "epoch",
          "name", "version", "label", "publisher", "application", "shelly",
          "shellyConfig", "keyring", "kernelId", "permissions", "signing"}
TOOLS = ("mount", "umount", "chroot", "cp", "mksquashfs", "mkfs.fat", "mcopy",
         "xorriso", "openssl", "find")


def fail(message):
    raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def load_manifest(path):
    data = json.loads(Path(path).read_text(), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != FIELDS:
        fail("manifest has missing or unknown fields")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        fail("unsupported manifest version")
    if data["architecture"] != "x86_64":
        fail("only x86_64 is supported")
    if type(data["epoch"]) is not int or not 1 <= data["epoch"] <= 4102444800:
        fail("invalid source epoch")
    for field in ("name", "version", "kernelId"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", data[field]):
            fail(f"invalid {field}")
    if not isinstance(data["label"], str) or not re.fullmatch(r"[A-Z0-9_]{1,32}", data["label"]):
        fail("invalid ISO label")
    for field in ("publisher", "application"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[ -~]{1,128}", data[field]):
            fail(f"invalid {field}")
    for field in ("profile", "work", "output", "shelly", "shellyConfig", "keyring"):
        value = data[field]
        if not isinstance(value, str) or not value.startswith("/") or re.search(r"[\s]", value):
            fail(f"{field} must be an absolute path without whitespace")
        if Path(value).resolve() != Path(value) or value == "/":
            fail(f"{field} must be canonical and not root")
    work, output, profile = (Path(data[k]) for k in ("work", "output", "profile"))
    if work == output or work in output.parents or output in work.parents:
        fail("work and output must be separate trees")
    if work == profile or work in profile.parents or profile in work.parents:
        fail("work and profile must be separate trees")
    permissions = data["permissions"]
    if not isinstance(permissions, dict):
        fail("permissions must be an object")
    for name, mode in permissions.items():
        if not name.startswith("/") or ".." in Path(name).parts or not re.fullmatch(r"[0-7]{3,4}", str(mode)):
            fail("invalid file permission")
    signing = data["signing"]
    if signing is not None:
        if not isinstance(signing, dict) or set(signing) != {"certificate", "key"}:
            fail("signing requires certificate and key")
        for value in signing.values():
            if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                fail("missing signing input")
            if profile == Path(value) or profile in Path(value).resolve().parents:
                fail("signing inputs must be outside the profile")
    return data


def command(*args, capture=False, env=None):
    return subprocess.run([str(a) for a in args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, env=env)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o644)


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def in_root(root, name):
    """Resolve a target path without following absolute links onto the host."""
    path = root / name.lstrip("/")
    if not path.resolve().is_relative_to(root):
        fail(f"target path escapes root: {name}")
    return path


def render(text, manifest):
    dt = datetime.datetime.fromtimestamp(manifest["epoch"], datetime.timezone.utc)
    values = {"DEVARIO_LABEL": manifest["label"], "DEVARIO_UUID": dt.strftime("%Y-%m-%d-%H-%M-%S-00"),
              "INSTALL_DIR": "devario", "ARCH": "x86_64"}
    for key, value in values.items():
        text = text.replace(f"%{key}%", value)
    if re.search(r"%[A-Z][A-Z0-9_]*%", text):
        fail("unresolved boot template placeholder")
    return text


def inventory(shelly, config, requested):
    # The caller mounts config at RLPM's configuration path for the complete
    # transaction, including this inventory query.
    result = command(shelly, "list", "standard", "--show-hidden", "--json", capture=True)
    data = json.loads(result.stdout)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or any(not isinstance(p, dict) or not p.get("Name") or not p.get("Version") for p in data):
        fail("invalid Shelly inventory")
    installed = {p["Name"]: p["Version"] for p in data}
    if len(installed) != len(data):
        fail("duplicate installed package")
    missing = sorted(set(requested) - installed.keys())
    if missing:
        fail("Shelly did not install requested package(s): " + ", ".join(missing))
    return installed


@contextlib.contextmanager
def shelly_configuration(config, work):
    """Mount RLPM's transaction config privately, without changing host bytes."""
    target = Path("/etc/pacman.conf")
    if not target.is_file() or target.is_symlink():
        fail("builder requires a regular /etc/pacman.conf as the RLPM config mountpoint")
    empty_hooks = work / "host-hooks-masked"
    empty_hooks.mkdir()
    mounted = []
    command("mount", "--bind", config, target)
    mounted.append(target)
    try:
        command("mount", "-o", "remount,bind,ro", target)
        # RLPM's configuration parser appends HookDir entries to its defaults.
        # Hide host defaults; the generated config still names target hooks.
        for directory in ("/usr/share/libalpm/hooks", "/etc/pacman.d/hooks"):
            host_hooks = Path(directory)
            if host_hooks.is_dir():
                command("mount", "--bind", empty_hooks, host_hooks)
                mounted.append(host_hooks)
                command("mount", "-o", "remount,bind,ro", host_hooks)
        yield
    finally:
        errors = []
        for path in reversed(mounted):
            try:
                command("umount", path)
            except subprocess.CalledProcessError:
                errors.append(str(path))
        if errors:
            fail("could not unmount private RLPM configuration: " + ", ".join(errors))


@contextlib.contextmanager
def mounted_root(root):
    mounts = []
    def mount(source, target, *options):
        destination = in_root(root, target)
        destination.mkdir(parents=True, exist_ok=True)
        command("mount", *options, source, destination)
        mounts.append(destination)
    try:
        mount("proc", "proc", "-t", "proc", "-o", "nosuid,noexec,nodev")
        mount("sys", "sys", "-t", "sysfs", "-o", "nosuid,noexec,nodev,ro")
        mount("udev", "dev", "-t", "devtmpfs", "-o", "mode=0755,nosuid")
        mount("devpts", "dev/pts", "-t", "devpts", "-o", "mode=0620,gid=5,nosuid,noexec")
        mount("shm", "dev/shm", "-t", "tmpfs", "-o", "mode=1777,nosuid,nodev")
        mount("run", "run", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev")
        mount("tmp", "tmp", "-t", "tmpfs", "-o", "mode=1777,nosuid,nodev")
        yield
    finally:
        errors = []
        for path in reversed(mounts):
            try:
                command("umount", path)
            except subprocess.CalledProcessError:
                errors.append(str(path))
        if errors:
            fail("could not unmount target paths; preserve work tree: " + ", ".join(errors))


def overlay(source, root):
    if not source.is_dir():
        return
    # Do not let a target symlink redirect a copied directory onto the host.
    for item in source.rglob("*"):
        in_root(root, str(item.relative_to(source)))
    command("cp", "-a", "--no-preserve=ownership", str(source) + "/.", root)


def isolated_config(source, root, keyring, destination):
    lines = []
    options = 0
    for line in Path(source).read_text().splitlines():
        if re.match(r"\s*(RootDir|DBPath|CacheDir|HookDir|LogFile|GPGDir)\s*=", line, re.I):
            continue
        lines.append(line)
        if line.strip().lower() == "[options]":
            options += 1
            lines.extend([f"RootDir = {root}", f"DBPath = {root}/var/lib/pacman",
                          f"CacheDir = {root}/var/cache/pacman/pkg", f"GPGDir = {keyring}",
                          f"LogFile = {root}/var/log/pacman.log",
                          f"HookDir = {root}/etc/pacman.d/hooks", f"HookDir = {root}/usr/share/libalpm/hooks"])
    if options != 1:
        fail("Shelly configuration must have exactly one options section")
    write(destination, "\n".join(lines) + "\n")


def identity(root, manifest):
    release = in_root(root, "etc/os-release")
    text = release.read_text()
    if not re.search(r'^ID=[\"\']?devario[\"\']?$', text, re.M) or re.search(r"^ID_LIKE=.*arch", text, re.M):
        fail("live root must identify as Devario")
    text = re.sub(r"^IMAGE_(ID|VERSION)=.*\n?", "", text, flags=re.M)
    text += f'IMAGE_ID={manifest["name"]}\nIMAGE_VERSION={manifest["version"]}\n'
    write(release, text)
    # Both locations agree; filesystem package owns the fallback location.
    write(in_root(root, "usr/lib/os-release"), text)
    if in_root(root, "etc/arch-release").exists():
        fail("filesystem package still supplies /etc/arch-release")


def boot_tree(root, profile, media, manifest):
    kernel = manifest["kernelId"]
    for name in (f"vmlinuz-{kernel}", f"initramfs-{kernel}.img"):
        source = in_root(root, "boot/" + name)
        if not source.is_file() or not source.stat().st_size:
            fail(f"missing generated boot file: {name}")
        copy(source, media / "devario/boot/x86_64" / name)
    for directory, target, pattern in (("syslinux", "boot/syslinux", "*.cfg"),
                                      ("efiboot/loader", "loader", "**/*.conf"),
                                      ("grub", "boot/grub", "*.cfg")):
        for source in (profile / directory).glob(pattern):
            write(media / target / source.relative_to(profile / directory), render(source.read_text(), manifest))
    source_dir = in_root(root, "usr/lib/syslinux/bios")
    for source in list(source_dir.glob("*.c32")) + [source_dir / p for p in ("isolinux.bin", "isohdpfx.bin", "lpxelinux.0", "memdisk")]:
        copy(source, media / "boot/syslinux" / source.name)
    for source in (profile / "syslinux").glob("*.png"):
        copy(source, media / "boot/syslinux" / source.name)
    for arch in ("x64", "ia32"):
        source = in_root(root, f"usr/lib/systemd/boot/efi/systemd-boot{arch}.efi")
        if arch == "x64" or source.exists():
            copy(source, media / f"EFI/BOOT/BOOT{arch.upper()}.EFI")
    validate_boot_tree(media)


def validate_boot_tree(media):
    for path in list((media / "boot/syslinux").glob("*.cfg")) + list((media / "loader/entries").glob("*.conf")):
        for line in path.read_text().splitlines():
            fields = line.split()
            if not fields:
                continue
            kind = fields[0].upper()
            if kind in ("INCLUDE", "CONFIG", "LINUX", "INITRD") and len(fields) > 1:
                value = fields[1].removeprefix("::")
                target = media / value.lstrip("/") if value.startswith("/") else path.parent / value
                if not target.is_file():
                    fail(f"missing boot reference: {path.name}: {value}")


def assemble(root, profile, media, work, output, manifest):
    boot_tree(root, profile, media, manifest)
    esp = work / "esp"
    esp.mkdir()
    for name in ("EFI", "loader", "devario/boot"):
        shutil.copytree(media / name, esp / name)
    esp_image = work / "efiboot.img"
    size = sum(p.stat().st_size for p in esp.rglob("*") if p.is_file())
    size = max(64 * 1024**2, ((size * 11 // 10 + 16 * 1024**2) // (1024**2) + 1) * 1024**2)
    with esp_image.open("wb") as stream:
        stream.truncate(size)
    command("mkfs.fat", "--invariant", "-F", "32", "-n", "DEVARIO_EFI", esp_image)
    command("mcopy", "-s", "-i", esp_image, *sorted(esp.iterdir()), "::/")
    # Boot assets must be staged before removing /boot from the installation root.
    for name in ("boot", "var/cache/pacman/pkg", "var/lib/pacman/sync", "var/log", "var/tmp", "tmp"):
        path = in_root(root, name)
        if path.exists():
            for item in path.iterdir():
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()
    write(in_root(root, "etc/machine-id"), "uninitialized\n")
    image = media / "devario/x86_64/airootfs.sfs"
    image.parent.mkdir(parents=True, exist_ok=True)
    squash_environment = dict(os.environ)
    # mksquashfs rejects combining SOURCE_DATE_EPOCH with explicit timestamps.
    squash_environment.pop("SOURCE_DATE_EPOCH", None)
    command("mksquashfs", root, image, "-noappend", "-comp", "xz", "-Xbcj", "x86",
            "-b", "1M", "-mkfs-time", manifest["epoch"], "-all-time", manifest["epoch"],
            env=squash_environment)
    with image.open("rb") as stream:
        sha512 = hashlib.file_digest(stream, "sha512").hexdigest()
    write(image.with_suffix(".sha512"), sha512 + "  airootfs.sfs\n")
    signing = manifest["signing"]
    if signing:
        command("openssl", "cms", "-sign", "-binary", "-noattr", "-in", image,
                "-signer", signing["certificate"], "-inkey", signing["key"],
                "-outform", "DER", "-out", str(image) + ".cms.sig")
    name = f'{manifest["name"]}-{manifest["version"]}-x86_64.iso'
    temporary = output / ("." + name + ".partial")
    target = output / name
    if target.exists() or temporary.exists():
        fail("refusing to overwrite an existing ISO")
    date = datetime.datetime.fromtimestamp(manifest["epoch"], datetime.timezone.utc).strftime("%Y%m%d%H%M%S00")
    try:
        command("xorriso", "-no_rc", "-as", "mkisofs", "-iso-level", "3",
                "-full-iso9660-filenames", "-joliet", "-joliet-long", "-rational-rock",
                "-volid", manifest["label"], "-appid", manifest["application"],
                "-publisher", manifest["publisher"], "-preparer", "prepared by devario-iso-builder",
                "--modification-date=" + date,
                "-eltorito-boot", "boot/syslinux/isolinux.bin", "-eltorito-catalog", "boot/syslinux/boot.cat",
                "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
                "-isohybrid-mbr", media / "boot/syslinux/isohdpfx.bin", "--mbr-force-bootable",
                "-partition_offset", "16", "-append_partition", "2", "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", esp_image,
                "-isohybrid-gpt-basdat", "-eltorito-alt-boot", "-e", "--interval:appended_partition_2:all::",
                "-no-emul-boot", "-output", temporary, media)
        if not temporary.stat().st_size:
            fail("empty ISO")
        metadata = command("xorriso", "-no_rc", "-indev", temporary, "-pvd_info", capture=True).stdout
        if "prepared by devario-iso-builder" not in metadata.lower():
            fail("ISO preparer metadata verification failed")
        return temporary, target
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build(manifest, manifest_path):
    if os.geteuid() != 0:
        fail("image construction requires root in an isolated build environment")
    for tool in TOOLS:
        if not shutil.which(tool):
            fail(f"missing host tool: {tool}")
    work, output, profile = (Path(manifest[k]) for k in ("work", "output", "profile"))
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        fail("work directory must be empty; cached stages are not supported")
    output.mkdir(parents=True, exist_ok=True)
    root, media = work / "root", work / "media"
    root.mkdir(); media.mkdir()
    requested = [line.strip() for line in (profile / "packages.x86_64").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    if not requested or any(not re.fullmatch(r"[A-Za-z0-9@._+][A-Za-z0-9@._+-]*", n) for n in requested):
        fail("invalid package list")
    overlay(profile / "airootfs", root)
    for name in ("var/lib/pacman", "var/cache/pacman/pkg", "var/log", "etc/pacman.d/hooks", "boot"):
        in_root(root, name).mkdir(parents=True, exist_ok=True)
    config = work / "shelly.conf"
    isolated_config(manifest["shellyConfig"], root, manifest["keyring"], config)
    with shelly_configuration(config, work), mounted_root(root):
        command(manifest["shelly"], "sync", "standard", "--no-confirm")
        command(manifest["shelly"], "install", "standard", "--no-confirm", *requested)
        installed = inventory(manifest["shelly"], config, requested)
    if re.search(r"^liveuser:", in_root(root, "etc/passwd").read_text(), re.M) or in_root(root, "home/liveuser").exists():
        fail("live account was materialized before the late overlay")
    # Restore only edition identity. Recopying passwd/shadow would erase users
    # created by package transactions. Other config uses package backup rules.
    copy(profile / "airootfs/etc/os-release", in_root(root, "etc/os-release"))
    overlay(profile / "late-airootfs", root)
    for name, mode in manifest["permissions"].items():
        in_root(root, name).chmod(int(mode, 8))
    identity(root, manifest)
    signing = manifest["signing"]
    if signing:
        copy(signing["certificate"], in_root(root, "etc/devario/live-signing.pem"))
    with mounted_root(root):
        command("chroot", root, "/usr/lib/devario/initramfs-update", "--mode", "live", "--kernel-id", manifest["kernelId"])
    write(media / "devario/pkglist.x86_64.txt", "".join(f"{k} {v}\n" for k, v in sorted(installed.items())))
    write(media / "devario/version", manifest["version"] + "\n")
    report = {"schemaVersion": 1, "builderVersion": VERSION, "builderSha256": digest(__file__),
              "manifestSha256": digest(manifest_path), "sourceDateEpoch": manifest["epoch"], "packages": installed}
    write(root / "usr/share/devario/iso-builder.json", json.dumps(report, sort_keys=True) + "\n")
    temporary, target = assemble(root, profile, media, work, output, manifest)
    try:
        report["isoSha256"] = digest(temporary)
        write(output / (target.name + ".json"), json.dumps(report, sort_keys=True, indent=2) + "\n")
        write(output / (target.name + ".sha256"), report["isoSha256"] + "  " + target.name + "\n")
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validate", action="store_true", help="validate contract without privileges or mutations")
    parser.add_argument("--private-mounts", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.validate:
        print("Devario ISO builder manifest is valid")
    else:
        if not args.private_mounts:
            command("unshare", "--mount", "--propagation", "private", sys.executable,
                    Path(__file__).resolve(), "--manifest", args.manifest.resolve(), "--private-mounts")
            return
        if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
            fail("image construction requires a private mount namespace")
        os.environ.pop("TMPDIR", None)
        os.environ["SOURCE_DATE_EPOCH"] = str(manifest["epoch"])
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        build(manifest, args.manifest)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f"devario-iso-builder: {error}", file=sys.stderr)
        sys.exit(1)
