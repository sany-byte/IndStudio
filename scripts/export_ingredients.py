#!/usr/bin/env python3
"""Consistent SQLite snapshot plus ingredient catalog and selected image assets.

No generation, selection changes or background-removal jobs are performed.
Run: python3 scripts/export_ingredients.py --root /opt/ingredient-studio
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def export(root, output, source_directory):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run = output / ('ingredients-' + stamp)
    run.mkdir(parents=True, exist_ok=False)
    status = run / 'status.json'
    write_json(status, {'status': 'running', 'started_at': stamp})
    try:
        package = run / 'ingredients'
        package.mkdir()
        source = sqlite3.connect((root / 'data/studio.sqlite3').resolve().as_uri() + '?mode=ro', uri=True, timeout=60)
        snapshot = sqlite3.connect(package / 'studio.sqlite3')
        try:
            source.backup(snapshot)
            snapshot.row_factory = sqlite3.Row
            tables = {}
            names = [r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for name in names:
                tables[name] = [dict(r) for r in snapshot.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
        finally:
            snapshot.close()
            source.close()
        write_json(package / 'database.json', tables)
        jobs = {j['id']: j for j in tables['jobs']}
        variants = tables.get('image_variants', [])
        ingredients = {}
        missing = []
        copied = {}

        def asset_path(asset, form_id, kind):
            if not asset:
                missing.append({'form_id': form_id, 'kind': kind, 'reason': 'asset_not_recorded'})
                return None
            original = (root / 'data/images' / asset).resolve()
            if not original.is_relative_to((root / 'data/images').resolve()):
                raise ValueError('Image asset escapes image directory')
            if not original.is_file():
                missing.append({'form_id': form_id, 'kind': kind, 'asset': asset, 'reason': 'file_missing'})
                return None
            relative = Path('images') / asset
            target = package / relative
            if str(relative) not in copied:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
                digest = hashlib.sha256()
                with target.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                copied[str(relative)] = {'sha256': digest.hexdigest(), 'bytes': target.stat().st_size}
            return relative.as_posix()

        for raw in sorted(tables['forms'], key=lambda f: (f['source'], f['id'])):
            form = dict(raw)
            for field in ('blocks', 'requirements_ru', 'requirements_en'):
                if isinstance(form.get(field), str):
                    try:
                        form[field] = json.loads(form[field])
                    except ValueError:
                        pass
            selected = jobs.get(form.get('selected_job'))
            form['selected_generation'] = selected
            form['selected_image'] = None
            form['transparent_image'] = None
            form['transparent_variant'] = None
            if selected:
                form['selected_image'] = asset_path(selected.get('asset'), form['id'], 'selected')
                candidates = [v for v in variants if v['generation_id'] == selected['id'] and v['kind'] == 'transparent' and v['status'] == 'ready']
                candidates.sort(key=lambda v: v.get('created_at') or 0, reverse=True)
                variant = next((v for v in candidates if v.get('asset') and (root / 'data/images' / v['asset']).is_file()), None)
                form['transparent_variant'] = variant
                form['transparent_image'] = asset_path(variant.get('asset') if variant else None, form['id'], 'transparent')
            elif form.get('selected_job'):
                missing.append({'form_id': form['id'], 'kind': 'selected', 'reason': 'generation_not_found'})
            ingredient = ingredients.setdefault(form['source'], {'source': form['source'], 'name_ru': form['name_ru'], 'name_en': form['name_en'], 'forms': []})
            ingredient['forms'].append(form)
        write_json(package / 'ingredients.json', list(ingredients.values()))
        # The supplied site's ingredient string is the join key, not Studio's form ID.
        sources = {}
        (package / 'source').mkdir()
        for filename in ('ingredients.csv', 'ingredient_states.csv'):
            source_file = source_directory / filename
            shutil.copyfile(source_file, package / 'source' / filename)
            with source_file.open(newline='', encoding='utf-8-sig') as handle:
                reader = csv.DictReader(handle)
                sources[filename] = (reader.fieldnames, list(reader))
        source_keys = {r['ingredient'] for r in sources['ingredients.csv'][1]}
        rows = []
        for ingredient in ingredients.values():
            for form in ingredient['forms']:
                rows.append({'ingredient': ingredient['source'], 'in_source_csv': int(ingredient['source'] in source_keys),
                             'form_id': form['id'], 'form_ru': form['form_ru'], 'form_en': form['form_en'],
                             'generation_id': form.get('selected_job'), 'image_path': form['selected_image'],
                             'transparent_image_path': form['transparent_image'],
                             'parameters_json': json.dumps(form, ensure_ascii=False)})
        with (package / 'form_images.csv').open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
        site_db = sqlite3.connect(package / 'site_import.sqlite3')
        try:
            for filename, table in (('ingredients.csv', 'ingredients'), ('ingredient_states.csv', 'ingredient_states')):
                fields, records = sources[filename]
                quoted = ['"' + name.replace('"', '""') + '"' for name in fields]
                site_db.execute('CREATE TABLE ' + table + '(' + ','.join(name + ' TEXT' for name in quoted) + ')')
                site_db.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in fields) + ')',
                                    [[record[name] for name in fields] for record in records])
            site_db.execute('CREATE UNIQUE INDEX ingredients_key ON ingredients(ingredient)')
            site_db.execute('CREATE INDEX ingredient_states_key ON ingredient_states(ingredient,state)')
            site_db.execute('CREATE TABLE form_images(ingredient TEXT NOT NULL,in_source_csv INTEGER NOT NULL,form_id TEXT PRIMARY KEY,form_ru TEXT,form_en TEXT,generation_id TEXT,image_path TEXT,transparent_image_path TEXT,parameters_json TEXT NOT NULL)')
            site_db.executemany('INSERT INTO form_images VALUES(?,?,?,?,?,?,?,?,?)', [list(row.values()) for row in rows])
            site_db.execute('CREATE INDEX form_images_ingredient ON form_images(ingredient)')
            site_db.execute('CREATE VIEW selected_images AS SELECT * FROM form_images WHERE image_path IS NOT NULL')
            site_db.commit()
        finally:
            site_db.close()
        unmatched = sorted(set(ingredients) - source_keys)
        write_json(package / 'source_match_report.json', {'source_ingredient_count': len(source_keys),
                   'studio_ingredient_count': len(ingredients), 'studio_only_ingredients': unmatched,
                   'source_only_ingredients': sorted(source_keys - set(ingredients))})
        (package / 'IMPORT_RU.md').write_text('''# Импорт в базу сайта

`site_import.sqlite3` — база для импорта. `studio.sqlite3` — полный снимок внутренней базы Studio с историей.

Таблицы `ingredients` и `ingredient_states` дословно сохраняют поля приложенных CSV (значения TEXT).
`form_images` содержит ВСЕ визуальные формы, параметры и пути к выбранным файлам.
Связь с исходной базой: `ingredients.ingredient = form_images.ingredient` (точное английское имя).
Числовых ID сайта в CSV нет, поэтому они не выдумываются. Если сайт использует числовые ID,
получите их соединением по ingredient при импорте. `form_id` — постоянный ID визуальной формы Studio.
`generation_id` — ID именно выбранной картинки. Пути относительны корню этого архива.

```sql
SELECT i.ingredient, f.form_id, f.form_ru, f.generation_id,
       f.image_path, f.transparent_image_path, f.parameters_json
FROM ingredients i JOIN form_images f ON f.ingredient = i.ingredient;
```

Для импорта без SQLite используйте `source/*.csv` и `form_images.csv` с той же связью.
`parameters_json` содержит все поля формы, полный выбранный generation snapshot и сведения прозрачного варианта.
NULL/пустой image_path означает, что выбранной картинки пока нет; форма всё равно присутствует.
Пустой transparent_image_path означает, что версия без фона недоступна на момент снимка.
`selected_images` — представление только форм с выбранным файлом.

Загрузите содержимое `images/` в хранилище сайта, сохранив имена файлов. В БД сайта
сохраните ingredient_id сайта, form_id, generation_id и два URL, построенных из соответствующих путей.
Если в хранилище меняются имена, сохраните соответствие старого пути и нового URL при загрузке.

`ingredient_states` хранит пищевые состояния из исходной базы, а `form_images` — визуальные формы.
Автоматического соответствия raw/cooked каждой форме нет: его нельзя достоверно вывести из этих CSV.
`source_match_report.json` перечисляет отличия набора ингредиентов. `in_source_csv=0`
означает дополнительный ингредиент Studio, отсутствующий в приложенном ingredients.csv.
Такие строки не нужно импортировать как уже существующие ингредиенты сайта.
''', encoding='utf-8')
        manifest = {'snapshot_at_utc': stamp, 'ingredient_count': len(ingredients), 'form_count': len(tables['forms']),
                    'selected_form_count': sum(bool(f.get('selected_job')) for f in tables['forms']),
                    'files': copied, 'missing_assets': missing,
                    'notes': 'All database tables and prompt/generation history are included. Image files include only the final selected generation and its available transparent derivative. Forms without a selection are retained. Paths are relative to this directory. Credentials are not exported; no .env is included.'}
        write_json(package / 'manifest.json', manifest)
        archive = run / 'ingredients.tar.gz'
        temporary = run / 'ingredients.tar.gz.partial'
        with tarfile.open(temporary, 'w:gz') as bundle:
            bundle.add(package, arcname='ingredients')
        temporary.replace(archive)
        write_json(status, {'status': 'complete', 'archive': str(archive), 'archive_bytes': archive.stat().st_size,
                            'ingredient_count': len(ingredients), 'form_count': len(tables['forms']), 'missing_asset_count': len(missing),
                            'completed_at': datetime.now(timezone.utc).isoformat()})
        print(str(archive), flush=True)
    except Exception as exc:
        write_json(status, {'status': 'failed', 'error': str(exc), 'failed_at': datetime.now(timezone.utc).isoformat()})
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source-directory', type=Path, help='Directory with the original site CSV files (default: ROOT/inputs)')
    args = parser.parse_args()
    export(args.root.resolve(), (args.output or args.root / 'data/exports').resolve(), (args.source_directory or args.root / 'inputs').resolve())
