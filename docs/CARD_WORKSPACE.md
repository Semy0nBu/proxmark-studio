# Card workspace

The **Cards** page has four tabs: **Overview**, **Changes**, **Editor**, and
**Operation result**. These views use the existing local library and immutable
revisions. Opening or comparing a snapshot does not contact a reader.

## Overview

The selected saved revision or unsaved editor copy determines coverage. Unknown
bytes remain unknown even when their stored placeholder is zero. Classic reads
with masked A/B fields require matching per-slot key evidence before those fields
count as known. Literal imports without a bitmap describe recorded values; they
do not prove a fresh physical read.

Classic Mini/1K/2K/4K snapshots with an explicitly declared model show a sector
map: blue for known data, amber for gaps, grey for unknown data, red for invalid
access bits. Clicking a sector opens the existing read-only memory map. LF
identifiers and structured records do not receive a full-memory percentage.
The primary action saves an unsaved copy, continues an incomplete Classic read,
or opens the existing write dialog. It never starts RF automatically.

## Changes

Choose **A → B** explicitly. Both saved revisions and the current working copy
are supported. The comparison distinguishes newly known bytes, differing known
values, and lost knowledge. Two unknown fillers are not a change. Classic data,
Key A, Key B, access bits, GPB and manufacturer block 0 have separate categories;
other supported flat records retain a generic comparison. Key values are not
shown in this table.

Double-click a change to open its position in the editor. When this requires a
different saved revision, the existing Save/Discard/Cancel prompt remains in
effect. Comparing snapshots never changes the selected revision or stored bytes.

## Interrupted HF writes

The result is bound to one reviewed plan, source, target observation and operation.
It distinguishes blocks confirmed by readback, blocks which may have been written,
and blocks whose write command was never sent. A write acknowledgement or a saved
v1 JSON report alone cannot establish a verified result. Delayed callbacks from
another operation cannot advance the current controller step.

**Read and compare** starts a new, read-only operation. It checks a fresh device
reply and matching card UID/profile, reads the effective planned bytes, checks the
card again, then completes the existing RF shutdown and connection check. Hidden
Classic keys use successful authentication evidence; an authentication failure
alone leaves them unknown. Trailer comparison uses the plan's preserved target
access conditions. UID/profile matching is an observation, not proof of unique
physical-card identity.

After a complete comparison with differences, **New write plan** opens a fresh
dialog with the preserved source, range and target UID. It requires a fresh backup,
review and explicit approval. The previous transaction is never resumed or replayed.
Old/new keys are only transient authentication candidates for the fresh preflight.
An unsuccessful cleanup retains the operation lock and publishes no new confirmation.

Native reports and a separate `write-postcheck.json` remain in the local session
folder. Postcheck summaries contain references and block addresses, not dump/key
values. The in-memory result panel is not restored as verified evidence after an
application restart. Firmware operations retain their separate lifecycle.

## Проверка изменений

Внедрён пакет второй редакции с дополнительными тестами самого приложения.
Проверки используют искусственные карты и подменённые вызовы клиента, включая
offscreen Qt. Они не подтверждают работу RF, USB или Blue Shark на реальной плате.

Запуск из корня проекта:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/audit_english_catalog.py
```
