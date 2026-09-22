"""Disposable, disk-backed grouping. Every source file remains in active_search."""
import json
import re
from functools import lru_cache


SORTS = {'newest': 'captured_at DESC,id DESC', 'oldest': 'captured_at ASC,id ASC',
         'name': 'filename COLLATE NOCASE,id', 'added': 'id DESC'}


@lru_cache(maxsize=8192)
def version_folder(folder):
    return re.sub(r'/(?:\[?developed\]?|web|raw|jpe?g|originals?)$', '', folder, flags=re.I).casefold()


def version_name(name):
    return re.sub(r'(?:\.[a-z0-9]{2,5})+$', '', name.casefold())


class Presentation:
    def __init__(self, session, options):
        self.session, self.db = session, session.catalog.db
        self.options = dict(options)
        self.signature = None
        self.matches_signature = None
        self.mapping_ready = False
        self.total = 0
        self.media_kind = ''
        self.media_totals = {}
        self.dirty = False

    @property
    def enabled(self):
        return bool(self.options.get('stacks'))

    def configure(self, options):
        if self.options == options:
            return
        if any(self.options.get(k) != options.get(k) for k in ('seconds', 'versions')):
            self.mapping_ready = False
        self.options = dict(options)
        self.signature = None

    def attach(self,path):
        self.db.execute('ATTACH DATABASE ? AS shared_stacks',(str(path),))
        self.map_path = path
        self.mapping_ready = True

    def mapping(self):
        if self.mapping_ready:
            return
        self.db.create_function('version_folder', 1, version_folder, deterministic=True)
        self.db.create_function('version_name', 1, version_name, deterministic=True)
        self.db.executescript('''DROP TABLE IF EXISTS temp.stack_map;
          CREATE TEMP TABLE stack_map(asset_id INTEGER PRIMARY KEY,stack_key TEXT NOT NULL);
          CREATE INDEX stack_map_key ON stack_map(stack_key);''')
        seconds = min(60, max(0, int(self.options.get('seconds', 2))))
        if seconds:
            # A run is confined to one camera and folder. Unknown dates and videos
            # are never inferred to be a burst. Group identity survives filtering.
            folder = 'version_folder(folder)' if self.options.get('versions', True) else 'folder'
            rows = self.db.execute(f'''SELECT id,{folder} dir,camera,unixepoch(captured_at) stamp
                FROM assets WHERE present=1 AND metadata_ready=1 AND media_kind='photo' AND captured_at IS NOT NULL
                AND id NOT IN (SELECT asset_id FROM stack_exclusions) ORDER BY dir,camera,captured_at,id''')
            # One sorted stream replaces three full window sorts. Only 2,048
            # pairs are buffered, even when a burst contains thousands of files.
            previous = None
            batch = []
            with self.db:
                for asset_id,directory,camera,stamp in rows:
                    if stamp is None:
                        continue
                    if previous is None or previous[:2] != (directory,camera) or stamp-previous[2] > seconds:
                        key = f'b:{asset_id}'
                    batch.append((asset_id,key))
                    previous = directory,camera,stamp
                    if len(batch)==2048:
                        self.db.executemany('INSERT INTO stack_map VALUES(?,?)',batch)
                        batch.clear()
                self.db.executemany('INSERT INTO stack_map VALUES(?,?)',batch)
        elif self.options.get('versions', True):
            self.db.execute('''INSERT INTO stack_map SELECT id,'v:'||min(id) OVER
                (PARTITION BY version_folder(folder),version_name(filename),captured_at,camera)
                FROM assets WHERE present=1 AND metadata_ready=1 AND media_kind='photo'
                AND captured_at IS NOT NULL AND id NOT IN (SELECT asset_id FROM stack_exclusions)''')
        self.mapping_ready = True

    def build(self, verdict=''):
        self.mapping()
        count, last, checked = self.db.execute('SELECT count(*),max(ordinal),count(verdict) FROM active_search').fetchone()
        expanded = sorted(set(self.options.get('expanded', [])))
        signature = count, last, checked, verdict, tuple(expanded)
        if self.signature == signature:
            self.total = self.media_totals.get(self.media_kind,0)
            return
        self.dirty = True
        self.db.executescript('''DROP TABLE IF EXISTS temp.display_rows;
            CREATE TEMP TABLE display_rows(position INTEGER PRIMARY KEY,asset_id INTEGER UNIQUE,
              stack_key TEXT,stack_count INTEGER,stack_total INTEGER,stack_expanded INTEGER,media_kind TEXT);
            CREATE INDEX display_media ON display_rows(media_kind,position);
            DROP TABLE IF EXISTS temp.expanded_stacks;
            CREATE TEMP TABLE expanded_stacks(key TEXT PRIMARY KEY);''')
        self.db.executemany('INSERT INTO expanded_stacks VALUES(?)', [(key,) for key in expanded])
        condition, params = '', []
        if verdict == 'pending':
            condition = ' AND s.verdict IS NULL'
        elif verdict:
            condition, params = ' AND s.verdict=?', [verdict]
        matches_signature = count,last,checked,verdict
        if matches_signature != self.matches_signature:
            self.db.executescript('''DROP TABLE IF EXISTS temp.grouped_matches;
              DROP TABLE IF EXISTS temp.stack_totals;
              CREATE TEMP TABLE stack_totals AS SELECT stack_key,count(*) total FROM stack_map GROUP BY stack_key;
              CREATE UNIQUE INDEX stack_totals_key ON stack_totals(stack_key);
              CREATE TEMP TABLE grouped_matches(asset_id INTEGER PRIMARY KEY,ordinal INTEGER,key TEXT,
                first INTEGER,n INTEGER,choice INTEGER,total INTEGER,media_kind TEXT);
              ''')
            self.db.execute('''INSERT INTO grouped_matches
          WITH candidates AS MATERIALIZED (
            SELECT s.asset_id,s.ordinal,a.media_kind,coalesce(m.stack_key,'a:'||s.asset_id) key,
              CASE WHEN c.asset_id=s.asset_id THEN 0 ELSE 1 END cover
            FROM active_search s JOIN assets a ON a.id=s.asset_id
            LEFT JOIN stack_map m ON m.asset_id=s.asset_id
            LEFT JOIN stack_covers c ON c.stack_key=m.stack_key
            WHERE a.present=1 AND s.file_version=a.version''' + condition + '''),
          grouped AS (SELECT key,min(ordinal) first,count(*) n,min(CASE WHEN cover=0 THEN ordinal END) cover_ordinal
            FROM candidates GROUP BY key)
          SELECT c.asset_id,c.ordinal,c.key,g.first,g.n,
            CASE WHEN c.ordinal=coalesce(g.cover_ordinal,g.first) THEN 1 ELSE c.ordinal+2 END,
            max(g.n,coalesce(t.total,g.n)),c.media_kind
          FROM candidates c JOIN grouped g ON g.key=c.key LEFT JOIN stack_totals t ON t.stack_key=c.key''', params)
            self.db.executescript('CREATE INDEX grouped_cover ON grouped_matches(choice,first); CREATE INDEX grouped_key ON grouped_matches(key,choice);')
            self.matches_signature = matches_signature
        self.db.execute('''INSERT INTO display_rows
          WITH visible AS (SELECT * FROM grouped_matches WHERE choice=1
            UNION ALL SELECT g.* FROM expanded_stacks e JOIN grouped_matches g ON g.key=e.key WHERE g.choice>1)
          SELECT row_number() OVER (ORDER BY first,choice)-1,asset_id,key,n,total,
            key IN (SELECT key FROM expanded_stacks),media_kind FROM visible''')
        self.media_totals = {row[0]:row[1] for row in self.db.execute('SELECT media_kind,count(*) FROM display_rows GROUP BY media_kind')}
        self.media_totals[''] = sum(self.media_totals.values())
        self.total = self.media_totals.get(self.media_kind,0)
        self.signature = signature
        self.dirty = False

    def page(self, offset, verdict, limit, expand):
        self.build(verdict)
        # Keep each interactive request bounded even for very long bursts.
        if expand and verdict in ('', 'pending'):
            for _ in range(4):
                if self.total >= offset + limit or self.session.exhausted:
                    break
                self.session.load_more()
                self.build(verdict)
        condition,params = ('d.media_kind=?',[self.media_kind]) if self.media_kind else ('1',[])
        rows = self.db.execute('''SELECT a.*,s.result_json,s.face_score,s.face_id face_match_id,s.unit_id,
              d.stack_key,d.stack_count,d.stack_total,d.stack_expanded
            FROM display_rows d CROSS JOIN assets a ON a.id=d.asset_id
            JOIN active_search s ON s.asset_id=d.asset_id
            WHERE '''+condition+' ORDER BY d.position LIMIT ? OFFSET ?',params+[limit,offset])
        items = []
        for row in rows:
            asset = self.session.catalog.media_units.asset_at(dict(row), row['unit_id'])
            if asset.pop('result_json', None):
                asset['verification'] = json.loads(row['result_json'])
            items.append(asset)
        result = dict(items=items, offset=offset, page_total=self.total, verdict=verdict,
                      has_more=not self.session.exhausted and verdict in ('', 'pending'),
                      material_total=self.session.counts()['candidates'])
        if self.session.mode == 'browse':
            result['metadata_count'] = self.session.metadata_count
        return result

    def restore(self, anchor):
        self.build()
        def position(asset_id):
            row = self.db.execute('SELECT position FROM display_rows WHERE asset_id=?', (asset_id,)).fetchone()
            if not row:
                row = self.db.execute('''SELECT min(position) FROM display_rows WHERE stack_key=
                    (SELECT stack_key FROM stack_map WHERE asset_id=?)''', (asset_id,)).fetchone()
            position = row[0] if row and row[0] is not None else None
            if position is not None and self.media_kind:
                position = self.db.execute('SELECT count(*) FROM display_rows WHERE media_kind=? AND position<?',(self.media_kind,position)).fetchone()[0]
            return position
        row = position(anchor.get('asset_id'))
        row = min(max(0, anchor.get('row', 0)), max(0, self.total-1)) if row is None else row
        selected = position(anchor.get('selected_id'))
        return dict(row=row, selected_row=row if selected is None else selected,
                    offset_y=anchor.get('offset_y', 0),
                    loaded_count=min(self.total, max(row+1, anchor.get('loaded_count', 0))))
