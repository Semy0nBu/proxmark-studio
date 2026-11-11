# Changelog / История изменений

## 0.3.0-preview.7 — 2026-11-11

- Updates preserve custom library/profile arguments in existing Desktop and
  Start Menu shortcuts for this installation.
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
