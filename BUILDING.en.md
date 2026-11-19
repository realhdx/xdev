# Building xdev from source

**English** · [简体中文](BUILDING.md)

Use 64-bit CPython 3.14 on Windows x64. The core commands use only the Python standard library. From the repository root, check the version and run the test suites.

```powershell
py -3.14 -B -m src.cli.main --version
py -3.14 -B -m unittest discover -s tests
py -3.14 -B -m unittest discover -s deploy
py -3.14 -B -m unittest discover -s product
```

These tests use temporary directories and simulated peers. To verify SSH between two PCs, check that a client has loaded xdev, or confirm delivery in a specific existing chat, follow the [installation and user guide](product/README.en.md).

## Build the Windows installer archive

Run the following PowerShell commands from the repository root. The download step needs access to PyPI. Once the dependencies are available locally, both building and installation can run offline. The [dependency manifest](dependencies/windows-x64-cp314/bundle-manifest.json) lists the name, version, filename and source for each dependency.

```powershell
$deps = '.\dependencies\windows-x64-cp314'
py -3.14 -m pip --isolated download --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes -r "$deps\requirements.lock.txt" -d "$deps\wheelhouse"
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare offline dependencies' }
py -3.14 -I -B -X utf8 product/build.py --wheelhouse $deps --output-dir .\build
```

The builder checks the name, size and SHA-256 digest of all 31 dependency files. It also checks that the runtime built from source matches the recorded digest. The ZIP archive and its matching `.sha256` file appear in `build`. If you already have an extracted xdev release with the same dependency versions, replace `--wheelhouse $deps` in the build command with `--dependencies '<extracted-directory>\assets\optional-deps.zip'`.

To verify the finished archive, pass its path and expected digest to the verifier.

```powershell
py -3.14 -I -B -X utf8 product/verify.py '<path-to-generated-zip>' --sha256 '<expected-sha256>'
```

Each release records the runtime and offline dependency digests in [product/release.json](product/release.json). Building the same version from identical source bytes and dependencies reproduces the same ZIP. When preparing a new version, update its release metadata as described in the [changelog](product/CHANGELOG.en.md). For installation, migration and client setup, see the [deployment guide](product/AI-DEPLOY.en.md).
