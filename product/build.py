"""Build the reproducible xdev Windows product from its reviewed runtime and offline wheels."""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def source_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = source_module("xdev_product_verify", SOURCE / "product/verify.py")
deploy = source_module("xdev_release_deploy", SOURCE / "deploy/deploy.py")
optional = source_module("xdev_release_optional", SOURCE / "deploy/optional.py")


def source_inputs(source, version, documentation_revision=0):
    package = verify.package_filenames(version, documentation_revision)
    product_inputs = verify.ROOT_FILES | verify.ENGLISH_MANUALS
    paths = {}
    for name in package - {verify.RUNTIME, verify.DEPENDENCIES}:
        directory = source / "product" if name in product_inputs else source
        paths[name] = directory / name
    for name, path in paths.items():
        relative = path.relative_to(source)
        verify.physical(path)
        for directory in relative.parents:
            verify.physical(source / directory)
        if not path.is_file():
            raise ValueError(f"Missing required product file: {name}")
    return paths


def publish_file(staged, destination):
    try:
        if os.name == "nt":
            # On Windows rename is atomic and an existing destination is never replaced.
            staged.rename(destination)
        else:
            os.link(staged, destination)
    except FileExistsError:
        verify.physical(destination)
        if verify.file_digest(staged) != verify.file_digest(destination):
            raise ValueError(f"An existing release has different bytes: {destination.name}") from None


def build(source, output_dir, *, wheelhouse=None, dependencies=None):
    if (wheelhouse is None) == (dependencies is None):
        raise ValueError("Select the pinned wheelhouse directory or the existing dependency ZIP")
    source = Path(source).resolve(strict=True)
    metadata_path = source / "product/release.json"
    verify.physical(metadata_path.parent)
    verify.physical(metadata_path)
    release = verify.validate_release(verify.strict_json(metadata_path.read_bytes()))
    revision = release.get("documentation_revision", 0)
    files = source_inputs(source, release["version"], revision)
    edition = f"-docs.{revision}" if revision else ""
    reviewed = {p.relative_to(source).as_posix() for p in deploy.source_files(source)}
    if reviewed != verify.runtime_filenames():
        unexpected = sorted(reviewed ^ verify.runtime_filenames())
        raise ValueError(f"Runtime inventory differs from the reviewed file list: {unexpected}")
    output_dir = Path(output_dir).resolve()
    if any(output_dir.is_relative_to(source / tree) for tree in deploy.SOURCE_TREES):
        raise ValueError("Write the product outside the runtime source directories")
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"xdev-{release['series']}-{release['version']}{edition}.zip"
    dest = output_dir / filename
    with tempfile.TemporaryDirectory(prefix=".xdev-build-", dir=output_dir) as work:
        work = Path(work)
        files[verify.RUNTIME] = work / "runtime.zip"
        runtime = deploy.build(source, files[verify.RUNTIME])
        if runtime["release"] != release["runtime_sha256"]:
            raise ValueError(f"Runtime differs from release.json; reviewed digest is {runtime['release']}")
        files[verify.DEPENDENCIES] = work / "optional-deps.zip"
        if wheelhouse is not None:
            optional.package(Path(wheelhouse), files[verify.DEPENDENCIES])
        else:
            # Work from a snapshot. The pinned metadata and each original wheel
            # are checked before the dependency archive can enter the product.
            shutil.copyfile(dependencies, files[verify.DEPENDENCIES])
            scratch = work / "wheel-check"
            scratch.mkdir()
            optional.extract(files[verify.DEPENDENCIES], scratch)
        if release.get("optional_deps_sha256") != verify.file_digest(files[verify.DEPENDENCIES]):
            raise ValueError("Dependency ZIP differs from the reviewed optional_deps_sha256")
        manifest = {"schema": 1, "files": {}}
        candidate = work / filename
        with zipfile.ZipFile(candidate, "w") as archive:
            for name, path in sorted(files.items()):
                blob = path.read_bytes()
                record = deploy.name_record(name)
                # Nested archives are already complete, deterministic containers.
                if name in (verify.RUNTIME, verify.DEPENDENCIES):
                    record.compress_type = zipfile.ZIP_STORED
                archive.writestr(record, blob)
                manifest["files"][name] = {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            archive.writestr(deploy.name_record(verify.MANIFEST), deploy.canonical_json(manifest))
        verify.verify_archive(candidate)
        checksum = verify.file_digest(candidate)
        sidecar = work / (filename + ".sha256")
        sidecar.write_bytes(f"{checksum}  {filename}\n".encode("ascii"))
        dest_sum = output_dir / sidecar.name
        if dest_sum.exists() and (dest_sum.is_symlink() or dest_sum.read_bytes() != sidecar.read_bytes()):
            raise ValueError(f"Existing checksum differs: {dest_sum.name}")
        publish_file(candidate, dest)
        publish_file(sidecar, dest_sum)
    result = {"archive": str(dest), "sha256": checksum, "checksum_file": str(dest_sum),
              "series": release["series"], "version": release["version"], "runtime_sha256": runtime["release"],
              "files": len(manifest["files"])}
    if edition:
        result["documentation_revision"] = revision
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE / "product/releases")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--wheelhouse", type=Path, help="Bundle directory containing the pinned wheelhouse and metadata")
    inputs.add_argument("--dependencies", type=Path, help="Already packaged and pinned offline dependency ZIP")
    args = parser.parse_args()
    return build(SOURCE, args.output_dir, wheelhouse=args.wheelhouse, dependencies=args.dependencies)


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, **main()}, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
