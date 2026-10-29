"""Read-only upgrade inventory; does not initialize the app, access S3, or delete data.

Usage: python backend/scripts/audit_upgrade.py /path/to/app.sqlite3 > audit.json
Run against a consistent backup before and after migration. Cleanup candidates are
conservative registered-media candidates only, not authorization to delete objects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def audit(path: Path) -> dict:
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts, ids, hashes = {}, {}, {}
    referenced_ids, strings, malformed = set(), set(), []

    def collect(value):
        if isinstance(value, dict):
            for item in value.values(): collect(item)
        elif isinstance(value, list):
            for item in value: collect(item)
        elif isinstance(value, int):
            # Conservative: unrelated numeric JSON values may retain extra assets.
            referenced_ids.add(value)
        elif isinstance(value, str): strings.add(value)

    for table in tables:
        columns = {r['name'] for r in db.execute(f'PRAGMA table_info("{table}")')}
        rows = [dict(r) for r in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        counts[table] = len(rows)
        if 'id' in columns: ids[table] = [r['id'] for r in rows]
        hashes[table] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
        if table == 'media_assets': continue
        media_columns = {r['from'] for r in db.execute(f'PRAGMA foreign_key_list("{table}")') if r['table'] == 'media_assets'}
        media_columns |= {c for c in columns if c.endswith('_asset_id') or c == 'media_asset_id'}
        for row in rows:
            referenced_ids.update(row[c] for c in media_columns if row[c] is not None)
            for column, value in row.items():
                if not isinstance(value, str): continue
                if column.endswith('_json') and value:
                    try: collect(json.loads(value))
                    except ValueError: malformed.append({'table': table, 'id': row.get('id'), 'column': column})
                elif column in {'asset_dir', 'object_key', 'output_path'}:
                    strings.add(value)
    media = [dict(r) for r in db.execute('SELECT * FROM media_assets')]
    # Preserve keys embedded in prompts, path prefixes, and string-encoded identifiers.
    retained_text = '\n'.join(strings)
    directories = {s.rstrip('/') for s in strings if s.startswith('model-profiles/')}
    candidates = []
    for asset in media:
        key = asset['object_key']
        if (asset['id'] not in referenced_ids and str(asset['id']) not in strings and key not in retained_text
                and not any(key.startswith(directory + '/') for directory in directories)):
            candidates.append({'id': asset['id'], 'objectKey': key})
    evidence = {'publishedVersions': 0, 'versionsWithoutExecutableSnapshot': []}
    if 'generation_image_versions' in tables:
        for row in db.execute('SELECT id, image_id, request_snapshot_json FROM generation_image_versions'):
            evidence['publishedVersions'] += 1
            try: package = json.loads(row['request_snapshot_json'] or '{}')
            except ValueError: package = {}
            if not isinstance(package, dict) or not package.get('prompt') or not package.get('imageInputs'):
                evidence['versionsWithoutExecutableSnapshot'].append({'versionId': row['id'], 'imageId': row['image_id']})
    report = {
        'mode': 'read-only', 'database': str(path.resolve()),
        'integrity': [r[0] for r in db.execute('PRAGMA integrity_check')],
        'foreignKeyErrors': [tuple(r) for r in db.execute('PRAGMA foreign_key_check')],
        'migrations': [r[0] for r in db.execute('SELECT version FROM schema_migrations ORDER BY version')],
        'counts': counts, 'ids': ids, 'tableHashes': hashes, 'evidence': evidence,
        'malformedSnapshots': malformed,
        'registeredMediaCleanupCandidates': [] if malformed else candidates,
        'cleanupBlocked': bool(malformed),
        'limitations': ['No object-store inventory or existence check.', 'Candidates require a fresh reference check, backup, and explicit cleanup approval.',
                       'All result rounds, published versions and their execution evidence are retained, including superseded rounds.'],
    }
    db.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), ensure_ascii=False, indent=2))
