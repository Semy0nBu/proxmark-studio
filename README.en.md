# Proxmark Studio

[Русский](README.md)

A desktop application for Proxmark3 RDV4.01: HF/LF reading, a card and key
library, dump editing, reviewed writes, antenna measurements, and RRG/Iceman
client tools. The interface supports English and Russian. Data stays local.

## Get the application

First distributable version: **0.3.0-preview.1, Windows 10 (1903+) / 11 x64**.

Run **`ProxmarkStudio-0.3.0-preview.1-Setup-x64.exe`** and choose the installer
language. It installs for the current user without administrator rights in
`%LOCALAPPDATA%\Programs\Proxmark Studio`. Desktop and Start Menu shortcuts are
selected by default.

Alternatively, extract the entire portable archive
`ProxmarkStudio-0.3.0-preview.1-windows-x64.zip` to a separate folder and open
`ProxmarkStudio.exe`. Connect your Proxmark over USB, choose **USB** and its COM
port, then **Connect**.

Python installation is not required. The complete bundle includes a matching
Proxmark client, its resources, and two RDV4 firmware profiles. Firmware updates
are a separate reviewed operation with a backup. Neither running Setup nor
connecting a board flashes firmware.

Both variants store your library and settings in `%LOCALAPPDATA%\Proxmark Studio`.
Use **About → Open library folder** to find it. Uninstalling the application
preserves this data. Before updating or uninstalling, finish operations and close
the GUI and Proxmark client normally; allow firmware installation to finish.
The release contains no developer card library, personal keys, captures, or logs.

[User guide](docs/USER_GUIDE_EN.md)

## Features

- Separate HF and LF workspaces, card detection, reading, and a local SQLite library.
- Automatic HF + LF intake with duplicate suppression while a card remains present.
- Complete and partial dumps with known-byte maps; resumable Classic reading.
- MIFARE Classic key provenance, candidate/verified status, timestamps, dictionary
  and Flipper file imports, and direct supported Flipper imports over USB.
- HEX editing, revision history, encoding views, and printable-string extraction.
- One-card HF auto-write: target checks, backup, plan review, and readback.
- LF, Classic, Magic, NDEF, emulation, traces, waveform/FFT, and SPIFFS tools.
- HF/LF antenna measurements and baseline comparisons.
- USB and Blue Shark Bluetooth Classic SPP. TCP connects to a separately configured
  compatible network bridge; the application does not host such a bridge.

The hardware test setup is an RDV4.01 with Blue Shark. A command appearing in the
client catalog does not establish physical testing of every supported card family.
An EM410x library record contains an identifier rather than full chip memory.
Some additional client Python scripts require external packages or tools.

## Run from source

Use Python 3.12 or newer on Windows:

```powershell
.\start.ps1
```

After initialization, `launch-gui.vbs` launches without a console.
A source checkout retains its development library in `data/personal`.
The packaged EXE uses the Windows user data directory instead.

```powershell
.\.venv\Scripts\python.exe run_gui.py --language en
.\.venv\Scripts\python.exe run_gui.py --demo
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

[Building](docs/BUILDING.md) · [Release preparation](docs/RELEASING.md)

## License

Proxmark Studio is licensed under **GPL-3.0-or-later**; see [LICENSE](LICENSE).
The RRG/Iceman client and third-party components retain their respective licenses.
The complete distribution includes notices, component versions, and corresponding
sources in `licenses/` and `sources/`.
See [third-party notices](THIRD_PARTY_NOTICES.md).
