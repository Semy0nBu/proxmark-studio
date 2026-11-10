# Third-party components

Proxmark Studio application code is GPL-3.0-or-later; see LICENSE. Component names,
copyright notices and licenses remain with their original authors.

## RRG/Iceman Proxmark3

The complete release includes a modified RRG/Iceman v4.23346 client and RDV4 images.
The changes cover the GUI command bridge, cancellation, Windows serial I/O,
large-buffer transport and USB reset handling. A process-local UTF-8 manifest
supports Unicode Windows paths without changing native code sections. The
preferred manifest, transformation tool and proof accompany the native sources.
The bootloader is not part of the
release images. The exact working sources and build inputs are supplied in
`sources/proxmark3-corresponding-source.zip`; upstream license text is retained.

Upstream: https://github.com/RfidResearchGroup/proxmark3

## Python, PySide6 / Qt, pyserial and packaging

The GUI includes Python, PySide6/Shiboken, Qt, and pyserial. Python uses the PSF
license, pyserial BSD terms, and the Qt/PySide components their selected open-source
license terms. Actual GPL/LGPL texts, copyright notices, versions and source
indexes are under `licenses/gui` and `sources/gui`.

PyInstaller includes its distribution exception; its notices are retained with
the GUI component licenses. Packaging does not change the dependencies' licenses.

Official references: https://www.python.org/psf/license/ ·
https://doc.qt.io/qt-6/licensing.html · https://doc.qt.io/qtforpython-6/licenses.html ·
https://pyserial.readthedocs.io/en/latest/appendix.html#license ·
https://pyinstaller.org/en/stable/license.html

## PyCryptodome

The GUI also includes PyCryptodome (the `pycryptodomex` distribution) for
optional password-protected library backups: AES-256-GCM and scrypt. Most code
is public domain; selected components use the BSD license. The complete
COPYRIGHT notice and matching source archive are retained under `licenses/gui`
and `sources/gui`. See https://www.pycryptodome.org/src/license.

## Native Windows runtime

The matching native client has its own MinGW/MSYS2 Qt and Python runtime.
Its DLLs are separate from the GUI runtime. Component package versions and notices
are supplied in `licenses/native-runtime` and the native release inventory.
Matching copyleft package sources, packaging recipes and patches are in
`sources/native-runtime`, with their archive index and hashes.

MSYS2 source origin: https://repo.msys2.org/mingw/sources/

Public upstream scripts and dictionaries retain upstream attribution. Additional
client Python scripts can depend on optional modules or external tools; those
optional packages are not silently installed by the application.

## Inno Setup

The Windows installer is built with Inno Setup 7.1.1 by Jordan Russell and
Martijn Laan. Its unmodified setup/uninstall runtime remains separate from the
Proxmark Studio application and retains the Inno Setup License. The license is
included at `docs/licenses/inno-setup/LICENSE.txt`; the reviewed compiler version,
official download URL, and hashes are recorded in `installer/toolchain.json`.

Upstream source: https://github.com/jrsoftware/issrc/tree/is-7_1_1

## Source use

Source archives contain the preferred source and supplied build recipes. They
are not a claim of bit-for-bit reproducibility of every upstream binary. Original
notices are preserved; see the per-component text for the applicable terms.
