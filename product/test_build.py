import hashlib
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile


def load_builder():
    spec = importlib.util.spec_from_file_location("test_product_builder", Path(__file__).with_name("build.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_builder()
verify = builder.verify


class ProductPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.source = self.work / "source"
        self.output = self.work / "release"
        identity = {"product": "xdev", "version": "1.0.0", "series": "windows-pair"}
        for name in verify.runtime_filenames():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(json.dumps(identity).encode() if name == "src/product.json" else b"fixture\n")
        for name in verify.DEPLOY_FILES | verify.INTEGRATION_FILES | {verify.SKILL_FILE}:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture\n")
        for name in verify.PROJECT_NOTICES:
            (self.source / name).write_text(f"fixture {name}\n", encoding="utf-8")
        product = self.source / "product"
        product.mkdir()
        for name in verify.ROOT_FILES | verify.ENGLISH_MANUALS:
            (product / name).write_bytes(b"fixture\n")
        self.deps = self.work / "offline.zip"
        self.deps.write_bytes(b"fixture dependency archive")
        runtime = builder.deploy.build(self.source, self.work / "baseline.zip")["release"]
        self.release = {**identity, "schema": 1, "runtime_sha256": runtime,
                        "optional_deps_sha256": verify.file_digest(self.deps)}
        (product / "release.json").write_text(json.dumps(self.release), encoding="utf-8")
        # This suite exercises the product envelope with no network or wheels.
        # deploy/optional.py owns verification of published wheel bytes.
        self.enterContext(patch.object(builder.optional, "extract", return_value=None))

    def build(self, out=None):
        return builder.build(self.source, out or self.output, dependencies=self.deps)

    def select_version(self, version):
        self.release["version"] = version
        identity = {key: self.release[key] for key in ("product", "series", "version")}
        (self.source / "src/product.json").write_bytes(json.dumps(identity).encode())
        self.release["runtime_sha256"] = builder.deploy.build(self.source, self.work / (version + ".zip"))["release"]
        (self.source / "product/release.json").write_bytes(json.dumps(self.release).encode())

    def test_legacy_and_skill_layouts_are_strict_even_with_rehashed_manifests(self):
        for version in ("1.0.0", "1.0.1", "1.0.2"):
            with self.subTest(version=version):
                self.select_version(version)
                result = self.build()
                self.assertEqual(verify.verify_archive(result["archive"]), self.release)
                with zipfile.ZipFile(result["archive"]) as archive:
                    self.assertEqual(verify.SKILL_FILE in archive.namelist(), version == "1.0.2")
                    extracted = self.work / ("valid-" + version)
                    archive.extractall(extracted)
                    files = {name: archive.read(name) for name in archive.namelist()}
                self.assertEqual(verify.verify_directory(extracted), self.release)
                if version == "1.0.2":
                    del files[verify.SKILL_FILE]
                else:
                    files[verify.SKILL_FILE] = b"unexpected for earlier releases"
                del files[verify.MANIFEST]
                records = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                           for name, data in files.items()}
                files[verify.MANIFEST] = json.dumps({"schema": 1, "files": records}).encode()
                alternate = self.work / ("other-layout-" + version + ".zip")
                with zipfile.ZipFile(alternate, "w") as archive:
                    for name, data in files.items():
                        archive.writestr(name, data)
                with self.assertRaises(ValueError):
                    verify.verify_archive(alternate)
                with zipfile.ZipFile(alternate) as archive:
                    directory = alternate.with_suffix("")
                    archive.extractall(directory)
                with self.assertRaises(ValueError):
                    verify.verify_directory(directory)

    def test_skill_is_mandatory_in_source_and_checked_by_digest(self):
        self.select_version("1.0.2")
        result = self.build()
        with zipfile.ZipFile(result["archive"]) as archive:
            target = self.work / "skill-verification"
            archive.extractall(target)
        (target / verify.SKILL_FILE).write_bytes(b"changed\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch|record or length"):
            verify.verify_directory(target)
        (self.source / verify.SKILL_FILE).unlink()
        with self.assertRaisesRegex(ValueError, "Missing required product file"):
            self.build(self.work / "missing-input")

    def test_two_independent_builds_are_byte_identical_and_verify_after_extraction(self):
        a, b = self.build(), self.build(self.work / "another")
        self.assertEqual(Path(a["archive"]).read_bytes(), Path(b["archive"]).read_bytes())
        self.assertEqual(self.build()["sha256"], a["sha256"])
        self.assertEqual(verify.verify_archive(a["archive"], a["sha256"]), self.release)
        with zipfile.ZipFile(a["archive"]) as package:
            extracted = self.work / "extracted"
            package.extractall(extracted)
        self.assertEqual(verify.verify_directory(extracted), self.release)
        (extracted / "install.py").write_bytes(b"tamper!\n")
        with self.assertRaises(ValueError):
            verify.verify_directory(extracted)
        with self.assertRaises(ValueError):
            verify.verify_archive(a["archive"], "0" * 64)

    def test_source_allowlist_and_immutable_published_version(self):
        result = self.build()
        archive = Path(result["archive"])
        outside = self.source / "src" / ".env"
        outside.write_bytes(b"placeholder")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.build()
        outside.unlink()
        (self.source / "product/README.md").write_text("expanded instructions", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs|different"):
            self.build()
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"])

    def test_release_identity_must_match_runtime(self):
        metadata = self.source / "product/release.json"
        metadata.write_text(json.dumps({**self.release, "version": "1.0.1"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "product identity"):
            self.build()
        self.assertFalse(list(self.output.glob("*.zip")))

    def test_surface_runtime_is_required_only_from_its_release(self):
        preview = "1.1.0-preview.1"
        extras = verify.runtime_filenames(preview) - verify.runtime_filenames("1.0.3")
        self.assertEqual(len(extras), 4)
        self.select_version(preview)
        with self.assertRaisesRegex(ValueError, "Runtime inventory"):
            self.build()
        for name in extras:
            (self.source / name).write_bytes(b"fixture component\n")
        self.select_version(preview)
        built = self.build()
        self.assertEqual(verify.verify_archive(built["archive"]), self.release)
        self.select_version("1.0.3")
        with self.assertRaisesRegex(ValueError, "Runtime inventory"):
            self.build()

    def test_documentation_edition_keeps_original_and_executable_payloads(self):
        original = self.build()
        self.assertEqual(Path(original["archive"]).name, "xdev-windows-pair-1.0.0.zip")
        revised = {**self.release, "documentation_revision": 1}
        (self.source / "product/release.json").write_text(json.dumps(revised), encoding="utf-8")
        manual = self.source / "product/README.md"
        manual.write_text("first revision", encoding="utf-8")
        edition = self.build()
        archive = Path(edition["archive"])
        self.assertEqual(archive.name, "xdev-windows-pair-1.0.0-docs.1.zip")
        self.assertEqual(edition["documentation_revision"], 1)
        self.assertEqual(verify.verify_archive(archive, edition["sha256"]), revised)
        self.assertEqual(self.build(self.work / "rebuild")["sha256"], edition["sha256"])
        with zipfile.ZipFile(original["archive"]) as a, zipfile.ZipFile(archive) as b:
            changed = {n for n in a.namelist() if a.read(n) != b.read(n)}
            self.assertEqual(changed, {"README.md", "release.json", verify.MANIFEST})
        manual.write_text("unversioned change", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs|different"):
            self.build()
        for result in (original, edition):
            self.assertEqual(verify.file_digest(Path(result["archive"])), result["sha256"])

    def test_preview_three_adds_relay_runtime_guide_and_restricted_config(self):
        old, new = "1.1.0-preview.2", "1.1.0-preview.3"
        relay_files = {verify.RELAY_GUIDE, verify.RELAY_ACL}
        self.assertEqual(verify.package_filenames(new) - verify.package_filenames(old), relay_files)
        self.assertEqual(verify.runtime_filenames(new) - verify.runtime_filenames(old),
                         {"src/adapters/portable.py", "src/runtime/outbox.py", "src/runtime/session_relay.py"})
        self.assertEqual(verify.runtime_filenames("1.1.0-preview.12"), verify.runtime_filenames("1.1.0"))
        for name in verify.runtime_filenames(new) - verify.runtime_filenames():
            (self.source / name).write_bytes(b"fixture runtime\n")
        self.select_version(new)
        for name in relay_files:
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"public example\n")
        manual = self.source / "product/README.md"
        manual.write_text("[Relay guide](../design/session-relay.md)", encoding="utf-8")
        result = self.build()
        self.assertEqual(verify.verify_archive(result["archive"], result["sha256"]), self.release)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertEqual(archive.read("README.md"), b"[Relay guide](design/session-relay.md)")
            self.assertTrue(relay_files.issubset(archive.namelist()))
        (self.source / verify.RELAY_GUIDE).unlink()
        with self.assertRaisesRegex(ValueError, "Missing required product file"):
            self.build()

    def test_invalid_documentation_revision_is_rejected(self):
        metadata = self.source / "product/release.json"
        for value in (None, 0, -1, False, True, 1.0, "1"):
            with self.subTest(value=value):
                metadata.write_text(json.dumps({**self.release, "documentation_revision": value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "documentation_revision"):
                    self.build()
        self.assertFalse(list(self.output.glob("*.zip")))

    def test_active_subscription_signal_enters_preview_ten(self):
        old, new = "1.1.0-preview.9", "1.1.0-preview.10"
        self.assertEqual(verify.runtime_filenames(new) - verify.runtime_filenames(old), {"src/runtime/github_demand.py"})

    def test_host_arrival_protocol_enters_preview_eleven(self):
        added = verify.runtime_filenames("1.1.0-preview.11") - verify.runtime_filenames("1.1.0-preview.10")
        self.assertEqual(added, {"src/adapters/mail_arrival.py", "src/adapters/mail_arrival_stdio.py"})

    def test_unified_delivery_status_enters_windows_pair_1_1_1(self):
        previous = verify.runtime_filenames("1.1.0")
        self.assertEqual(verify.runtime_filenames("1.1.1"), previous | {"src/runtime/delivery_status.py"})
        self.assertNotIn("src/runtime/delivery_status.py", previous)

    def test_stable_client_guides_and_offline_links_preserve_earlier_package_layouts(self):
        preview = "1.1.0-preview.13"
        earlier = verify.package_filenames(preview)
        edition = verify.package_filenames(preview, documentation_revision=1)
        preview_additions = verify.RELEASE_NOTES | {verify.WORKBUDDY_SKILL} | verify.ISOLATED_BROWSER_EXAMPLE
        self.assertEqual(edition - earlier, preview_additions)
        self.assertEqual(edition, verify.package_filenames("1.1.0-preview.14"))
        version = "1.1.0"
        current = verify.package_filenames(version)
        self.assertEqual(current - edition, {"CLIENTS.md", "CLIENTS.en.md"})
        for name in verify.runtime_filenames(version) - verify.runtime_filenames():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture runtime\n")
        for name in current - {verify.RUNTIME, verify.DEPENDENCIES}:
            directory = "product" if name in builder.PRODUCT_DIRECTORY_INPUTS else ""
            path = self.source / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith(".md"):
                shutil.copyfile(builder.SOURCE / directory / name, path)
            elif not path.exists():
                path.write_bytes(b"fixture\n")
        self.select_version(preview)
        self.assertEqual(verify.verify_archive(self.build()["archive"]), self.release)
        self.release["documentation_revision"] = 1
        self.select_version(preview)
        previous = self.build()
        self.assertTrue(previous["archive"].endswith("-1.1.0-preview.13-docs.1.zip"))
        self.assertEqual(verify.verify_archive(previous["archive"]), self.release)
        del self.release["documentation_revision"]
        self.select_version(version)
        result = self.build()
        unpacked = self.work / "unpacked"
        with zipfile.ZipFile(result["archive"]) as archive:
            archive.extractall(unpacked)
        self.assertEqual(verify.verify_directory(unpacked), self.release)
        for name in current:
            if not name.endswith(".md"):
                continue
            manual = unpacked / name
            for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", manual.read_text(encoding="utf-8")):
                filename = target.partition("#")[0]
                if filename and not re.match(r"[a-z]+:", filename):
                    local = (manual.parent / filename).resolve()
                    self.assertTrue(local.is_relative_to(unpacked) and local.is_file(), (name, target))
        for name in preview_additions | verify.CLIENT_GUIDES:
            path = unpacked / name
            blob = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(ValueError, "missing or additional"):
                verify.verify_directory(unpacked)
            path.write_bytes(blob)

    def test_background_receiver_enters_the_kit_with_local_subscriptions(self):
        old, new = "1.1.0-preview.7", "1.1.0-preview.8"
        self.assertEqual(verify.package_filenames(new) - verify.package_filenames(old), {verify.MAIL_LISTENER_SETUP})

    def test_private_github_mail_adds_runtime_setup_and_guide(self):
        old, new = "1.1.0-preview.6", "1.1.0-preview.7"
        self.assertEqual(verify.runtime_filenames(new) - verify.runtime_filenames(old), {"src/runtime/github_mail.py"})
        self.assertEqual(verify.package_filenames(new) - verify.package_filenames(old),
                         {verify.GITHUB_MAIL_GUIDE, verify.GITHUB_MAIL_SETUP})

    def test_preview_six_contains_resident_hosts_discovery_and_transport_selection(self):
        old, new = "1.1.0-preview.5", "1.1.0-preview.6"
        self.assertEqual(verify.runtime_filenames(new) - verify.runtime_filenames(old),
                         {"src/adapters/resident.py", "src/adapters/host_catalog.py",
                          "src/adapters/mcp_catalog.py", "src/runtime/mail_routes.py"})
        self.assertEqual(verify.package_filenames(new) - verify.package_filenames(old), verify.HOST_GUIDES)

    def test_preview_four_adds_direct_mail_and_optional_queue(self):
        old, new = "1.1.0-preview.3", "1.1.0-preview.4"
        self.assertEqual(verify.runtime_filenames(new) - verify.runtime_filenames(old),
                         {"src/runtime/ssh_mail.py", "src/runtime/managed_queue.py"})
        self.assertEqual(verify.package_filenames(new) - verify.package_filenames(old), {verify.MANAGED_QUEUE_GUIDE})
        self.assertEqual(verify.runtime_filenames(new), verify.runtime_filenames("1.1.0-preview.5"))
        for filename in verify.runtime_filenames(new) - verify.runtime_filenames():
            (self.source / filename).write_bytes(b"fixture runtime\n")
        for filename in (verify.RELAY_ACL, verify.RELAY_GUIDE, verify.MANAGED_QUEUE_GUIDE):
            path = self.source / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"fixture instructions\n")
        (self.source / "product/README.en.md").write_bytes(b"[Queue](../design/managed-queue.md)")
        self.select_version(new)
        result = self.build()
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertEqual(archive.read("README.en.md"), b"[Queue](design/managed-queue.md)")
            self.assertEqual(archive.read(verify.MANAGED_QUEUE_GUIDE), b"fixture instructions\n")

    def test_license_and_citation_are_required_from_1_0_3_docs_3(self):
        self.select_version("1.0.3")
        for revision in (2, 3):
            with self.subTest(revision=revision):
                self.release["documentation_revision"] = revision
                (self.source / "product/release.json").write_text(json.dumps(self.release), encoding="utf-8")
                result = self.build()
                self.assertEqual(verify.verify_archive(result["archive"], result["sha256"]), self.release)
                with zipfile.ZipFile(result["archive"]) as archive:
                    for name in ("LICENSE", "CITATION.cff"):
                        if revision == 2:
                            self.assertNotIn(name, archive.namelist())
                        else:
                            self.assertEqual(archive.read(name), (self.source / name).read_bytes())
                    extracted = self.work / f"docs-{revision}"
                    archive.extractall(extracted)
                self.assertEqual(verify.verify_directory(extracted), self.release)
        (extracted / "LICENSE").unlink()
        with self.assertRaisesRegex(ValueError, "missing or additional"):
            verify.verify_directory(extracted)
        (self.source / "CITATION.cff").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required product file"):
            self.build()
        self.assertTrue({"LICENSE", "CITATION.cff"} <= verify.package_filenames("1.0.4"))

    def test_english_manuals_enter_the_kit_at_docs_5(self):
        for name in verify.ENGLISH_MANUALS:
            (self.source / "product" / name).unlink()
        metadata = self.source / "product/release.json"
        self.release["documentation_revision"] = 4
        self.select_version("1.0.3")
        previous = self.build()
        self.assertEqual(verify.verify_archive(previous["archive"]), self.release)
        with zipfile.ZipFile(previous["archive"]) as archive:
            self.assertFalse(verify.ENGLISH_MANUALS.intersection(archive.namelist()))

        self.release["documentation_revision"] = 5
        metadata.write_text(json.dumps(self.release), encoding="utf-8")
        (self.source / "README.en.md").write_text("Repository landing page", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing required product file"):
            self.build()
        for name in verify.ENGLISH_MANUALS:
            (self.source / "product" / name).write_text(f"Kit manual: {name}", encoding="utf-8")
        (self.source / "product/RELEASE-NOTES.en.md").write_text("Release page", encoding="utf-8")
        result = self.build()
        self.assertEqual(verify.verify_archive(result["archive"]), self.release)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertNotIn("RELEASE-NOTES.en.md", archive.namelist())
            for name in verify.ENGLISH_MANUALS:
                self.assertEqual(archive.read(name), (self.source / "product" / name).read_bytes())
            target = self.work / "bilingual"
            archive.extractall(target)
        self.assertEqual(verify.verify_directory(target), self.release)
        (target / "README.en.md").unlink()
        with self.assertRaisesRegex(ValueError, "missing or additional"):
            verify.verify_directory(target)

    def test_extra_unknown_file_or_directory_and_manipulated_manifest_are_rejected(self):
        archive = self.build()["archive"]
        extracted = self.work / "extracted"
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        extra = extracted / "local-state"
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, "Unexpected package directory"):
            verify.verify_directory(extracted)
        extra.rmdir()
        extra.write_bytes(b"unlisted")
        with self.assertRaisesRegex(ValueError, "missing or additional"):
            verify.verify_directory(extracted)
        extra.unlink()
        manifest = extracted / verify.MANIFEST
        old = manifest.read_text(encoding="utf-8")
        manifest.write_text('{"schema":1,' + old[1:], encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            verify.verify_directory(extracted)


if __name__ == "__main__":
    unittest.main()
