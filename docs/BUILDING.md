# Build from source

Running from source requires Python 3.12+, PySide6 Essentials, and pyserial.
The reviewed portable build requires **Python 3.12.14 x64**; its version must match
`tools/release_gui_sources.json`. Check `python --version` before creating the
build environment below. The tested build host is Windows x64. Source mode
retains `data/personal`; frozen mode uses LocalAppData.

```powershell
python -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-release\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-release\Scripts\python.exe tools/audit_english_catalog.py --output build/catalog-audit.json
```

To change the build Python or GUI dependencies, review the new versions and
update `requirements-build.txt` and `tools/release_gui_sources.json` together.
Replace the corresponding official source archive entries, sizes and SHA256
hashes in the source lock, then rebuild and repeat tests and frozen diagnostics.
The collector rejects runtime versions that differ from the reviewed lock.
The native client's private MinGW Python is a separate runtime.

For a GUI update, reuse the verified public backend from a complete previous
portable bundle. This preserves its matched client, images, notices and sources:

```powershell
.\.venv-release\Scripts\python.exe tools/build_release.py --base-bundle "D:\PreviousRelease\ProxmarkStudio" --output-root dist
```

Build a new backend from a reviewed native source/build tree instead:

```powershell
.\.venv-release\Scripts\python.exe tools/build_release.py --native-source "D:\ProxmarkSource" --output-root dist
```

The native tree must contain the matching executable, public client resources,
reviewed runtime manifest, matching MinGW Python prefix, and local MSYS2 package
metadata. The firmware stage validates two previously reviewed RDV4 image manifests.
These are explicit binary inputs, not binaries from an arbitrary user's settings.
A public source checkout alone cannot invent these reviewed firmware inputs.

For GUI-only distribution, pass `--gui-only`; the recipient selects a separate
compatible client. The complete local release is the default for end users.

Build phases are `freeze`, `stage`, and `archive`; `all` runs them in order.
Use a new output directory for a new build. The builder does not delete existing
bundles. Native/GUI sources and notices are collected along with executable files.
Official source downloads are pinned or indexed by hash. GNU tar with zstd support
is used to inspect MSYS2 source archives; it can be selected by the source collector.

PyInstaller runs with a clean build PATH so unrelated development DLLs cannot be
collected from installed tools. The frozen GUI isolates its DLL, Qt-plugin and
Python environment from its native client. Do not merge their runtime directories.

```powershell
.\.venv-release\Scripts\python.exe tools/verify_release.py --application dist/ProxmarkStudio --output build/release-check.json
.\dist\ProxmarkStudio\ProxmarkStudio.exe --self-test build/frozen-check.json --language en
```

The diagnostic uses a temporary empty profile and offline native commands.
It does not access a board. For hardware acceptance, separately test connection,
reading, cancellation, reconnect and reviewed write/restore on designated cards.

The staged native executable uses an embedded process-local UTF-8 manifest for
Windows 10 1903+ / Windows 11. This fixes native Python home paths containing
non-ASCII characters. The transformation preserves code/imports/entrypoint and
COFF symbols, records its proof and updates the matched client hashes. Firmware
images are unchanged. Its XML, transformation tool and recipe accompany the
corresponding native source. No Windows-wide regional setting is changed.

Reference: [Microsoft process UTF-8 code pages](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page).

## Windows installer

Build the complete, verified `dist/ProxmarkStudio` payload first. The installer
wraps that folder in one Setup EXE; it retains the matching client, firmware
profiles, notices and corresponding sources.

Use **Inno Setup 7.1.1 x64** from its
[official release](https://github.com/jrsoftware/issrc/releases/tag/is-7_1_1).
The exact [download URL](https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe),
download SHA256, and `ISCC.exe` SHA256 are pinned in
[`installer/toolchain.json`](../installer/toolchain.json). Verify the download
against that lock. The builder also checks the compiler hash and version before
compiling; a different compiler requires a reviewed toolchain-lock update.

```powershell
.\.venv-release\Scripts\python.exe tools/build_installer.py --compiler "D:\InnoSetup\ISCC.exe" --application dist/ProxmarkStudio
```

The default output directory is `dist`. It contains
`ProxmarkStudio-0.3.0-preview.1-Setup-x64.exe`, `installer-manifest.json`, and the
updated `SHA256SUMS.txt`. Use `--output-root` to choose a different output folder.
The installer script and toolchain lock are included in the public source export.
The unmodified Inno Setup runtime retains its own
[license](licenses/inno-setup/LICENSE.txt).

Test installation, same-version reinstall, shortcuts, installed offline
diagnostics, and uninstall using isolated folders and synthetic user data.
Confirm that uninstall preserves the library and that a running GUI or native
client is not force-stopped. Packaging never connects to or flashes a board.
