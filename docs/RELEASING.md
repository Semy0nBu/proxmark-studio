# Release preparation

`pm3gui/version.py` is the single application version. This preview targets
Windows x64 and the RDV4.01 / Blue Shark test setup. Publishing is a separate step;
local release tooling does not push commits, create GitHub releases, or upload files.

## Build and review

1. Run the complete software suite and English audit.
2. Build into a new output folder using the reviewed native client and image inputs.
3. Include notices, exact modified native source, app source, and matching copyleft
   runtime sources. Check source indexes and the application file manifest.
4. Run the real frozen EXE diagnostic in English and Russian from a relocated
   folder with a clean PATH, outside the source checkout. Compare bundle files
   before/after. Confirm that the library is outside the application folder.
5. Verify both firmware profiles without flashing. Hardware testing is a separate,
   explicitly selected operation and is recorded separately from offline checks.
6. Create the portable ZIP, then compile the single-EXE Windows installer from
   the same verified complete payload using the locked Inno Setup compiler.
7. Test current-user installation without elevation, default Desktop/Start Menu
   shortcuts, a path containing spaces and non-ASCII characters, offline GUI/native
   diagnostics, reinstall, and uninstall. Use synthetic data to verify that the
   library, settings, backups, and unknown user files survive uninstall. Check
   that running GUI/native processes are not force-stopped.
8. Record the installer manifest and SHA256SUMS for both artifacts. Code signing
   is not configured for this first preview; the release manifest records
   `signed: false`.

A fresh profile and restricted PATH on the build machine are not a clean Windows
VM test. Record that distinction in acceptance results. A separate clean PC and
hardware test are required before describing those environments as verified.

## Publication snapshot

`tools/build_release.py` creates a source snapshot under `output/source/proxmark-studio`
using an explicit allowlist. Use that snapshot for a future GitHub repository.
The development workspace also contains private lab reports and experiments;
its entire contents are not the publication source.

The snapshot excludes personal databases/settings, dumps, keys, Flipper imports,
operation sessions, raw device backups, reference archives, the iPhone/SPI experiments,
build environments, local build-path manifests, and hardware reports.
Public default dictionaries from the upstream native repository are distinguished
from personal dictionaries by the native allowlist.

The included GitHub workflow runs tests and catalog auditing only. It does not
publish. Before creating a release, run the local build/review steps and attach
the portable archive, `ProxmarkStudio-0.3.0-preview.1-Setup-x64.exe`, and their
checksums. Keep source archives and dependency notices available with the
corresponding binary version. Include the installer script, builder, toolchain
lock, tests, and Inno Setup notice in the explicit publication allowlist.

The installer targets `%LOCALAPPDATA%\Programs\Proxmark Studio`; user data stays
in `%LOCALAPPDATA%\Proxmark Studio` and survives uninstall. Before updates or
uninstall, finish operations and close the GUI/native client normally. Never
force-close a firmware operation. Installation itself must not connect or flash.

For installed updates, preserve the AppId, user data, and settings. Test removal
of obsolete installer-owned files without deleting unknown user files. Portable
updates go in a separate folder; retain the old program until validation is
complete. Arbitrary downgrades are unsupported; future data migrations need
explicit compatibility and backup tests.
