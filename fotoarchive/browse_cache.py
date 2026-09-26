"""Disposable last browse layout on SSD; no photos or model results are copied."""
import json
import sqlite3
import uuid


FORMAT = 1
MAX_BYTES = 128 * 1024**2
TABLES = {
    'active_search': '''asset_id INTEGER PRIMARY KEY,file_version INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,verdict TEXT,result_json TEXT,face_score REAL,face_id TEXT,unit_id TEXT''',
    'search_moments': 'unit_id TEXT PRIMARY KEY,asset_id INTEGER NOT NULL',
    'stack_map': 'asset_id INTEGER PRIMARY KEY,stack_key TEXT NOT NULL',
    'stack_totals': 'stack_key TEXT PRIMARY KEY,total INTEGER',
    'grouped_matches': '''asset_id INTEGER PRIMARY KEY,ordinal INTEGER,key TEXT,
        first INTEGER,n INTEGER,choice INTEGER,total INTEGER,media_kind TEXT,width INTEGER,height INTEGER''',
    'display_rows': '''position INTEGER PRIMARY KEY,asset_id INTEGER UNIQUE,stack_key TEXT,
        stack_count INTEGER,stack_total INTEGER,stack_expanded INTEGER,media_kind TEXT,width INTEGER,height INTEGER''',
    'expanded_stacks': 'key TEXT PRIMARY KEY',
}
INDEXES = {
    'active_search': ['active_search_order ON active_search(ordinal)',
                      'active_search_verdict ON active_search(verdict,ordinal)'],
    'search_moments': ['search_moments_asset ON search_moments(asset_id)'],
    'stack_map': ['stack_map_key ON stack_map(stack_key)'],
    'grouped_matches': ['grouped_cover ON grouped_matches(choice,first)',
                        'grouped_key ON grouped_matches(key,choice)'],
    'display_rows': ['display_media ON display_rows(media_kind,position)'],
}


def normalized(value):
    return json.loads(json.dumps(value, sort_keys=True))


class BrowseCache:
    def __init__(self, cfg):
        self.folder = cfg.data_dir / 'ui-cache'
        self.folder.mkdir(exist_ok=True)
        self.manifest = self.folder / 'browse-current.json'
        self.saved_signature = None

    def load(self, catalog, key, revision):
        attached = False
        try:
            name = json.loads(self.manifest.read_text(encoding='utf-8'))['file']
            path = self.folder / name
            if path.name != name or not name.startswith('browse-') or not name.endswith('.sqlite3'):
                return None
            if path.stat().st_size > MAX_BYTES:
                return None
            catalog.db.execute('ATTACH DATABASE ? AS startup_cache', (path.resolve().as_uri()+'?mode=ro',))
            attached = True
            state = json.loads(catalog.db.execute('SELECT value FROM startup_cache.snapshot').fetchone()[0])
            if state['format'] != FORMAT or state['key'] != normalized(key) or state['revision'] != normalized(revision):
                raise ValueError('Catalogue layout changed')
            for table in TABLES if state['presentation'] else ('active_search', 'search_moments'):
                catalog.db.execute(f'SELECT * FROM startup_cache.{table} LIMIT 0')
            self.saved_signature = json.dumps(state, sort_keys=True)
            return state
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            if attached:
                catalog.db.execute('DETACH DATABASE startup_cache')
            return None

    def save(self, session, key, revision, obsolete):
        presentation = session.presentation
        state = dict(format=FORMAT, key=key, revision=revision, total=session.total,
            metadata_count=session.metadata_count, media_totals=getattr(session, 'media_totals', {}),
            presentation=dict(options=presentation.options, signature=presentation.signature,
                matches_signature=presentation.matches_signature, media_totals=presentation.media_totals)
                if presentation else None)
        signature = json.dumps(state, sort_keys=True)
        if signature == self.saved_signature or obsolete():
            return
        path = self.folder / ('browse-'+uuid.uuid4().hex+'.sqlite3')
        db = None
        installed = False
        try:
            db = sqlite3.connect(path)
            db.execute('PRAGMA synchronous=OFF')
            db.set_progress_handler(lambda: int(obsolete()), 10000)
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={MAX_BYTES // page_size}')
            tables = TABLES if presentation else ('active_search', 'search_moments')
            with db:
                for table in tables:
                    db.execute(f'CREATE TABLE {table}({TABLES[table]})')
                    rows = session.catalog.db.execute(f'SELECT * FROM {table}')
                    placeholders = ','.join('?' for _ in rows.description)
                    while batch := rows.fetchmany(2048):
                        if obsolete():
                            return
                        db.executemany(f'INSERT INTO {table} VALUES({placeholders})', batch)
                    for definition in INDEXES.get(table, []):
                        db.execute('CREATE INDEX '+definition)
                db.execute('CREATE TABLE snapshot(value TEXT)')
                db.execute('INSERT INTO snapshot VALUES(?)', (signature,))
            db.close()
            db = None
            if obsolete() or path.stat().st_size > MAX_BYTES:
                return
            temporary = self.manifest.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(file=path.name)), encoding='utf-8')
            temporary.replace(self.manifest)
            installed = True
            self.saved_signature = signature
            # Keep only the newest cache. An older one may still be attached by
            # a reader on Windows; it is removed on the next successful save.
            for old in self.folder.glob('browse-*.sqlite3'):
                if old != path:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except (OSError, sqlite3.Error):
            pass  # Optional acceleration must never fail a catalogue request.
        finally:
            if db:
                db.close()
            if not installed:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
