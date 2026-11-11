# Changelog / История изменений

## 0.3.0-preview.10 — 2026-11-11

- On Windows, Blue Shark checks the selected paired SPP service before a fresh
  native COM-port open. Existing connected sessions keep their current client.
- Bluetooth preparation runs in a separate process with a bounded deadline.
  Stop, timeout and close release that process and prevent a delayed command.
- The worker uses file-based results in the windowless EXE. Source launches own
  the real Python process directly, preserving cancellation in virtual environments.
- Service discovery does not pair devices, reset drivers or change board settings.
  Fresh Proxmark OS verification is still required before the link is connected;
  bounded recovery remains available for a real outage.

По-русски: готовность службы Blue Shark проверяется перед открытием COM-порта.
Подготовка отменяется и не блокирует интерфейс. Прошивка платы не меняется.

## 0.3.0-preview.9 — 2026-11-11

- Workbench interface with top navigation and a persistent connection / Stop bar.
  Language, Guided / Compact mode and library backup are grouped in Settings.
- The card editor opens on HEX, with address gutters and separate colors for
  manufacturer data, keys, access fields and unread bytes. Text remains optional.
- A sector inspector follows the cursor, shows recorded key availability without
  exposing values, and decodes known Classic access conditions. Unknown fields
  remain unknown; the inspector does not authenticate or send device commands.
- Existing profiles migrate their layout while preserving library records,
  revisions, key evidence and connection settings.
- README and English/Russian guides show the native Workbench interface using
  synthetic data. Reader firmware is unchanged in this update.

По-русски: интерфейс Workbench, HEX с адресами и цветами областей, инспектор
секторов и постоянная строка подключения. Данные библиотеки сохраняются.

## 0.3.0-preview.8 — 2026-11-11

- Collections and tags for library records, with combined search and HF/LF
  filters. Grouping changes preserve the selected revision and dump bytes;
  existing libraries and password backups retain cards, versions and keys.
- Named quick actions in the expert console: review up to 20 sequential commands,
  stop on error or cancellation, preserve SPIFFS completion fences. Concurrent
  editors cannot overwrite newer saves, and a backup blocks device startup.
- A selected-client catalog collected from offline help, bound to the exact EXE
  SHA256. Inspect added, removed and changed syntax; stale or incompatible expert
  forms are blocked. New command syntax remains help-only until supported.
- A reviewed diagnostic ZIP with allowlisted versions, states, hashes and numeric
  completion codes. No logs, commands, card IDs, dumps, keys or personal paths.
- Expanded HEX shares the working document, read-only safeguards and unknown-byte
  highlighting. Host-side help/report workers cancel cleanly when the app closes.
- English/Russian guides include troubleshooting, workflow screenshots and dated
  hardware coverage. These desktop changes do not replace reader firmware.

По-русски: коллекции и метки, избранные сценарии, каталог конкретного клиента,
обезличенный диагностический архив и большое окно HEX. Дампы и история версий
сохраняются; ошибочные и отменённые команды автоматически не повторяются.

## 0.3.0-preview.7 — 2026-11-11

- Updates preserve custom library/profile arguments in existing Desktop and
  Start Menu shortcuts for this installation, including Unicode paths.
- Malformed source metadata produces an explained write blocker instead of a
  type error or an assumed complete source.

- Guided and Compact interface modes, switched live from the sidebar and saved
  per profile. Optional explanations collapse in Compact; blockers, errors and
  partial/placeholder-write warnings remain visible. Both modes use the same
  operation parameters, reviewed plans, backup and readback checks.

- Source → blank → plan → write → verify panels across the existing HF/LF writers.
  The selected snapshot and observed target appear separately, with family,
  capacity, access, unknown-data and preserved-service-field explanations.
- Changed sources or write parameters invalidate approval. The prepared backup,
  target identity recheck and readback remain mandatory; identical data needs no write.
- Closing a write wizard cancels its owned operation and closes after completion.
  Errors retain the source and report; no failed write is automatically repeated.

По-русски: источник и заготовка показаны рядом, ограничения объясняются до записи.
Изменение параметров требует нового плана. Закрытие отменяет текущую операцию
и ждёт её завершения; успешный результат подтверждается чтением.

## 0.3.0-preview.6 — 2026-11-11

- Whole-profile library backup to one archive: cards, all revisions, partial-read
  metadata, MIFARE keys with provenance and timestamps, settings and profile files.
- Optional AES-256-GCM password protection with scrypt key derivation. Restore
  verifies the archive and database before creating a separate profile, with no
  merge or overwrite. Background workers support cancellation and normal exit.
- Library profile selection from the sidebar, with English and Russian UI.

## 0.3.0-preview.5 — 2026-11-10

Changes since the public `preview.1`:

- Card overview with revision selection, known-byte coverage, gaps and Classic sectors.
- Revision comparison separates new data, changed values and lost information;
  Classic keys, access conditions and block 0 have their own categories.
- Interrupted HF writes have an operation-bound result and a readback/comparison
  path to a new reviewed plan. Unknown A/B values cannot enter key-change plans.
- Stop no longer loses the current step's completion or leaves the idle window
  stuck in Running. Repeated Stop and failed cleanup retain a usable exit path.
- Bounded USB/Blue Shark idle reconnection without replaying card operations;
  USB port renumbering follows the confirmed device serial number.
- Large flasher logs no longer kill the client. Transition-only port errors are
  distinguished from write failures, while mandatory readback checks remain.
- Short button animations, reduced-motion support and interruptible smooth scrolling.

По-русски: добавлены паспорт карты, содержательное сравнение версий и результат
прерванной HF-записи. Неизвестные ключи A/B исключены из плана смены ключей.
Исправлено зависание после Stop, доработаны переподключение USB/Blue Shark,
журналы прошивальщика и плавность интерфейса. Повторная запись требует нового
плана и подтверждения; операции с картами автоматически не повторяются.

## 0.3.0-preview.4 — локальная сборка

- Ограниченный журнал больше не прерывает процесс прошивки; ошибки сохраняются
  даже при сокращении большого вывода клиента.
- Временные ошибки COM при переходе в загрузчик отделены от ошибок записи.
  После записи по-прежнему обязательны проверки ELF, загрузчика, SPI и версии OS.
- Восстановление соединения в простое: до трёх проверок версии без повтора RF-операций.
  Смена USB COM отслеживается только по ранее подтверждённому серийному номеру.
- Устранено влияние запоздалых сигналов старой попытки установки на новую.
- Короткие анимации кнопок и системный режим уменьшения анимаций; нажатия
  выполняются сразу, ручная прокрутка прерывает плавную.

## 0.3.0-preview.3 — локальная сборка

- Ошибка открытия COM-порта больше не запускает цикл проверки остановки RF.
- После неудачной проверки остановки можно закрыть GUI; неопределённое состояние
  платы сохраняется в статусе выхода и не отмечается успешной остановкой.
- Проверка несохранённых правок и запрет закрытия при активной прошивке сохранены.

## 0.3.0-preview.2 — локальная сборка

- Защита записи Classic: маскированные и неизвестные A/B не попадают в план
  смены ключей, в том числе при разрешённой записи нулевых заполнителей.
- Паспорт выбранной версии карты, нормализованная карта известных байтов и
  отдельное представление LF-идентификаторов.
- Сравнение версий с разделением новых данных, изменений и потери сведений;
  отдельные категории данных, ключей, прав доступа и блока 0 для Classic.
- Результат прерванной HF-записи с привязкой к операции и безопасным
  перечитыванием. Повторная запись требует нового плана и подтверждения.
- Проверки синтетических карт и настоящих Qt-контроллеров без RF.

## 0.3.0-preview.1

Первый выпуск для установки на Windows 10 (1903+) и Windows 11 x64.

- Установщик со всеми компонентами и ярлыками. Также доступна portable-версия.
- Рабочие области HF и LF, чтение карт и автоматическое сохранение в библиотеку.
- Полные и частичные дампы, версии записей, HEX-редактор и просмотр кодировок.
- Библиотека MIFARE-ключей с источниками, датами и импортом из Flipper Zero.
- Запись HF-карт с просмотром плана, резервной копией и проверкой результата.
- Подключение по USB и Blue Shark, отмена операций и восстановление соединения.
- Измерения антенн, установка прошивки и инструменты клиента RRG/Iceman.
- Русский и английский интерфейс.

Установка, повторная установка, ярлыки и удаление проверены на рабочем ПК.
Удаление сохраняет пользовательскую библиотеку. Выпуск пока без цифровой
подписи; проверка на отдельной чистой Windows ещё предстоит.

Клиент и прошивки включены в комплект вместе с лицензиями и соответствующими
исходниками. Прошивка устанавливается отдельным действием в приложении.
