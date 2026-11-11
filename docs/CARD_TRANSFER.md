# Source → blank → write

The one-card writers share a source/blank panel and five visible steps:
**Source → Blank → Plan → Write → Verify**. Source selection never accesses a reader.

The source card shows the selected snapshot, family, recorded identity and known
data. The blank starts as unchecked. A saved source UID is never presented as a
fresh target observation. On a small window, the two cards stack vertically.

## Interface modes

Choose **Guided** or **Compact** in **Settings → Interface mode**. The preference applies immediately
to open views and new dialogs, and is saved per library profile. Guided is the
default and shows steps and explanations inline. Compact keeps the card pair,
short statuses and the plan visible; **Compatibility and limitations** and
**Explanation** reveal optional detail. It applies to the writers and explanatory
sections of the library, laboratory, antennas, firmware and backup screens.

Mode changes do not change a source, keys, operation parameters, approval or
device ownership. Blockers, failures and partial/placeholder-write warnings stay
visible. Target checks, backup, explicit approval and readback are the same in
both modes. Switching modes never starts a device command.

## HF data writing

Use **Auto-write…** for MIFARE Classic Mini/1K/2K/4K and supported Ultralight/NTAG
user memory. Select a library revision, editor snapshot, file or saved write
scenario. Replace the physical source with one writable target on the HF antenna.
Select **Check card** to read its identity, geometry, access rights and backup.

The compatibility panel explains family/size conflicts, unknown data, preserved
UID and service regions, and backup readiness. A matching UID or memory size alone
does not enable writing. Classic requires matching geometry; the wizard does not
silently shrink a 4K image into 1K or extend a smaller image. NTAG model and memory
length must match the selected writer's validated profile.

Normal data writing preserves the target UID and access conditions. It does not
promise a complete physical clone. A/B changes are a separate explicit option and
require known source values plus successful target access checks. Unknown keys
never become zero keys. Unread user blocks are skipped unless placeholder writing
is explicitly selected; that option can overwrite target data with stored fillers.

After a backup, review the actual before/after rows and approve the plan. The
destination is checked again and changed data is read back. An acknowledgement
alone is insufficient. Identical selected data produces a no-write result.
**Report and backup files…** opens the local transaction folder.

## Other existing family writers

**HF / LF family wizards…** opens the existing family operations. Their backup,
scope and recheck rules remain in force: ISO15693/iCLASS/LEGIC memory regions,
MIFARE Plus/FeliCa/Topaz user blocks, DESFire data files, Magic user blocks or
Gen1a identity settings, NDEF messages and supported LF identifiers.

The two cards show the effective data and the checked destination. A prepared
manual file/block payload is labelled as such; a structured JSON file's byte
length is not reported as chip capacity. NDEF is a message, LF records are
identifiers, and a checked block is not evidence that an entire card is empty.

The dedicated EM410x LF writer requires a positively detected, unprotected
T5577/T55x7. It backs up all eight page-0 blocks and checks the factory area.
An ordinary read-only EM410x card is not called a writable blank.

## Changes, cancellation and failure

Editing the source, revision, range, payload or other write settings invalidates
the previous approval. A new attempt prepares a fresh plan and target backup.
The source remains available after errors. Closing an active wizard requests
cancellation and waits for the operation to release its ownership. Written bytes
cannot be rolled back by Stop; no write is replayed automatically after a failure.

## Verification scope

Tests use independent synthetic card responders, real Qt windows offscreen and
the existing runner/controller boundaries. They cover HF/LF success, wrong family
and geometry, authentication failure, UID swaps, readback mismatch, changed
settings, unapproved writes and the supported family forms. They do not establish
new physical-card, RF or firmware compatibility. No new native write commands or
physical chip capabilities are introduced by this interface change.

## По-русски

В **Настройках → Режим интерфейса** выберите «Пошаговый» или «Компактный». Выбор применяется сразу
и сохраняется для текущего профиля. В компактном режиме длинные пояснения
открываются по запросу. Ошибки, запреты и предупреждения о неполном источнике
остаются видны. Параметры и обязательные проверки операции не меняются.

Источник и заготовка показаны отдельно. Выбор карты или файла не запускает плату.
Проверка заготовки читает сведения и резервные данные; после неё можно подтвердить
план. Совместимость объясняется для выбранной области: семейство, объём, права,
неизвестные данные и сохраняемые служебные поля.

Обычная HF-запись сохраняет UID заготовки. Для изменения UID нужен соответствующий
Magic-мастер. Меньшая память не заполняется обрезанным дампом автоматически.
У LF переносится идентификатор на подтверждённую T5577, а у NDEF — сообщение.
Изменение параметров требует нового плана. Успех появляется после проверки
чтением; подтверждение записи клиентом само по себе его не заменяет.
