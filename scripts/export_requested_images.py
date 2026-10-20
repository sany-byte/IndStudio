#!/usr/bin/env python3
"""Package only requested ingredient/form images, retaining the original CSV columns."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
import zipfile


def export(root, source_csv, destination, batch_id):
    with source_csv.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        fields, requests = reader.fieldnames, list(reader)
    destination.mkdir(parents=True, exist_ok=False)
    csv_path = destination / 'ingredients-to-generate.csv'
    source = sqlite3.connect((root / 'data/studio.sqlite3').resolve().as_uri() + '?mode=ro', uri=True, timeout=60)
    snapshot = sqlite3.connect(':memory:')
    source.backup(snapshot); source.close()
    snapshot.row_factory = sqlite3.Row
    images = root / 'data/images'
    rows, assets, missing, unavailable_transparent = [], {}, [], []

    def existing_asset(name):
        if not name:
            return False
        path = (images / name).resolve()
        return path.is_relative_to(images.resolve()) and path.is_file()

    for request in requests:
        form = snapshot.execute('SELECT * FROM forms WHERE source=? AND lower(form_en)=lower(?)', (request['ingredient'], request['form'])).fetchone()
        output = {**request, 'image': '', 'transparent_image': ''}
        if form:
            candidates = snapshot.execute("SELECT * FROM jobs WHERE form_id=? AND status='succeeded' AND review!='rejected' ORDER BY CASE WHEN id=? THEN 0 WHEN batch_id=? THEN 1 WHEN review='accepted' THEN 2 ELSE 3 END,created_at DESC,id", (form['id'], form['selected_job'], batch_id)).fetchall()
            job = next((j for j in candidates if existing_asset(j['asset'])), None)
            if job:
                output['image'] = job['asset']; assets[job['asset']] = images / job['asset']
                variants = snapshot.execute("SELECT * FROM image_variants WHERE generation_id=? AND kind='transparent' AND status='ready' ORDER BY created_at DESC", (job['id'],)).fetchall()
                variant = next((v for v in variants if existing_asset(v['asset'])), None)
                if variant:
                    output['transparent_image'] = variant['asset']; assets[variant['asset']] = images / variant['asset']
                else:
                    unavailable_transparent.append(dict(request))
        if not output['image']:
            missing.append(dict(request))
        rows.append(output)
    snapshot.close()
    if missing:
        (destination / 'missing.json').write_text(json.dumps(missing, ensure_ascii=False, indent=2))
        raise RuntimeError('Missing images for %d requested forms; see missing.json' % len(missing))
    with csv_path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + ['image', 'transparent_image'])
        writer.writeheader(); writer.writerows(rows)
    with csv_path.open(newline='', encoding='utf-8-sig') as handle:
        checked = list(csv.DictReader(handle))
    assert [{k: row[k] for k in fields} for row in checked] == requests
    archive = destination / 'ingredient-images.zip'
    temporary = destination / 'ingredient-images.zip.partial'
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        bundle.write(csv_path, csv_path.name)
        for name, path in assets.items():
            bundle.write(path, 'images/' + name)
    temporary.replace(archive)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert all('images/' + row['image'] in names for row in rows)
        assert all(not row['transparent_image'] or 'images/' + row['transparent_image'] in names for row in rows)
    result = {'requested_rows': len(rows), 'images': sum(bool(r['image']) for r in rows),
              'transparent_images': sum(bool(r['transparent_image']) for r in rows),
              'archive': str(archive), 'archive_bytes': archive.stat().st_size,
              'csv': str(csv_path), 'missing_transparent': unavailable_transparent}
    (destination / 'status.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='missing_transparent'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batch-id', required=True)
    args = parser.parse_args()
    export(args.root, args.csv, args.output, args.batch_id)
