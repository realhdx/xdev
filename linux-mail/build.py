"""Build the portable Linux Mail preview from the pinned, public xdev core."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import zipfile


CORE = "b31a312d0f7a30a1bce316effe7324beb593a070"
VERSION = "1.1.0-preview.1"
NAME = "xdev-linux-mail-" + VERSION
HERE = Path(__file__).resolve().parent


def add_file(archive, path, data, mode=0o644):
    item = tarfile.TarInfo(f"{NAME}/{path}")
    item.size = len(data)
    item.mode = mode
    archive.addfile(item, io.BytesIO(data))


def build(output):
    source = subprocess.check_output(
        ["git", "archive", "--format=zip", CORE, "--", "src", "LICENSE", "CITATION.cff"],
        cwd=HERE.parent,
    )
    output.mkdir(parents=True, exist_ok=True)
    target = output / (NAME + ".tar.gz")
    # Keep the archive independent of user, machine, checkout path and local line endings.
    with tempfile.TemporaryDirectory(dir=output) as staging:
        temporary = Path(staging) / target.name
        with zipfile.ZipFile(io.BytesIO(source)) as core, temporary.open("wb") as dest:
            with gzip.GzipFile(filename="", fileobj=dest, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for entry in sorted(core.namelist()):
                        if entry.endswith("/"):
                            continue
                        data = core.read(entry)
                        path = "runtime/" + entry if entry.startswith("src/") else entry
                        if entry == "src/product.json":
                            product = json.loads(data)
                            product.update(series="linux-mail", version=VERSION)
                            data = (json.dumps(product, indent=2) + "\n").encode()
                        add_file(archive, path, data)
                    for filename in ("README.md", "README.en.md", "xdev"):
                        data = (HERE / filename).read_text(encoding="utf-8").encode("utf-8")
                        add_file(archive, filename, data, 0o755 if filename == "xdev" else 0o644)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(target)
    target.with_suffix(target.suffix + ".sha256").write_bytes(
        f"{digest}  {target.name}\n".encode("ascii")
    )
    print(f"{digest}  {target.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
