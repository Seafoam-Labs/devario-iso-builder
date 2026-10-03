import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("builder", Path(__file__).parents[1] / "builder.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = dict(schemaVersion=1, architecture="x86_64", epoch=1789488000,
            profile=str(self.root / "profile"), work=str(self.root / "work"),
            output=str(self.root / "output"), shelly="/usr/bin/shelly",
            shellyConfig=str(self.root / "shelly.conf"), keyring=str(self.root / "keys"),
            name="devario", version="test", label="DEVARIO_TEST", publisher="Devario Project",
            application="Devario Live", kernelId="linux-cachyos-lts", permissions={}, signing=None)

    def load(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.manifest))
        return b.load_manifest(path)

    def test_manifest(self):
        self.assertEqual(self.load(), self.manifest)
        self.manifest["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.load()

    def test_path_escape(self):
        self.manifest["work"] = "/tmp/../etc"
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.load()
        (self.root / "escape").symlink_to("/etc")
        with self.assertRaisesRegex(ValueError, "escapes"):
            b.in_root(self.root, "escape/passwd")

    def test_overlap(self):
        self.manifest["output"] = self.manifest["work"] + "/out"
        with self.assertRaisesRegex(ValueError, "separate"):
            self.load()

    def test_duplicate_keys(self):
        path = self.root / "bad.json"
        path.write_text('{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            b.load_manifest(path)

    def test_template(self):
        result = b.render("root=live:UUID=%DEVARIO_UUID% rd.live.dir=%INSTALL_DIR%/%ARCH%", self.manifest)
        self.assertIn("rd.live.dir=devario/x86_64", result)
        self.assertNotIn("%", result)
        with self.assertRaisesRegex(ValueError, "placeholder"):
            b.render("%UNKNOWN%", self.manifest)

    def test_missing_package(self):
        response = subprocess.CompletedProcess([], 0, '[{"Name":"bash","Version":"1"}]')
        with patch.object(b, "command", return_value=response):
            self.assertEqual(b.inventory("shelly", "config", ["bash"]), {"bash": "1"})
            with self.assertRaisesRegex(ValueError, "kernel"):
                b.inventory("shelly", "config", ["bash", "kernel"])

    def test_mount_cleanup_on_failure(self):
        calls = []
        def invoke(*args, **kwargs):
            calls.append(args)
        with patch.object(b, "command", side_effect=invoke):
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                with b.mounted_root(self.root):
                    raise RuntimeError("transaction failed")
        mounted = [args[-1] for args in calls if args[0] == "mount"]
        unmounted = [args[-1] for args in calls if args[0] == "umount"]
        self.assertEqual(mounted[::-1], unmounted)

    def test_isolated_paths(self):
        source = self.root / "source.conf"
        source.write_text('[options]\nRootDir = /\nDBPath = /host-db\n[devario-core]\nServer = file:///repo\n')
        result = self.root / "result.conf"
        b.isolated_config(source, self.root / "target", self.root / "keys", result)
        text = result.read_text()
        self.assertNotIn("/host-db", text)
        self.assertIn(f"DBPath = {self.root}/target/var/lib/shelly", text)
        self.assertIn("Server = file:///repo", text)

    def test_inventory_uses_explicit_configuration(self):
        response = subprocess.CompletedProcess([], 0, '[{"Name":"bash","Version":"1"}]')
        with patch.object(b, "command", return_value=response) as invoke:
            b.inventory("shelly", "/build/transaction.conf", ["bash"])
        self.assertEqual(invoke.call_args.args[:4], ("shelly", "--config", "/build/transaction.conf", "list"))

    def test_isolated_hooks_replace_defaults_in_override_order(self):
        source = self.root / "source.conf"
        source.write_text("[options]\nHookDirMode = Append\nHookDir = /host/hooks\n")
        output = self.root / "transaction.conf"
        b.isolated_config(source, self.root / "target", self.root / "keys", output)
        text = output.read_text()
        self.assertNotIn("/host/hooks", text)
        self.assertNotIn("HookDirMode = Append", text)
        self.assertEqual(text.count("HookDirMode = Replace"), 1)
        self.assertLess(text.index("usr/share/rlpm/hooks"), text.index("etc/shelly.d/hooks"))

    def test_inventory_rejects_duplicate_records(self):
        response = subprocess.CompletedProcess([], 0,
            '[{"Name":"bash","Version":"1"},{"Name":"bash","Version":"2"}]')
        with patch.object(b, "command", return_value=response):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                b.inventory("shelly", "config", ["bash"])

    def test_boot_references(self):
        config = self.root / "boot/syslinux/devario.cfg"
        b.write(config, "INCLUDE missing.cfg\n")
        with self.assertRaisesRegex(ValueError, "missing boot reference"):
            b.validate_boot_tree(self.root)
        b.write(config.parent / "missing.cfg", "")
        b.validate_boot_tree(self.root)


if __name__ == "__main__":
    unittest.main()
