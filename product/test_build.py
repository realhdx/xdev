import hashlib
import importlib.util
import json
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
        for name in verify.ROOT_FILES:
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
        metadata.write_text(json.dumps({**self.release, "version": "2.0.0"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "product identity"):
            self.build()
        self.assertFalse(list(self.output.glob("*.zip")))

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

    def test_invalid_documentation_revision_is_rejected(self):
        metadata = self.source / "product/release.json"
        for value in (None, 0, -1, False, True, 1.0, "1"):
            with self.subTest(value=value):
                metadata.write_text(json.dumps({**self.release, "documentation_revision": value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "documentation_revision"):
                    self.build()
        self.assertFalse(list(self.output.glob("*.zip")))

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
