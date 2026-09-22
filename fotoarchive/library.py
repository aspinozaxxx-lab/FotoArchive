"""Small catalogue UI records and bounded reads independent of GPU work."""
from collections import OrderedDict, deque
from dataclasses import replace
import json
import math
import sqlite3
from threading import Condition, Thread
from uuid import uuid4


def initialize_library(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(assets)')}
    for name, definition in [('user_place', "TEXT NOT NULL DEFAULT ''"),
                             ('user_latitude', 'REAL'), ('user_longitude', 'REAL')]:
        if name not in columns:
            db.execute(f'ALTER TABLE assets ADD COLUMN {name} {definition}')
    db.executescript('''
      CREATE INDEX IF NOT EXISTS assets_stack_input ON assets(present,metadata_ready,media_kind,folder,camera,captured_at,id);
      CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY,name TEXT NOT NULL,
        examples TEXT NOT NULL,rejected TEXT NOT NULL,cover TEXT);
      CREATE TABLE IF NOT EXISTS stack_exclusions(asset_id INTEGER PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS stack_covers(stack_key TEXT PRIMARY KEY,asset_id INTEGER NOT NULL);
      INSERT OR IGNORE INTO app_state VALUES('library_revision','0');
      CREATE TRIGGER IF NOT EXISTS library_geo AFTER UPDATE OF latitude,longitude,geo_text,user_place,user_latitude,user_longitude ON assets BEGIN
        UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='library_revision';
      END;
    ''')
    db.commit()


class Library:
    def __init__(self, catalog):
        self.catalog, self.db = catalog, catalog.db

    def people(self):
        rows = []
        for record in self.db.execute('SELECT * FROM people ORDER BY name COLLATE NOCASE,id'):
            person = dict(record)
            person['examples'] = json.loads(person['examples'])
            person['rejected'] = json.loads(person['rejected'])
            person['faces'] = [info for key in person['examples'] if (info := self.catalog.face_info(key))]
            person['thumbnail'] = next((face['thumbnail'] for face in person['faces'] if face['id'] == person['cover']),
                                       person['faces'][0]['thumbnail'] if person['faces'] else '')
            rows.append(person)
        return rows

    def save_person(self, command, commit=True):
        name = str(command.get('name', '')).strip()[:120]
        if not name:
            raise ValueError('Укажите имя человека')
        examples = list(dict.fromkeys(command.get('examples', [])))
        if not examples or len(examples) > 128:
            raise ValueError('Добавьте от 1 до 128 примеров одного человека')
        rejected = sorted(set(command.get('rejected', [])) - set(examples))
        key = command.get('person_id') or str(uuid4())
        cover = command.get('cover')
        if cover not in examples:
            cover = examples[0]
        from contextlib import nullcontext
        with self.db if commit else nullcontext():
            self.db.execute('INSERT OR REPLACE INTO people VALUES(?,?,?,?,?)',
                            (key, name, json.dumps(examples), json.dumps(rejected), cover))
        return key

    def merge_people(self, ids):
        people = {p['id']: p for p in self.people()}
        chosen = [people[key] for key in dict.fromkeys(ids) if key in people]
        if len(chosen) < 2:
            raise ValueError('Выберите минимум двух людей для объединения')
        examples = list(dict.fromkeys(key for p in chosen for key in p['examples']))
        with self.db:
            key = self.save_person(dict(person_id=chosen[0]['id'], name=chosen[0]['name'],
                examples=examples, rejected=[key for p in chosen for key in p['rejected']], cover=chosen[0]['cover']),commit=False)
            self.db.executemany('DELETE FROM people WHERE id=?', [(p['id'],) for p in chosen[1:]])
        return key

    def split_person(self, command):
        person = next((p for p in self.people() if p['id']==command['person_id']),None)
        selected = list(dict.fromkeys(command['examples']))
        if not person or not selected or not set(selected)<set(person['examples']):
            raise ValueError('Выберите часть примеров существующего человека')
        with self.db:
            key = self.save_person(dict(name=command['name'],examples=selected),commit=False)
            self.save_person(dict(person_id=person['id'],name=person['name'],
                examples=[face for face in person['examples'] if face not in selected],
                rejected=person['rejected'],cover=person['cover']),commit=False)
        return key

    def navigation(self, filters):
        from .catalog import Filters
        # Folder totals describe sources; date density and media counters reflect
        # the ordinary filters but not the selected media tab/date handles.
        folders = [dict(row) for row in self.db.execute('''SELECT folder,count(*) count FROM assets
            WHERE present=1 AND metadata_ready=1 GROUP BY folder ORDER BY folder''')]
        base = replace(filters, media_kind='', date_from='', date_to='', unknown_date=False)
        where, params = base.expression()
        dates = [dict(row) for row in self.db.execute(f'''SELECT substr(captured_at,1,7) month,count(*) count
            FROM assets WHERE {where} GROUP BY month ORDER BY month''', params)]
        where, params = replace(filters, media_kind='').expression()
        media = {row[0]: row[1] for row in self.db.execute(
            f'SELECT media_kind,count(*) FROM assets WHERE {where} GROUP BY media_kind', params)}
        return dict(folders=folders, dates=dates, media=media, people=self.people())

    def places(self, filters, viewport='', step=5, asset_sql=''):
        where, params = filters.expression()
        if asset_sql:
            where += ' AND id IN ('+asset_sql+')'
        labels_where,labels_params = where,list(params)
        coordinates = 'coalesce(user_latitude,latitude)', 'coalesce(user_longitude,longitude)'
        lat, lon = coordinates
        step = max(.0001,min(10,float(step)))
        if viewport:
            w,s,e,n = map(float,viewport.split(','))
            where += f' AND {lat}>=? AND {lat}<=? AND {lon}>=? AND {lon}<=?'
            params += [s,n,w,e]
        # At most 2,592 grid cells. No full-coordinate array crosses into Qt.
        points = [dict(row) for row in self.db.execute(f'''SELECT avg({lat}) latitude,avg({lon}) longitude,
            count(*) count,sum(user_latitude IS NOT NULL) manual_count,
            sum(user_latitude IS NULL) metadata_count,min(id) asset_id,CAST(({lat}+90)/{step} AS INTEGER)*{step}-90 south,
            CAST(({lon}+180)/{step} AS INTEGER)*{step}-180 west FROM assets
            WHERE {where} AND {lat} IS NOT NULL AND {lon} IS NOT NULL
            GROUP BY CAST(({lat}+90)/{step} AS INTEGER),CAST(({lon}+180)/{step} AS INTEGER) LIMIT 4096''', params)]
        for point in points:
            point.update(north=min(90,point['south']+step),east=min(180,point['west']+step))
        labels = [dict(row) for row in self.db.execute(f'''SELECT
            CASE WHEN user_place<>'' THEN user_place ELSE geo_text END place,count(*) count
            FROM assets WHERE {labels_where} AND (geo_text<>'' OR user_place<>'') GROUP BY place ORDER BY count DESC LIMIT 500''', labels_params)]
        return dict(points=points, places=labels)

    def storyboard(self, asset_id, version):
        total = self.db.execute('''SELECT count(*) FROM units u JOIN unit_details d ON d.unit_id=u.id
            WHERE u.asset_id=? AND d.file_version=?''', (asset_id, version)).fetchone()[0]
        if not total:
            return []
        offsets = sorted({round(i * (total-1) / min(7, max(1, total-1))) for i in range(min(8, total))})
        return [dict(row) for offset in offsets if (row := self.db.execute('''SELECT u.id unit_id,u.timestamp_ms,d.thumbnail
            FROM units u JOIN unit_details d ON d.unit_id=u.id WHERE u.asset_id=? AND d.file_version=?
            ORDER BY u.timestamp_ms LIMIT 1 OFFSET ?''', (asset_id, version, offset)).fetchone())]

    def set_place(self, command):
        name = str(command.get('name', '')).strip()[:500]
        latitude, longitude = command.get('latitude'), command.get('longitude')
        if (latitude is None) != (longitude is None):
            raise ValueError('Нужны обе координаты')
        if latitude is not None and not (math.isfinite(latitude) and math.isfinite(longitude)
                                          and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError('Проверьте координаты')
        with self.db:
            self.db.execute('UPDATE assets SET user_place=?,user_latitude=?,user_longitude=? WHERE id=? AND version=?',
                (name, latitude, longitude, command['asset_id'], command['version']))

    def command(self, command):
        from .catalog import Filters
        action = command['action'].removeprefix('library_')
        payload = {}
        if action == 'navigation':
            payload = self.navigation(Filters(**command.get('filters', {})))
        elif action == 'places':
            payload = self.places(Filters(**command.get('filters', {})),command.get('viewport',''),command.get('step',5))
        elif action == 'storyboard':
            payload = dict(asset_id=command['asset_id'], version=command['version'],
                           frames=self.storyboard(command['asset_id'], command['version']))
        elif action == 'save_person':
            payload = dict(person_id=self.save_person(command), people=self.people())
        elif action == 'merge_people':
            payload = dict(person_id=self.merge_people(command['person_ids']), people=self.people())
        elif action == 'split_person':
            payload = dict(person_id=self.split_person(command),people=self.people())
        elif action == 'delete_person':
            with self.db:
                self.db.execute('DELETE FROM people WHERE id=?', (command['person_id'],))
            payload = dict(people=self.people())
        elif action == 'set_place':
            self.set_place(command)
        elif action == 'stack_cover':
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO stack_covers VALUES(?,?)', (command['stack_key'], command['asset_id']))
                self.db.execute("UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='browse_revision'")
        elif action == 'unstack':
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO stack_exclusions VALUES(?)', (command['asset_id'],))
                self.db.execute("UPDATE app_state SET value=CAST(value AS INTEGER)+1 WHERE key='browse_revision'")
        return dict(type=command['action'], serial=command.get('serial'), **payload)


class LibraryReader:
    def __init__(self, cfg, emit):
        self.cfg, self.emit = cfg, emit
        self.condition = Condition()
        self.reads, self.writes = OrderedDict(), deque()
        self.ready = self.stopped = False
        self.thread = Thread(target=self.run, name='library-navigation', daemon=True)
        self.thread.start()

    def enable(self):
        with self.condition:
            self.ready = True
            self.condition.notify()

    def send(self, command):
        with self.condition:
            if command['action'] in ('library_navigation', 'library_places', 'library_storyboard'):
                self.reads[command['action']] = command
            elif len(self.writes) < 64:
                self.writes.append(command)
            else:
                self.emit(dict(type='library_error', message='Дождитесь сохранения предыдущих изменений'))
            self.condition.notify()

    def close(self):
        with self.condition:
            self.stopped = True
            self.reads.clear()
            self.condition.notify()
        # UI edits are small transactions. Finish acknowledged saves at shutdown.
        self.thread.join(4)

    def run(self):
        from .catalog import Catalog
        catalog = None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or self.ready and (self.reads or self.writes))
                    if self.stopped and not self.writes:
                        return
                    command = self.writes.popleft() if self.writes else self.reads.popitem(last=False)[1]
                try:
                    if catalog is None:
                        catalog = Catalog.open_reader(self.cfg)
                        catalog.db.close()
                        catalog.db = sqlite3.connect(self.cfg.data_dir / 'catalog.sqlite3', timeout=3)
                        catalog.db.row_factory = sqlite3.Row
                        catalog.db.create_function('lower', 1, lambda s: s.casefold() if s else '', deterministic=True)
                        from .media_units import MediaUnits
                        catalog.media_units = MediaUnits(catalog, initialize=False)
                    self.emit(Library(catalog).command(command))
                except Exception as exc:
                    self.emit(dict(type='library_error', serial=command.get('serial'), message=str(exc)))
        finally:
            if catalog:
                catalog.close()
