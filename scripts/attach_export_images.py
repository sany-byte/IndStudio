#!/usr/bin/env python3
"""Add only an images column to the original ingredient CSV using an existing export."""
import argparse
import csv
import json
from pathlib import Path
import shutil


def attach(source, exported, output):
    catalog = {row['source']: row for row in json.loads((exported / 'ingredients.json').read_text())}
    output.mkdir(parents=True, exist_ok=True)
    with (source / 'ingredients.csv').open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        fields, originals = reader.fieldnames, list(reader)
    if 'images' in fields:
        raise ValueError('The source CSV already has an images column')
    linked = 0
    with (output / 'ingredients.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + ['images'])
        writer.writeheader()
        for original in originals:
            images = []
            for form in catalog.get(original['ingredient'], {}).get('forms', []):
                image, transparent = form.get('selected_image'), form.get('transparent_image')
                if not image:
                    continue
                for asset in (image, transparent):
                    if asset and not (exported / asset).is_file():
                        raise FileNotFoundError(asset)
                images.append({'form': form['form_en'], 'form_ru': form['form_ru'],
                               'image': image, 'transparent_image': transparent})
            linked += len(images)
            writer.writerow({**original, 'images': json.dumps(images, ensure_ascii=False)})
    shutil.copyfile(source / 'ingredient_states.csv', output / 'ingredient_states.csv')
    with (output / 'ingredients.csv').open(newline='', encoding='utf-8-sig') as handle:
        result = list(csv.DictReader(handle))
    assert [{key: row[key] for key in fields} for row in result] == originals
    print(json.dumps({'ingredients': len(originals), 'linked_images': linked,
                      'original_fields_unchanged': True, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--exported', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    attach(args.source, args.exported, args.output)
