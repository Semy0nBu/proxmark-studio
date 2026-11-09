# Native client checks without hardware

These harnesses compile and execute production client/firmware helpers from the
selected source tree. UART, clocks and bulk packet delivery are deterministic fixtures;
no COM port or RFID device is opened.

```powershell
$bulkClientSource = 'D:\Tools\Proxmark3-RDV4\v4.23346-gui-blueshark-bulk-client'
.\.venv\Scripts\python.exe tools/native-client-tests/bulk_tests.py `
  --client-source $bulkClientSource `
  --output outputs/native-bulk-tests

.\.venv\Scripts\python.exe tools/native-client-tests/frame_tests.py `
  --client-source $bulkClientSource `
  --output outputs/native-frame-tests

.\.venv\Scripts\python.exe tools/native-client-tests/uart_wait_test.py `
  --client "$bulkClientSource/client" `
  --out outputs/native-uart-tests
```

The default compiler is the installed ProxSpace UCRT64 GCC. `--compiler` accepts
another compiler path. Outputs contain compiler logs, executables, results and
hashes of the exact production/test sources.
Select the complete patched source tree. The plain upstream reference tree
does not contain the additional framed RX/TX/Win32 helper headers until the
corresponding client patches have been applied.

`test_bulk_download.c` directly includes `comms_bulk_download.h`, used by the
native client. It runs in unoptimized and optimized builds with warnings,
including exhaustive enum switches, treated as errors. It checks:

- Native NG offsets and terminal metadata, unaligned buffers, truncated headers,
  wrong NG/OLD mode and refusal of anonymous ACK.
- 512-byte segmented requests and zero-segment-size single-request mode (the
  engine mode used by USB), including 30,000 bytes and partial final segments.
- Gaps, duplicate/overlapping/out-of-order chunks, integer bounds, zero/huge
  lengths, missing payloads, nonzero status and incorrect terminal byte counts.
- Missing terminal or CRC-discarded/missing chunk, with no later request queued.
- A single monotonic deadline across all segments and ignored/WTX packets;
  finite timeout zero and explicit unlimited deadline are distinct.
- Cancellation/disconnection before or during transfer, between segments and
  before final commit; request-send failures and injected allocation failure.
- Unchanged caller bytes on every failure, canaries around destination and
  actual allocated staging memory, and commit only after full success.

The engine harness does **not** calculate CRC or replace the native receiver's
CRC check. A discarded corrupt frame is tested at the bulk layer as missing
coverage. The existing frame-reader and TX lifecycle tests cover actual shared
framing helpers separately. Real transport selection in `comms.c`, UART driver
behavior, firmware compatibility and RF operation still require an actual
client build and separately recorded hardware checks; a passing harness is not
proof that every USB/Bluetooth device works.

## USB session reset, Windows I/O and startup handshake

Select the complete USB candidate source tree, including `common_arm/usb`:

```powershell
$usbClientSource = 'D:\Tools\Proxmark3-RDV4\v4.23346-gui-usb-stable-client'
.\.venv\Scripts\python.exe tools/native-client-tests/run_usb_reset_tests.py `
  --client-source $usbClientSource `
  --output outputs/native-usb-reset-tests

.\.venv\Scripts\python.exe tools/native-client-tests/usb_uart_tests.py `
  --client-source $usbClientSource `
  --output outputs/native-usb-uart-tests

.\.venv\Scripts\python.exe tools/native-client-tests/handshake_tests.py `
  --client-source $usbClientSource `
  --output outputs/native-handshake-tests
```

The reset runner inserts exact functions and control-request bodies from the
selected firmware into `production_usb_reset_test.c`'s host fixture, and compiles
the selected production `usb_read_ng.c`. The generated C file, build/test logs,
executables and source hashes go only to `--output`; the firmware tree is not
modified. Cases cover cached command bytes, interrupted/reset generations, OUT
bank/cache alignment, configuration/clear-halt/detach paths and normal reads
across 64-byte packet boundaries. Register fixtures do not emulate silicon bank
timing, and these tests do not install firmware or prove real hot-replug.

The Windows I/O runner executes the production serial helpers with mocked Win32
APIs, including partial reads/writes and timeout changes without a runtime RX
purge. The handshake runner exercises the production fresh-PING validator and
checks its binding to startup code. These tests neither open a COM port nor
replay a device/RF command. Existing historical reports under `outputs/usb-live`
remain separate from newly selected output directories.
