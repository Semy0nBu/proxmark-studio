from pathlib import Path
import sys
import traceback

if __name__ == "__main__":
    try:
        from pm3gui.app import main
        result = main()
    except Exception as exc:
        from pm3gui.paths import startup_error_path
        error_path = startup_error_path()
        try:
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        from PySide6.QtWidgets import QApplication, QMessageBox
        application = QApplication.instance() or QApplication(sys.argv[:1])
        from pm3gui.i18n import tr
        QMessageBox.critical(None, tr('Proxmark Studio — ошибка запуска'), tr('{0}\n\nПодробности: {1}', str(exc), str(error_path)))
        result = 1
    raise SystemExit(result)
