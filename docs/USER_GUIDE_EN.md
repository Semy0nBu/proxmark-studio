# User guide

## First launch

Windows 10 (1903+) / 11 x64 is required. Run
`ProxmarkStudio-0.3.0-preview.1-Setup-x64.exe` and choose English or Russian for
the installer. It installs for the current user without administrator rights.
The default folder is `%LOCALAPPDATA%\Programs\Proxmark Studio`; Desktop and
Start Menu shortcuts are selected by default.

For the portable version, extract the complete ZIP and open `ProxmarkStudio.exe`.
Keep the `_internal`, `client`, `firmware`, `licenses`, and `sources` directories
together. Python installation is not required.
Choose the interface language at the bottom of the sidebar, then restart.
Interface mode switches immediately: Guided shows explanations and Compact
keeps optional details collapsed. Updates preserve custom profile arguments in
this installation's Desktop and Start Menu shortcuts.
**About** shows the version and opens the library folder.

## Connection

**USB:** connect the RDV4.01 using a data-capable cable, select USB and the Proxmark
COM port, then Connect. Wait for a verified connection. If no port appears, check
the cable and Windows Device Manager → Ports. Close another client using that port.

**Blue Shark:** enable power and BTpower, pair it in Windows, select Blue Shark,
and choose its outgoing SPP COM port. This is Bluetooth Classic SPP. Matching
BTADDON firmware and a compatible client are required.

**TCP:** enter `tcp:host:port` for an existing compatible network bridge.
The stock Blue Shark does not host that server.

On first launch, the complete release selects its bundled client. An external
compatible `proxmark3.exe` can be chosen in the expanded connection settings.
Updates preserve that external-client setting. If moving from portable leaves
a path to the old folder, select `client\proxmark3.exe` inside the installed
application and save settings.

## Read and organize

Choose HF or LF and place a card on the appropriate antenna. Detection identifies
the protocol; supported reading saves data to Cards. EM410x saves an identifier.
Automatic HF + LF intake handles a presented card once and rearms after removal.

A partial-read prompt may offer to preserve the available data. Missing positions
are zero-filled and remain marked as unknown. Editing them does not prove they
were read. Resume reading can fill Classic gaps after fresh card and key checks.

Use Cards to rename, annotate, import/export, inspect HEX, and save edits as a new
revision. Encodings provides text and printable-string views. Hiding MF keys
changes the view, preserving the source data.

## MIFARE keys

Import dictionaries or saved Flipper files, or retrieve supported files over
Flipper USB. Imported keys are candidates. Client verification is recorded
separately from import. Legacy system sources follow the selected language;
filenames and user labels remain intact. Displayed times use UTC+3 without a suffix.

## Write

Use the sidebar **Interface mode** selector for **Guided** explanations or
**Compact** details on demand. The choice takes effect immediately and is saved
for the current profile. Both modes keep the same operations and write checks.

Auto-write prepares one HF target: source, target check, backup, plan review,
write, and readback. Unknown source positions are not treated as confirmed zeros.
UID changes, system pages, and access rights use their supported dedicated flows.
The source and blank appear side by side, with the checked scope and reasons for
restrictions. See [Source → blank → write](CARD_TRANSFER.md) for the full workflow.

Use family tools and a compatible writable blank for LF. An EM410x read alone
does not establish a T5577 chip. Laboratory provides family workflows, blocks,
NDEF, emulation, traces, signals, and board files. Check the actual target type
before choosing a family-specific operation.

## Stop and connection status

Stop remains available at the top. After cancellation, the app may verify the
board response before releasing the next operation. Open Client log or Ctrl+J
for details. When connection is lost, check power and transport and wait for
confirmed board state before starting a new operation.

## Firmware

The complete bundle includes RDV4 profiles with and without Blue Shark. Select
the appropriate profile and use USB. Review the plan; the app verifies files,
backs up the board, and replaces the main image while preserving the bootloader.
Neither the application installer nor connecting a board flashes firmware automatically.

## Data, backup, and updates

**Library…** in the sidebar creates one archive
of your saved library, with optional password protection. **Restore** creates a
separate profile. See [Library backup](LIBRARY_BACKUPS.md) for the steps and format.

The EXE stores data in `%LOCALAPPDATA%\Proxmark Studio`: SQLite, settings,
revisions, imports, and operation logs. Use About → Open library folder.

Before updating or uninstalling, finish operations and close the GUI and Proxmark
client normally. Allow firmware installation to finish; do not force-stop the
process to release files. Use **Library…** to create a backup archive. Copying
the entire data folder with the application closed is also supported.

To update an installed copy, run the new Setup. For portable updates, extract the
new release separately and retain the old folder until the update is checked.
Both variants use the same user library. Arbitrary downgrades are not supported;
changes to the data format require separate compatibility testing.

Uninstall through Windows Settings → Apps. Uninstalling preserves
`%LOCALAPPDATA%\Proxmark Studio`, including the library, settings, and backups.
Reinstalling uses that data. When moving to another data location, copy the
complete library with both instances closed and preserve the original copy.

For an issue report, include the About version, transport, and relevant log
excerpt. Card libraries, dumps, and key files contain card data and are not
needed for an ordinary interface report.
