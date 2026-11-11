# User guide

## First launch

Windows 10 (1903+) / 11 x64 is required. Run
the release's `ProxmarkStudio-<version>-Setup-x64.exe` and choose English or Russian for
the installer. It installs for the current user without administrator rights.
The default folder is `%LOCALAPPDATA%\Programs\Proxmark Studio`; Desktop and
Start Menu shortcuts are selected by default.

For the portable version, extract the complete ZIP and open `ProxmarkStudio.exe`.
Keep the `_internal`, `client`, `firmware`, `licenses`, and `sources` directories
together. Python installation is not required.
Use **Settings → Language** to choose the interface language, then restart.
Interface mode switches immediately: Guided shows explanations and Compact
keeps optional details collapsed. Updates preserve custom profile arguments in
this installation's Desktop and Start Menu shortcuts.
**About** shows the version and opens the library folder.

## Workbench layout

Use the top navigation to switch between Workspace, Cards, MIFARE keys,
Antennas, Encodings and Laboratory. The connection state and Stop remain visible
across all pages. **Connection…** opens the device settings.

Cards opens on the working HEX editor. Its gutter shows byte offsets without
adding them to the dump. Manufacturer data, key fields and access fields use
different colors; unread bytes keep their own warning highlight. The inspector
follows the cursor and sector selection. Recorded key availability and saved
access conditions describe the selected snapshot, not a fresh authentication.
Use **Text** for an optional decoded view; Overview, Changes and Operation result
remain separate tabs.

## Connection

**USB:** connect the RDV4.01 using a data-capable cable, select USB and the Proxmark
COM port, then Connect. Wait for a verified connection. If no port appears, check
the cable and Windows Device Manager → Ports. Close another client using that port.

**Blue Shark:** enable power and BTpower, pair it in Windows, select Blue Shark,
and choose its outgoing SPP COM port. This is Bluetooth Classic SPP. Matching
BTADDON firmware and a compatible client are required.

Click **Connect** to use direct Bluetooth with the bundled client. Leave
**Keep connection** enabled; disabling it uses the legacy COM path for individual commands.

With the bundled client, the selected COM entry identifies your already paired
Blue Shark from Windows' saved pairing data. The client then opens one Bluetooth
connection directly. A fresh reply from the Proxmark confirms the connection;
finding a paired device alone does not establish a working link.

Device lookup and connection setup have time limits and can be cancelled with
Stop. If connection fails, check BTpower, Windows pairing and the selected outgoing
Bluetooth port, then connect again. This update does not require changes to
pairing, drivers or firmware. Older custom clients without direct Bluetooth
support continue to use the selected COM port.

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

In **Card details**, choose or type a **Collection** and enter comma-separated
**Tags**, then Save. These labels belong to this library entry, even when another
entry has the same UID. Updating them preserves the selected memory revision;
it does not add a memory version or change the dump. Clearing a field removes
the assignment. Up to 32 tags are supported, with 48 characters per tag.

Search includes collections and tags. The collection and tag selectors combine
with the text search and HF/LF filter. **Unsorted** finds entries without a
collection. Filtering preserves unsaved edits; changing to another card still
offers Save, Discard or Cancel.

The **Overview** tab shows coverage for the selected saved revision or working
copy. **Changes** compares two explicitly selected versions; unknown fillers
are not new data. **Operation result** explains an interrupted HF write and
offers a new read-and-compare step. See [Card workspace](CARD_WORKSPACE.md).

## MIFARE keys

Import dictionaries or saved Flipper files, or retrieve supported files over
Flipper USB. Imported keys are candidates. Client verification is recorded
separately from import. Legacy system sources follow the selected language;
filenames and user labels remain intact. Displayed times use UTC+3 without a suffix.

## Write

Use **Settings → Interface mode** for **Guided** explanations or
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

## Quick actions and client capabilities

Open **Laboratory → Console → Quick actions…** to keep a named sequence in
the current profile. Choose New, enter a name and one command per line, select
the per-command timeout, and Save. A sequence accepts 1–20 commands. Opening,
selecting or saving it sends nothing to the reader.

**Review and run…** shows the sequence before starting it. Commands run in
order; an error or Stop ends the remaining steps. Completed writes are not
undone. **Without a device** is for offline client operations; RF commands
require device mode. The saved commands may contain your own arguments or keys,
so treat the profile and its backup as private data.

Open **Laboratory → Console → Selected client catalog…** to collect help from
the selected client without opening a reader. Choose **Update from client**
after changing the client EXE.
The saved catalog is tied to that executable's SHA256; its comparison shows
added, removed and changed commands. A help entry does not prove support on
your card or firmware. New or changed syntax remains inspectable while old
forms cannot execute it as though it were confirmed. Cancel stops the update;
opening a catalog or a confirmed form does not start a command.

See [Desktop workflows](DESKTOP_WORKFLOWS.md) for these tools and diagnostics.

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

**Settings → Library…** creates one archive
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

## Troubleshooting and diagnostic packages

Open **Laboratory → Diagnostics → Diagnostic report…**, refresh the snapshot
and review the displayed JSON, then choose **Save ZIP…**. The package contains application/runtime
versions, the client hash, connection/operation state and numeric completion
codes. Dumps, keys, card IDs, commands, raw logs, settings and personal paths
are excluded. This action does not query the device or upload anything. Send
the ZIP yourself with the steps that triggered the problem. **Save log…** is
a separate raw export and can include card data and keys.

| Message | What to check next |
| --- | --- |
| `Win32 error 2` opening a COM port | The selected port no longer exists. Refresh ports, check the USB data cable and select the currently enumerated Proxmark. |
| `Win32 error 5` / access denied | Another client or app may hold the port. Disconnect that client, then reconnect from one application. |
| `Win32 error 1168` on Blue Shark | On the legacy COM path, Windows could not open the SPP port. Check Battery/BTpower and Windows pairing. Older custom clients still use this path; the bundled client supports a direct Bluetooth connection. |
| `Win32 error 121` | The Windows operation timed out. Check power, radio range or the cable, then request a fresh connection check. This does not establish a card-authentication failure. |
| CRC or truncated bulk transfer | The received data is not confirmed. Stop, wait for cleanup and a fresh connection check, then retry a read. Do not automatically repeat an uncertain write. |
| `AcquireEncryptedNonces: Auth2 error` | A nested authentication step was rejected. Check the antenna position, selected card profile and known keys. This is a card/RF step; it does not by itself establish a Bluetooth disconnect. |

The client's exit code `-5` and **Win32 error 5** are different values from
different layers. Read the associated message before choosing a remedy. During
an active firmware write, allow the flasher to finish; reconnecting power is
not a normal way to clear an interface error.

## Recorded hardware coverage

These are dated checks on a specific RDV4.01, not a promise that every feature
works with every card. No card UID or personal dump is needed to identify the
scope of a test.

| Date | Card or transport | Confirmed scope and limits |
| --- | --- | --- |
| 2026-11-09 | MIFARE Classic 1K, Magic Gen1a | Detection, partial/full reading, resuming missing data, and one user-block write (block 4) followed by restoration. The final image matched the original. UID/key/access-bit changes and other Magic generations were not established by that test. |
| 2026-11-09 | Ordinary EM410x LF card | Identifier reading and automatic intake. Its writable-chip type was not confirmed; no card memory was written. |
| 2026-11-09 | Card sold or described as “H2” | Not identified. A successful read or write is not claimed. |
| 2026-11-09 | T5577 blank | No confirmed writable blank was available. Physical LF writing remains unverified. |
| 2026-11-11 | USB, preview.7, no card | Fresh version replies, bulk transfer, cancellation with RF cleanup, and reconnection checks passed. No card or firmware writes were performed. |
| 2026-11-11 | Blue Shark, preview.7 | The SPP port was unavailable with Windows error 1168. Radio-outage recovery was not retested in that run. |

Other families, NDEF writing and compatibility with an external reader need
their own physical checks. Software and offscreen UI tests use simulated
responses; they do not extend this hardware coverage.
