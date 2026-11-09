"""Audit English message coverage and formatting without loading the GUI or devices."""
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
from string import Formatter


ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile('[А-Яа-яЁё]')


def fields(text):
    return Counter((name, specification, conversion)
                   for _, name, specification, conversion in Formatter().parse(text)
                   if name is not None)


def audit(root):
    catalog_path = root / 'pm3gui/locales/en.json'
    duplicates = []
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'), object_pairs_hook=object_pairs)
    errors = [{'kind': 'duplicate-catalog-key', 'source': key} for key in duplicates]
    for source, target in catalog.items():
        if not isinstance(target, str) or not target.strip():
            errors.append({'kind': 'empty-or-invalid-target', 'source': source})
            continue
        if CYRILLIC.search(target):
            errors.append({'kind': 'cyrillic-english-target', 'source': source, 'target': target})
        try:
            if fields(source) != fields(target):
                errors.append({'kind': 'format-fields-differ', 'source': source, 'target': target})
        except ValueError as error:
            errors.append({'kind': 'invalid-format-string', 'source': source, 'error': str(error)})
    explicit, dynamic, bilingual = set(), 0, 0
    paths = [*root.joinpath('pm3gui').glob('*.py'), root / 'run_gui.py']
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
            if name == 'tr' and node.args:
                source = node.args[0]
                if isinstance(source, ast.Constant) and isinstance(source.value, str):
                    explicit.add(source.value)
                    if CYRILLIC.search(source.value) and source.value not in catalog:
                        errors.append({'kind': 'missing-explicit-message', 'file': str(path.relative_to(root)),
                                       'line': node.lineno, 'source': source.value})
                else:
                    dynamic += 1
            start = 1 if name == 'button' else 0
            if name in {'say', 'label', 'classic_pane', 'button'} and len(node.args) > start + 1:
                ru, en = node.args[start:start + 2]
                if (isinstance(ru, ast.Constant) and isinstance(ru.value, str)
                        and CYRILLIC.search(ru.value) and isinstance(en, ast.Constant)
                        and isinstance(en.value, str)):
                    bilingual += 1
                    if CYRILLIC.search(en.value):
                        errors.append({'kind': 'cyrillic-bilingual-english', 'file': str(path.relative_to(root)),
                                       'line': node.lineno, 'target': en.value})
    return {'passed': not errors, 'catalog_entries': len(catalog), 'explicit_messages': len(explicit),
            'dynamic_translation_calls': dynamic, 'bilingual_pairs_checked': bilingual, 'errors': errors,
            'limits': ['Static audit; runtime widget and persisted-metadata tests are separate.',
                       'User-authored text, filenames, card payloads and native CLI output are not translated.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = audit(args.root.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
