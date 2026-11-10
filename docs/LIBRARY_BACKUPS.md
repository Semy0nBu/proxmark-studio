# Library backup / Резервная копия библиотеки

Available since **0.3.0-preview.6**, in both the installer and portable package.

## English

Select **Library…** in the sidebar. The current profile's path appears at the top.

1. In **Back up**, choose a new `.pm3backup` filename outside your profile.
   The archive includes saved cards, every revision, partial-read metadata,
   key values and their dates, sources and card associations, settings, imports,
   operation reports, logs and other regular files in that profile.
2. Optionally enable **Protect with a password**, enter the password twice,
   and select **Create archive**. The password is not saved. Without a password,
   the file is an ordinary ZIP archive containing unencrypted data.
3. In **Restore**, select the archive, enter its password if needed, and choose
   a **new profile folder**. Existing folders are never overwritten or merged.
   After validation, select **Open restored profile** to switch to that library.
   **Open profile…** also opens a previously restored profile later. A normal
   desktop shortcut continues to open the default profile; `--data-dir` can
   launch a chosen profile directly.

The application copies committed database data with SQLite's online backup API,
including committed WAL changes. Finish device operations first. Unsaved editor
changes are not included; save them before exporting. Application binaries,
installed firmware, files outside the profile and the device's memory are not
part of this archive. Restoring data never writes to a card or flashes a device.

Cancel or close the dialog to cancel an unfinished task. Closing the application
during a backup requests cancellation and waits for the worker to finish. A task
which has already published its complete archive or profile remains complete.
Checks run in the background; large profiles can take time. The supported limits
are 50,000 files and 16 GiB of uncompressed profile data. Links and special files
are rejected. Empty directories are not preserved.

### Password protection and format

Format v1 encrypts the entire ZIP stream, including its manifest and filenames,
using AES-256-GCM. A random 16-byte salt and 12-byte nonce are generated for each
archive. scrypt derives a 32-byte key with N=131072, r=8 and p=1. The format header
is authenticated; the GCM tag is 16 bytes. These parameters require approximately
128 MiB of memory while deriving the key. Passwords use UTF-8 without normalization.

The fixed header is `PM3BKUP` followed by byte `01`, bytes `11 08 01` (hex),
salt and nonce; ciphertext and the tag follow. A plain archive starts with ZIP's
standard signature. The manifest records format/version, creation time, app
version, relative paths, sizes and SHA-256 hashes. It contains no executable
restore instructions. Restoring checks authentication (when encrypted), paths,
ZIP structure/CRC, sizes, hashes, database structure, integrity and revision hashes
before publishing a new profile. Newer, unsupported formats are rejected.

There is no password reset. Keep the password separately from the archive.
Protection applies to the exported archive: active and restored profiles remain
unencrypted. Temporary plaintext is removed normally when the task ends; this
does not promise forensic erasure. A database snapshot is staged in system temp,
not on the archive destination. Decryption and restore use temporary staging
beside the new profile. Do not attach library backups to public bug reports.

## По-русски

Откройте **Библиотека…** внизу бокового меню. В окне указан путь текущего профиля.

1. На вкладке **Создать копию** выберите новый файл `.pm3backup` вне папки
   профиля. Сохраняются карты, все версии и сведения о непрочитанных областях,
   ключи с датами, источниками и привязками к картам, настройки, импорты,
   отчёты, журналы и остальные обычные файлы профиля.
2. При необходимости включите **Защитить паролем**, введите пароль дважды
   и нажмите **Создать архив**. Пароль не сохраняется. Без пароля получается
   обычный ZIP с открытыми данными.
3. На вкладке **Восстановить** выберите архив, укажите пароль, если он задан,
   и новую папку профиля. Существующие папки не заменяются и не объединяются.
   После проверки нажмите **Открыть восстановленный профиль**. Позже его можно
   выбрать через **Открыть профиль…**. Обычный ярлык запускает основной профиль;
   для прямого запуска отдельного профиля есть параметр `--data-dir`.

Перед копированием завершите операции устройства и сохраните правки редактора.
Несохранённые изменения не попадают в архив. Программа использует снимок SQLite,
учитывающий сохранённые изменения WAL. Файлы приложения, прошивка платы, память
устройства и файлы вне профиля в эту копию не входят. Восстановление не запускает
запись карт или прошивку. Можно отменить задачу или закрыть окно; приложение
дождётся завершения фонового потока. Уже полностью созданная копия сохраняется.

Пароль защищает весь архив, включая имена файлов: AES-256-GCM, scrypt и случайные
соль/nonce. При неверном пароле или повреждении зашифрованных данных новый профиль
не создаётся. Потерянный пароль восстановить нельзя. Текущая и восстановленная
библиотеки остаются незашифрованными; временные файлы удаляются обычным способом.
Лимиты: 50 000 файлов и 16 ГиБ распакованных данных. Ссылки и специальные файлы
не поддерживаются, пустые папки не сохраняются. Не публикуйте архив библиотеки.
