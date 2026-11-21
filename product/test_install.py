"""Exercise product installation with isolated, fully manifested two-version test kits."""

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import install
import verify


SOURCE = Path(__file__).resolve().parents[1]
DEPLOY = install.load_at(SOURCE / "deploy/deploy.py")
NAMES = install.load_at(SOURCE / "src/runtime/device_names.py")


class KitFixture:
    def kit(self, version, variant=""):
        package = self.base / ("kit-" + version + variant)
        source = self.base / ("src-" + version + variant)
        identity = {"product": "xdev", "series": "windows-pair", "version": version}
        for name in verify.runtime_filenames():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        (source / "src/product.json").write_bytes(DEPLOY.canonical_json(identity))
        shutil.copyfile(SOURCE / "src/runtime/device_names.py", source / "src/runtime/device_names.py")
        (source / "src/cli/main.py").write_text(
            "import json, os\nfrom pathlib import Path\n"
            "cfg = json.loads((Path(os.environ['XDEV_HOME'])/'config.json').read_text(encoding='utf-8-sig'))\n"
            f"print(json.dumps({{'version': {version!r}, 'device': cfg['device'], 'variant': {variant!r}}}))\n",
            encoding="utf-8")
        filenames = verify.package_filenames(version)
        for name in filenames:
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
        for name in ("install.py", "migrate.py", "verify.py"):
            shutil.copyfile(SOURCE / "product" / name, package / name)
        for name in ("deploy.py", "optional.py"):
            shutil.copyfile(SOURCE / "deploy" / name, package / "deploy" / name)
        runtime = DEPLOY.build(source, package / "runtime.zip")["release"]
        dependencies = DEPLOY.digest(package / verify.DEPENDENCIES)
        (package / "release.json").write_bytes(DEPLOY.canonical_json(
            {**identity, "schema": 1, "runtime_sha256": runtime, "optional_deps_sha256": dependencies}))
        members = {name: {"size": (package / name).stat().st_size, "sha256": DEPLOY.digest(package / name)}
                   for name in filenames}
        (package / "package-manifest.json").write_bytes(DEPLOY.canonical_json({"schema": 1, "files": members}))
        return package

class ProductInstallTests(KitFixture, unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="xdev-product-fixture-")))
        self.root = self.base / "安装 space"

    def run_cli(self, package, *args, ok=True):
        result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(package / "install.py"),
                                 "--root", str(self.root), *args], capture_output=True, encoding="utf-8", timeout=30)
        data = json.loads(result.stdout)
        self.assertEqual(data["ok"], ok, data)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        return data

    def test_standard_dsh_upgrade_and_rollback_reuse_deps_with_node_companion(self):
        first, second = self.kit("1.0.0"), self.kit("1.0.1")
        self.root = self.base / "CrossDeviceCollab"
        one = self.run_cli(first, "install", "--cli-only")
        real_load = install.load_at
        optional = real_load(SOURCE / "deploy/optional.py")
        clients = real_load(SOURCE / "deploy/clients.py")
        site = self.root / "deps"
        (site / "mcp").mkdir(parents=True)
        (site / "mcp/__init__.py").write_text("verified package content")
        checksum = optional.digest(second / verify.DEPENDENCIES)
        receipt = {"schema": 1, "archive_sha256": checksum, "lock_sha256": optional.PINNED[optional.LOCK],
                   "packages": optional.PACKAGES, "files": optional.inventory(site)}
        (site / optional.RECEIPT).write_text(json.dumps(receipt))
        companion = site / "codebuddy-cli/node_modules"
        companion.mkdir(parents=True)
        marker = companion / "outside-python.js"
        marker.write_bytes(b"leave the other app alone")
        profile = self.base / "cordis.patch.yml"
        profile.write_bytes(b"old")

        def plan(root, current, selected, *, explicit, rollback):
            self.assertEqual(root, self.root)
            self.assertNotEqual(current, selected)
            self.assertEqual(explicit, not rollback)
            before, after = (b"new", b"old") if rollback else (b"old", b"new")
            return [(profile, before, after)], {"profile_configured": True}

        def active(args):
            return install.service(second, install.parser().parse_args(args))

        planning = Mock(side_effect=plan)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}), patch.dict(
                sys.modules, {"dsh_roots": SimpleNamespace(profile_transition=planning, clients=clients)}), patch.object(
                install, "load_at", side_effect=lambda path: optional if Path(path).name == "optional.py" else real_load(path)), patch.object(
                optional, "verify_imports") as imports:
            upgraded = active(["upgrade", "--expect-current", one["active"], "--with-dsh"])
            self.assertEqual(upgraded["mcp_dependencies"]["preserved_external_directories"], ["codebuddy-cli"])
            self.assertEqual(profile.read_bytes(), b"new")
            self.assertEqual(marker.read_bytes(), b"leave the other app alone")
            rolled = active(["rollback", "--expect-current", upgraded["active"], "--release", one["active"]])
            self.assertEqual(rolled["active"], one["active"])
            self.assertEqual(profile.read_bytes(), b"old")
            (site / "unreviewed.pth").touch()
            with self.assertRaisesRegex(ValueError, "Unrecorded"):
                active(["upgrade", "--expect-current", one["active"], "--with-dsh"])
            self.assertEqual((self.root / "current.txt").read_text().splitlines()[0], one["active"])
            self.assertEqual(profile.read_bytes(), b"old")
            self.assertEqual(marker.read_bytes(), b"leave the other app alone")
            self.assertEqual(planning.call_count, 2)
            imports.assert_called_once_with(site)

    def test_install_upgrade_rollback_preserve_device_state_and_launcher(self):
        first = self.kit("1.0.0")
        second = self.kit("1.0.1")
        one = self.run_cli(first, "install", "--device", "desktop", "--peer", "邻机",
                           "--peer-ssh", "neighbor", "--peer-alias", "laptop", "--cli-only")
        self.assertEqual(one["device"], NAMES.machine_name())
        self.assertEqual(one["aliases"], ["desktop"])
        self.assertEqual(one["peers"]["邻机"]["ssh"], "neighbor")
        self.assertTrue(one["active_verified"])
        self.assertEqual(one["active_product"]["version"], "1.0.0")
        config = self.root / "config.json"
        current_config = json.loads(config.read_bytes())
        config.write_bytes(json.dumps({**current_config, "extra": {"untouched": True}}, indent=4).encode() + b"\r\n")
        expected_config = config.read_bytes()
        marker = self.root / "state" / "existing-task"
        marker.write_bytes(b"preserve existing state")
        two = self.run_cli(second, "upgrade", "--cli-only", "--expect-current", one["active"])
        self.assertEqual(two["previous_product"]["version"], "1.0.0")
        replay = self.run_cli(second, "upgrade", "--cli-only")
        self.assertEqual((replay["active"], replay["previous"]), (two["active"], one["active"]))
        self.run_cli(second, "rollback", "--expect-current", one["active"], ok=False)
        back = self.run_cli(second, "rollback", "--version", "1.0.0", "--expect-current", two["active"])
        self.assertEqual(back["active_product"], one["active_product"])
        result = json.loads(subprocess.check_output(["cmd.exe", "/d", "/c", str(self.root / "xdev.cmd"), "who"],
                                                     cwd=self.base, encoding="utf-8", timeout=10))
        self.assertEqual((result["version"], result["device"]), ("1.0.0", NAMES.machine_name()))
        self.assertEqual(config.read_bytes(), expected_config)
        self.assertEqual(marker.read_bytes(), b"preserve existing state")

    def test_name_conflict_catalog_immutability_and_legacy_adoption(self):
        first = self.kit("1.0.0")
        alternate = self.kit("1.0.0", "rebuilt")
        runtime = DEPLOY.digest(first / "runtime.zip")
        DEPLOY.stage(self.root, first / "runtime.zip", runtime)
        DEPLOY.change_pointer(self.root, runtime)
        (self.root / "config.json").write_bytes(b'{"device":"laptop", "existing":17}')
        known = self.run_cli(first, "upgrade", "--name", "写作站", "--peer", "Peer-PC", "--cli-only")
        self.assertEqual(known["active_product"]["runtime_sha256"], runtime)
        saved = json.loads((self.root / "config.json").read_bytes())
        self.assertEqual((saved["existing"], saved["device"], saved["aliases"]), (17, "写作站", ["laptop"]))
        self.assertEqual(saved["identity_key"], "laptop")
        self.assertEqual(saved["peers"], {"Peer-PC": {"ssh": "desktop", "aliases": ["desktop"], "identity_key": "desktop"}})
        self.run_cli(first, "install", "--alias", "pEER-pc", "--cli-only", ok=False)
        self.run_cli(first, "install", "--peer-ssh", "test", "--cli-only", ok=False)
        self.run_cli(first, "install", "--peer", "other-pc", "--peer-ssh=-oBad", "--cli-only", ok=False)
        self.run_cli(alternate, "upgrade", "--cli-only", ok=False)
        unchanged = self.run_cli(first, "status")
        self.assertEqual(unchanged["active"], runtime)
        self.assertEqual(unchanged["releases"], [runtime])

    def test_two_upgrades_from_one_expected_release_cannot_both_win(self):
        first, left, right = [self.kit(version) for version in ("1.0.0", "1.0.1", "1.0.2")]
        active = self.run_cli(first, "install", "--device", "laptop", "--cli-only")["active"]
        def upgrade(package):
            args = [sys.executable, "-I", "-B", str(package / "install.py"), "--root", str(self.root),
                    "upgrade", "--cli-only", "--expect-current", active]
            return json.loads(subprocess.run(args, capture_output=True, timeout=30).stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(upgrade, (left, right)))
        self.assertEqual(sum(result["ok"] for result in results), 1)
        final = self.run_cli(first, "status")
        self.assertTrue(final["active_verified"])
        self.assertEqual(final["previous"], active)
        self.assertEqual(final["device"], NAMES.machine_name())

    def test_default_machine_name_rename_and_distinct_custom_bindings(self):
        kit = self.kit("1.0.0")
        first = self.run_cli(kit, "install", "--cli-only")
        self.assertEqual(first["device"], NAMES.machine_name())
        self.assertEqual(first["peers"], {})
        named = self.run_cli(kit, "install", "--name", "研究站-A", "--alias", "作者机", "--cli-only")
        self.assertEqual(named["device"], "研究站-A")
        self.assertEqual(named["machine_name"], NAMES.machine_name())
        self.assertEqual(named["identity_key"], first["device"].casefold())
        self.assertIn("作者机", named["aliases"])
        config = self.root / "config.json"
        saved = json.loads(config.read_bytes())
        saved["machine_name"] = "Other-Machine-Fixture"
        config.write_bytes(DEPLOY.canonical_json(saved))
        self.run_cli(kit, "upgrade", "--cli-only", ok=False)

    def test_named_peer_and_local_identity_survive_rename_and_ordinary_aliases(self):
        kit = self.kit("1.0.0")
        first = self.run_cli(kit, "install", "--name", "甲站", "--peer", "Peer-A", "--cli-only")
        updated = self.run_cli(kit, "install", "--name", "甲新站", "--alias", "laptop", "--peer", "Peer-B",
                               "--peer-identity-key", "peer-a", "--peer-alias", "Peer-A", "--peer-alias", "desktop",
                               "--cli-only")
        self.assertEqual(first["identity_key"], updated["identity_key"])
        self.assertEqual(list(updated["peers"]), ["Peer-B"])
        self.assertEqual(updated["peers"]["Peer-B"]["identity_key"], "peer-a")
        self.assertEqual(updated["peers"]["Peer-B"]["ssh"], "Peer-A")

    def test_legacy_rollback_restores_role_and_reactivation_restores_modern_address(self):
        kit = self.kit("1.0.0")
        legacy_src = self.base / "old-code"
        entry = legacy_src / "src/cli/main.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("import json, os\nfrom pathlib import Path\n"
                         "role = json.loads((Path(os.environ['XDEV_HOME'])/'config.json').read_bytes())['device']\n"
                         "assert role in ('desktop','laptop','local'), 'Invalid configured device'\n"
                         "print(json.dumps({'device':role}))\n", encoding="utf-8")
        archive = self.base / "legacy.zip"
        legacy = DEPLOY.build(legacy_src, archive)["release"]
        DEPLOY.stage(self.root, archive, legacy)
        DEPLOY.change_pointer(self.root, legacy)
        path = self.root / "config.json"
        path.write_bytes(DEPLOY.canonical_json({"device": "laptop", "custom": 7}))
        modern = self.run_cli(kit, "upgrade", "--name", "新主机", "--peer", "新对端", "--cli-only")
        snapshot = json.loads(path.read_bytes())
        old = self.run_cli(kit, "rollback", "--release", legacy)
        self.assertEqual(old["active"], legacy)
        data = json.loads(subprocess.check_output(["cmd.exe", "/d", "/c", str(self.root / "xdev.cmd"), "who"],
                                                  cwd=self.base, timeout=10))
        self.assertEqual(data["device"], "laptop")
        self.assertEqual(json.loads(path.read_bytes()), {"custom": 7, "device": "laptop"})
        self.assertEqual(self.run_cli(kit, "rollback")["active"], modern["active"])
        self.assertEqual(json.loads(path.read_bytes()), snapshot)
        self.run_cli(kit, "rollback", "--release", legacy)
        self.assertEqual(self.run_cli(kit, "upgrade", "--cli-only")["device"], "新主机")
        self.assertEqual(json.loads(path.read_bytes()), snapshot)

    def test_legacy_without_config_requires_old_role_when_group_history_exists(self):
        kit = self.kit("1.0.0")
        src = self.base / "old"
        entry = src / "src/cli/main.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("print('old')", encoding="utf-8")
        archive = self.base / "old.zip"
        sha = DEPLOY.build(src, archive)["release"]
        DEPLOY.stage(self.root, archive, sha)
        DEPLOY.change_pointer(self.root, sha)
        (self.root / "state/discussion-sends.sqlite3").touch()
        self.run_cli(kit, "upgrade", "--cli-only", ok=False)
        new = self.run_cli(kit, "upgrade", "--device", "desktop", "--peer", "Peer-Win", "--cli-only")
        self.assertEqual(new["identity_key"], "desktop")
        self.assertEqual(new["peers"]["Peer-Win"]["identity_key"], "laptop")
        self.assertEqual(self.run_cli(kit, "rollback")["active"], sha)
        self.assertNotIn("device", json.loads((self.root / "config.json").read_bytes()))
        again = self.run_cli(kit, "rollback", "--version", "1.0.0")
        self.assertEqual((again["device"], again["identity_key"], again["peers"]),
                         (new["device"], new["identity_key"], new["peers"]))

    def test_legacy_clients_roll_through_both_address_formats_without_losing_the_pair(self):
        oldkit, kit = self.kit("1.0.0"), self.kit("1.0.1")
        self.run_cli(kit, "install", "--device", "laptop", "--keep-legacy-clients", "--cli-only", ok=False)
        self.assertFalse(self.root.exists())
        source = self.base / "old-role-release"
        main = source / "src/cli/main.py"
        main.parent.mkdir(parents=True)
        main.write_text("print('historical reader')", encoding="utf-8")
        archive = self.base / "legacy.zip"
        legacy = DEPLOY.build(source, archive)["release"]
        DEPLOY.stage(self.root, archive, legacy)
        DEPLOY.change_pointer(self.root, legacy)
        cfg = self.root / "config.json"
        cfg.write_bytes(b'{"existing": 7}')
        # The real Windows pair previously derived the role from its hostname.
        direct = self.run_cli(kit, "upgrade", "--device", "laptop", "--name", "出版室", "--peer", "测量站",
                              "--keep-legacy-clients", "--cli-only")
        self.assertEqual((direct["device"], direct["legacy_clients"], direct["identity_key"]), ("出版室", True, "laptop"))
        saved = json.loads(cfg.read_bytes())
        self.assertEqual((saved["device"], saved["primary_name"], saved["existing"]), ("laptop", "出版室", 7))
        self.assertEqual(NAMES.DeviceDirectory(saved).mail_device("测量站"), "desktop")
        self.assertTrue(self.run_cli(kit, "upgrade", "--cli-only")["legacy_clients"])
        self.run_cli(kit, "rollback", "--release", legacy)
        self.assertEqual(json.loads(cfg.read_bytes()), {"existing": 7})
        self.run_cli(kit, "rollback", "--version", "1.0.1")
        self.assertEqual(json.loads(cfg.read_bytes()), saved)
        former = DEPLOY.digest(oldkit / "runtime.zip")
        DEPLOY.stage(self.root, oldkit / "runtime.zip", former)
        report = self.run_cli(kit, "rollback", "--release", former)
        lightweight = json.loads(cfg.read_bytes())
        self.assertEqual((report["device"], lightweight["device"]), ("laptop", "laptop"))
        self.assertNotIn("legacy_clients", lightweight)
        self.assertNotIn("primary_name", lightweight)
        self.assertEqual(NAMES.DeviceDirectory(lightweight).resolve("出版室"), "laptop")
        self.assertEqual(NAMES.DeviceDirectory(lightweight).resolve("测量站"), "desktop")
        self.run_cli(kit, "rollback", "--release", legacy)
        self.run_cli(kit, "rollback", "--version", "1.0.1")
        self.assertEqual(json.loads(cfg.read_bytes()), saved)
        self.run_cli(kit, "rollback", "--release", former)
        self.run_cli(kit, "upgrade", "--cli-only")
        self.assertEqual(json.loads(cfg.read_bytes()), saved)
        normal = self.run_cli(kit, "install", "--no-keep-legacy-clients", "--cli-only")
        self.assertEqual((normal["device"], normal["legacy_clients"], normal["identity_key"]), ("出版室", False, "laptop"))
        self.assertEqual(json.loads(cfg.read_bytes())["device"], "出版室")

    def test_existing_generic_pair_cannot_turn_ordinary_aliases_into_legacy_business_identities(self):
        kit = self.kit("1.0.1")
        self.run_cli(kit, "install", "--name", "Studio", "--alias", "laptop", "--peer", "Lab",
                     "--peer-alias", "desktop", "--cli-only")
        self.run_cli(kit, "upgrade", "--keep-legacy-clients", "--cli-only", ok=False)
        self.assertFalse(self.run_cli(kit, "status")["legacy_clients"])

    def test_invalid_package_or_dependencies_do_not_activate(self):
        first = self.kit("1.0.0")
        self.run_cli(first, "install", "--device", "laptop", ok=False)  # Empty synthetic dependency archive.
        self.assertFalse((self.root / "current.txt").exists())
        self.assertFalse((self.root / "config.json").exists())
        self.run_cli(first, "install", "--device", "laptop", "--cli-only")
        second = self.kit("1.0.1")
        (second / "deploy/deploy.py").write_text("raise RuntimeError('unexpected execution')", encoding="utf-8")
        failure = self.run_cli(second, "upgrade", "--cli-only", ok=False)
        self.assertNotIn("unexpected execution", failure["error"])
        self.assertEqual(self.run_cli(first, "status")["active_product"]["version"], "1.0.0")

    def test_unrelated_root_is_untouched(self):
        first = self.kit("1.0.0")
        self.root.mkdir()
        (self.root / "unrelated.txt").write_bytes(b"alone")
        self.run_cli(first, "install", "--device", "desktop", "--cli-only", ok=False)
        self.assertEqual([p.name for p in self.root.iterdir()], ["unrelated.txt"])


if __name__ == "__main__":
    unittest.main()
