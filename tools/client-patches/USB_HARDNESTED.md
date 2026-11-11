# Native USB and Hardnested changes in preview.12

For a new build, use `sources/proxmark3-corresponding-source.zip` from the current
full portable/installer payload. It includes every local change, the new DCB
header, generated version metadata, source manifest and Windows UTF-8 recipe.
Base: RRG/Iceman `v4.23346`, commit `dc0110c29d320e04b37ea436ee2924901407ae72`.
The new native version is `Iceman/ProxmarkStudio/v4.23346-usb-cpu-fix`, ARM checksum
`c3f8ba2a1`. Sources retain upstream GPL-3.0-or-later licensing and attribution.

The changes bound interrupted USB control/status/bulk waits, initialize Windows
DCB settings from the driver, preserve original API errors, and cooperatively
cancel Hardnested preparation/SIMD search with all workers joined before cleanup.

## Reviewing the delta

[`usb-hardnested-v4.23346.patch`](usb-hardnested-v4.23346.patch) applies to the exact
**preview.10 corresponding-source archive**, not plain upstream. Base archive SHA256:
`664512a5b6d8a77c062fd465c5e4615b4d880bf20a5306d6e2981c0f157b3c4e`.
[`usb-hardnested-sources.json`](usb-hardnested-sources.json) pins both archives,
client hashes, firmware images and all ten changed paths, including one new header
and three generated version files. Earlier patches alone do not produce this backend.

Preserve file bytes and line endings. From a freshly extracted `proxmark3` folder
with the patch in its parent folder, PowerShell:

```powershell
git init --quiet
$nativePatch = (Resolve-Path '../usb-hardnested-v4.23346.patch').Path
[IO.File]::WriteAllText((Join-Path (Get-Location) '.git/info/attributes'), "* -text`n", [Text.UTF8Encoding]::new($false))
git -c core.autocrlf=false apply --check $nativePatch
git -c core.autocrlf=false apply $nativePatch
```

Both apply checks passed on an isolated exact base; all **2804 target source-file
hashes** matched the current full archive, with no extra source files. The minimal
code delta does not replace the old archive's generated manifest/UTF-8 proof:
those remain historical. Use the current full archive for its current complete metadata.

Build notes are in that archive and [`docs/BUILDING.md`](../../docs/BUILDING.md);
production-code test runners are in [`native-client-tests/README.md`](../native-client-tests/README.md).
GCC 16.2.0/ProxSpace UCRT64 was used; binary hashes can vary with timestamps/toolchains.
Build platform variants sequentially. The packaged Windows client also uses the
resource-only UTF-8 transformation. Application installation does **not** flash a
board: CPU cancellation is a client fix, while the USB driver fix needs the matching
main firmware through the existing reviewed firmware workflow. Bootloader and Blue
Shark radio firmware are outside that update. This audit did not build or flash anything.
