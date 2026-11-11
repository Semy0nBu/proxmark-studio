# Data and privacy

[Русский](PRIVACY_RU.md)

## Data location

By default, the packaged application stores cards, keys, settings, and operation results in
`%LOCALAPPDATA%\Proxmark Studio`. Source mode uses `data/personal`.
Open the data folder through **About**. The `--data-dir` and `--demo` options
can select a different profile.

Uninstalling keeps your library. To remove your data, close the application and
delete the user-data folder separately. Files exported elsewhere and copies you
have made remain in their chosen locations.

## Encryption and logs

The application **does not encrypt its library**. MIFARE keys are stored in an
ordinary SQLite database; dumps and backups are stored in the database and files.
Their protection depends on Windows permissions and the security of your computer.

The application can export a password-protected whole-profile
archive from **Library…**. AES-256-GCM protects the archive, including filenames;
the password is not saved. Without a password it is an ordinary, unencrypted ZIP.
Active and restored profiles remain unencrypted. Backups include logs and other
profile files, which may contain sensitive data. See [Library backup](LIBRARY_BACKUPS.md).

The console keeps the last 100 commands, including their arguments. History,
dictionaries, reports, and client output can contain UIDs, keys, passwords, and
card contents. The client's `--incognito` option does not disable the GUI's own
logs and history. Console history can be cleared in its interface; this does not
remove data from other reports, the library, or backups.

Named quick actions keep their command arguments in `quick-actions.json`.
The selected-client catalog cache contains the local executable path and its
offline help. These are private profile files and are included in whole-profile
backups. The diagnostic ZIP is separate: it exports only the reviewed,
allowlisted versions, states, client hash and numeric codes, without raw logs,
commands, identifiers, library data, settings or paths. It is not uploaded.

Review logs and screenshots before sharing them. An ordinary bug report usually
needs the version, reproduction steps, and a relevant output excerpt. Do not
attach the entire library folder.

## Network access

The GUI code has no built-in analytics, cloud synchronization, or automatic
library upload. Normal USB or Blue Shark operation does not require internet
access. TCP mode connects the client to the endpoint you specify.

The GUI can also launch scripts from the bundled client or a client you select.
Those scripts are not network-sandboxed. For example, the bundled
`fm11rf08s_full.py` sends card block 0 to an external Fudan signature-validation
service when explicitly given `--validate`. This requires internet access and
the `requests` package. That request is not made without the option.

Check what third-party scripts do before running them. Installing Python packages
and building from source can also contact dependency servers specified by those
tools. These are separate actions, not a GUI library upload.

## Download contents

The download contains the application, client, firmware, public resources,
licenses, and corresponding component sources. It excludes the developer's
personal library, dumps, keys, and logs. The README screenshot uses demo data.

This page describes the current implementation. It is not a substitute for an
audit of every third-party script and its network traffic.
