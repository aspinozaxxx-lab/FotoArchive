"""Reconcile a completely read source with SQLite; originals are never modified."""
import os
from pathlib import Path
import stat
import time

from .config import SUPPORTED
from .media_types import format_key


class LibraryCheck:
    def __init__(self, catalog, includes):
        self.catalog, self.db = catalog, catalog.db
        self.root = catalog.cfg.root.resolve()
        self.folders = [(self.root / item).resolve() for item in includes]
        if any(not folder.is_relative_to(self.root) for folder in self.folders):
            raise ValueError('Папка находится вне источника')
        self.started = time.time()
        self.state = dict(phase='checking', checked=0, added=0, changed=0,
                          restored=0, unchanged=0, removed=0, folder='')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS library_seen(asset_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS library_missing(asset_id INTEGER PRIMARY KEY);
            DELETE FROM library_seen;
            DELETE FROM library_missing;
        ''')
        self.seen = []

    def available(self):
        for folder in [self.root, *self.folders]:
            # Opening a directory distinguishes an empty folder from access loss.
            with os.scandir(folder) as entries:
                next(entries, None)

    def record(self, path):
        key = os.path.normcase(str(path.resolve()))
        old = self.db.execute('SELECT id,present FROM assets WHERE path_key=?', (key,)).fetchone()
        asset_id, changed = self.catalog.register(path)
        category = ('added' if old is None else 'changed' if changed else
                    'restored' if not old['present'] else 'unchanged')
        self.state[category] += 1
        self.state['checked'] += 1
        self.state['folder'] = path.parent.relative_to(self.root).as_posix()
        self.seen.append((asset_id,))
        return asset_id, changed

    def flush(self):
        if self.seen:
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO library_seen VALUES(?)', self.seen)
            self.seen.clear()

    def status(self):
        return self.state | {'seconds': round(time.time() - self.started, 1)}

    def finish(self):
        """Bounded verification, then one atomic catalogue/outbox transaction.

        Nothing is hidden until the entire enumeration and absence verification
        succeed. A restart discards this scratch list and replays the saved scan.
        """
        self.flush()
        self.state['phase'] = 'reconciling'
        self.available()
        prefixes = [os.path.normcase(str(folder)) + os.sep for folder in self.folders]
        where = ' OR '.join('substr(a.path_key,1,?)=?' for _ in prefixes) or '0'
        params = [value for prefix in prefixes for value in (len(prefix), prefix)]
        last_id = 0
        while True:
            rows = self.db.execute(f'''SELECT a.id,a.path FROM assets a
                LEFT JOIN library_seen s ON s.asset_id=a.id
                WHERE a.source_id=? AND a.present=1 AND a.id>? AND s.asset_id IS NULL
                AND ({where}) ORDER BY a.id LIMIT 128''',
                [self.catalog.source_id, last_id, *params]).fetchall()
            if not rows:
                break
            missing = []
            for row in rows:
                path = Path(row['path'])
                try:
                    info = path.stat()
                except (FileNotFoundError, NotADirectoryError):
                    missing.append((row['id'],))
                else:
                    if stat.S_ISREG(info.st_mode) and format_key(path) in SUPPORTED:
                        # A file may have reappeared after its folder was visited.
                        self.record(path)
                    else:
                        missing.append((row['id'],))
            self.available()
            self.flush()
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO library_missing VALUES(?)', missing)
            last_id = rows[-1]['id']
            yield
        self.available()
        with self.db:
            self.state['removed'] = self.db.execute('''SELECT count(*) FROM assets
                WHERE present=1 AND id IN (SELECT asset_id FROM library_missing)''').fetchone()[0]
            self.db.execute('''INSERT INTO outbox(unit_id,revision)
                SELECT u.id,a.version FROM units u JOIN assets a ON a.id=u.asset_id
                JOIN library_missing m ON m.asset_id=a.id WHERE a.present=1
                ON CONFLICT(unit_id) DO UPDATE SET revision=outbox.revision+1''')
            self.db.execute('''UPDATE assets SET present=0,updated_at=?
                WHERE id IN (SELECT asset_id FROM library_missing)''', (time.time(),))
            # Preserve successful analysis for files that later return unchanged.
            self.db.execute("""UPDATE jobs SET status='pending' WHERE status='running'
                AND asset_id IN (SELECT asset_id FROM library_missing)""")
            self.db.execute("""UPDATE unit_jobs SET status='pending' WHERE status='running'
                AND unit_id IN (SELECT id FROM units WHERE asset_id IN
                    (SELECT asset_id FROM library_missing))""")
        self.state['phase'] = 'complete'
