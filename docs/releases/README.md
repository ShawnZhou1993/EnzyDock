# Releases

One file per published release: `vX.Y.Z.md`, in the order they were published.
The file is the **body of the GitHub Release**, so it has to be written the
way a receiver reads it — what to download, how to verify it, what it needs,
what it cannot do.

- [v0.1.0](v0.1.0.md) — first public release

---

## Why this file is in the repository

The first v0.1.0 shipped three defects that nobody would have found locally:

- the `.sha256` sidecar had CRLF line endings, so `sha256sum -c` on macOS or
  Linux reported a **perfect download as corrupt**;
- the `.sh` files inside the ZIP had lost their executable bit, so on a Mac
  they failed with "Permission denied";
- the bundle manifest was not a valid checksum file.

All three were caught by one action: **downloading the published assets and
running the receiver's own commands on them**. That is the step in the
checklist below, and it is not optional.

---

## Checklist for publishing a release

### 1. Before you build

```bat
.venv\Scripts\python.exe -m pytest -q
git status --short              REM must be empty
```

Re-count the tests — the badge in `README.md` is a literal number:

```bat
.venv\Scripts\python.exe -m pytest -q --collect-only
```

### 2. Build and pack (Windows)

```bat
.venv\Scripts\python.exe scripts\build_windows.py --zip
.venv\Scripts\python.exe scripts\verify_bundle.py
.venv\Scripts\python.exe scripts\pack_distribution.py --platform windows
```

Note the **sha256** the packer prints — the release notes must quote the
digest of the archive that is actually uploaded, not of an earlier build.
Every re-pack changes it, because the bundle manifest inside carries a
timestamp.

### 3. Build and pack (macOS)

PyInstaller cannot cross-compile, so this runs **on a Mac**:

```sh
sh scripts/build_mac.sh --zip
.venv/bin/python scripts/verify_bundle.py --dist dist/EnzyDock.app
```

Produces `dist/EnzyDock-<version>-macos-arm64.zip` and its `.sha256` sidecar.

### 4. Tag, upload, publish

```bat
git tag -a vX.Y.Z -m "EnzyDock X.Y.Z" <commit>
git push origin vX.Y.Z
```

```bat
REM gh is not on PATH by default after the winget install
set PATH=%PATH%;C:\Program Files\GitHub CLI

gh release create vX.Y.Z --repo ShawnZhou1993/EnzyDock ^
   --title "EnzyDock X.Y.Z" --notes-file docs\releases\vX.Y.Z.md ^
   dist\EnzyDock-X.Y.Z-win64.zip dist\EnzyDock-X.Y.Z-win64.zip.sha256
```

`gh auth login` is only needed once per machine. The device-code flow works
from a non-interactive shell and copies the code to the clipboard.

### 5. Verify the way a receiver will

This is the step that catches things nothing else does:

```bat
gh release download vX.Y.Z --dir build\verify --clobber
cd build\verify
sha256sum -c EnzyDock-X.Y.Z-win64.zip.sha256          REM expect: OK, no warning
REM unpack, then:
cd EnzyDock
sha256sum -c BUNDLE-MANIFEST.txt                      REM expect: one OK per file, exit 0
```

Check the macOS assets the same way, plus the permissions inside the ZIP:

```bat
python -c "import zipfile;[print(i.filename, oct(i.external_attr>>16)) for i in zipfile.ZipFile('kit.zip').infolist() if i.filename.endswith(('.sh','.command'))]"
```

Every `.sh` / `.command` must be `0o100755`.

### 6. Adding the macOS build to an existing release

When somebody with a Mac produces the `.app`, upload it to the release that
already exists rather than cutting a new one:

```sh
gh release upload v0.1.0 EnzyDock-0.1.0-macos-arm64.zip EnzyDock-0.1.0-macos-arm64.zip.sha256 \
   --repo ShawnZhou1993/EnzyDock
```

Then add the row to the **Downloads** table in that release's notes file, and
commit the updated file. The SHA256 quoted in the notes must come from the
packer output of that run.

---

## Asset naming

The names are not chosen by hand — they come from
`scripts/packaging_layout.py`, which the packer, the splitter and the bundle
verifier all read:

| Platform | Archive | Sidecar | Helper scripts |
|---|---|---|---|
| Windows | `EnzyDock-<ver>-win64.zip` | `*.zip.sha256` | `check-install.bat`, `HOW-TO-RUN.txt` |
| macOS | `EnzyDock-<ver>-macos-arm64.zip` | `*.zip.sha256` | `check-install.sh`, `HOW-TO-RUN-mac.txt` |

On Windows the helpers and the manifest live **inside** the bundle. On macOS
they live **beside** `EnzyDock.app` — a file dropped inside a package is
invisible — so the packer adds them to the archive explicitly.

---

## Writing the notes

- Lead with the download table. A receiver should not have to read prose to
  find out which file to click.
- Quote the real SHA256 of the archive that is attached.
- Say what the platform **cannot** do. v0.1.0 ships a macOS *build kit*
  rather than an `.app`, and saying that up front saves a support round.
- End with the papers to cite. Users publishing results cite AutoDock Vina,
  RDKit and 3Dmol.js — not this shell.