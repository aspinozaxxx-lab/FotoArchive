"""Cancellable, bounded catalogue reads independent of the GPU coordinator."""
from collections import OrderedDict
from dataclasses import asdict,replace
from threading import Condition, Thread
import time
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace


class StackMaps:
    """One small disposable map per interval, shared by the four result views."""
    def __init__(self,catalog):
        self.catalog = catalog
        self.maps = {}
        self.folder = catalog.cfg.data_dir/'ui-cache'
        self.folder.mkdir(exist_ok=True)

    def get(self,options):
        key = options.get('seconds',2),options.get('versions',True)
        if key not in self.maps:
            from .presentation import Presentation
            Presentation(SimpleNamespace(catalog=self.catalog),options).mapping()
            handle = tempfile.NamedTemporaryFile(prefix='stacks-',suffix='.sqlite3',dir=self.folder,delete=False)
            path = Path(handle.name)
            handle.close()
            db=sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE stack_map(asset_id INTEGER PRIMARY KEY,stack_key TEXT NOT NULL)')
                rows = self.catalog.db.execute('SELECT * FROM stack_map')
                with db:
                    while batch:=rows.fetchmany(4096):
                        db.executemany('INSERT INTO stack_map VALUES(?,?)',batch)
                    db.execute('CREATE INDEX stack_map_key ON stack_map(stack_key)')
                self.maps[key] = path
            finally:
                db.close()
                if key not in self.maps:
                    path.unlink(missing_ok=True)
        return self.maps[key]

    def clear(self):
        for path in self.maps.values():
            # Only files created by this object, never any catalogue or source.
            if path.resolve().parent==self.folder.resolve():
                path.unlink(missing_ok=True)
        self.maps.clear()

    def retain(self,paths):
        for key,path in list(self.maps.items()):
            if path not in paths:
                if path.resolve().parent==self.folder.resolve():
                    path.unlink(missing_ok=True)
                self.maps.pop(key)

from .catalog import Catalog, Filters
from .search_session import SearchSession


class BrowseViews:
    """At most four frozen lists on disk, invalidated by catalogue membership changes."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.control = Catalog.open_reader(cfg)
        self.revision = None
        self.views = OrderedDict()
        self.maps = StackMaps(self.control)

    def select(self, command, obsolete):
        revision = (self.control.state('browse_revision'), self.control.state('library_revision') if
                    any(command.get('filters', {}).get(k) for k in ('place', 'has_gps', 'geo_bounds')) else None)
        if revision != self.revision:
            self.clear()
            self.maps.clear()
            self.revision = revision
        filters = Filters(**command.get('filters', {}))
        options = dict(command.get('presentation', {}))
        requested_media = filters.media_kind
        # Photos and videos share one frozen membership/order and one grouping.
        # Switching the tab selects a covering index in the display table.
        if options.get('stacks'):
            filters = replace(filters,media_kind='')
        key = tuple(sorted(asdict(filters).items())) + tuple((k, options.get(k)) for k in ('sort', 'stacks', 'seconds', 'versions'))
        if key in self.views:
            catalog, session = self.views[key]
            self.views.move_to_end(key)
            session.request_id = command['id']
            catalog.db.set_progress_handler(lambda: int(obsolete()), 1000)
            session.configure_presentation(options)
            if session.presentation:
                session.presentation.media_kind = requested_media
                session.total = session.media_totals.get(requested_media,0)
            return catalog, session, True
        catalog = Catalog.open_reader(self.cfg)
        catalog.db.set_progress_handler(lambda: int(obsolete()), 1000)
        try:
            session = SearchSession(catalog, None, command['id'], filters, '', None, [], mode='browse', presentation=options)
            if session.presentation:
                self.control.db.set_progress_handler(lambda:int(obsolete()),1000)
                try:
                    session.presentation.attach(self.maps.get(options))
                finally:
                    self.control.db.set_progress_handler(None,0)
                session.media_totals = {row[0]:row[1] for row in catalog.db.execute('''SELECT a.media_kind,count(*)
                  FROM active_search s CROSS JOIN assets a ON a.id=s.asset_id GROUP BY a.media_kind''')}
                session.media_totals[''] = sum(session.media_totals.values())
                session.presentation.media_kind = requested_media
                session.total = session.media_totals.get(requested_media,0)
        except Exception:
            catalog.close()
            raise
        self.views[key] = (catalog, session)
        while len(self.views) > 4:
            _, (old, _) = self.views.popitem(last=False)
            old.close()
        self.maps.retain({getattr(session.presentation,'map_path',None) for _,session in self.views.values()})
        return catalog, session, False

    def clear(self):
        for catalog, _ in self.views.values():
            catalog.close()
        self.views.clear()

    def discard(self,catalog):
        for key,(candidate,_) in list(self.views.items()):
            if candidate is catalog:
                self.views.pop(key)
                catalog.close()
                break

    def close(self):
        self.clear()
        self.maps.clear()
        self.control.close()


class BrowseReader:
    def __init__(self, cfg, emit):
        self.cfg, self.emit = cfg, emit
        self.condition = Condition()
        self.ready = self.stopped = False
        self.generation = 0
        self.request = None
        self.pages = OrderedDict()
        self.thread = Thread(target=self.run, name='catalogue-browser', daemon=True)
        self.thread.start()

    def enable(self):
        with self.condition:
            self.ready = True
            self.condition.notify()

    def replace(self, command=None):
        with self.condition:
            self.generation += 1
            self.request = dict(command) if command else None
            self.pages.clear()
            self.condition.notify()

    def page(self, command):
        with self.condition:
            key = (command['id'], command.get('view', 0), command.get('offset', 0))
            self.pages[key] = dict(command)
            # Qt requests at most four pages. Keep a hard bound as well.
            while len(self.pages) > 8:
                self.pages.popitem(last=False)
            self.condition.notify()

    def close(self):
        with self.condition:
            self.stopped = True
            self.generation += 1
            self.request = None
            self.pages.clear()
            self.condition.notify()

    def obsolete(self, generation):
        return self.stopped or generation != self.generation

    def run(self):
        catalog = session = None
        views = None
        session_generation = -1
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or
                        (self.ready and (self.request is not None or self.pages)))
                    if self.stopped:
                        return
                    generation = self.generation
                    command, self.request = self.request, None
                    if command is None:
                        _, command = self.pages.popitem(last=False)
                started = time.perf_counter()
                try:
                    if command['action'] == 'browse':
                        catalog = session = None
                        if views is None:
                            views = BrowseViews(self.cfg)
                        catalog, session, cached = views.select(command, lambda: self.obsolete(generation))
                        session_generation = generation
                        restore = session.restore_position(command['refresh_anchor']) if command.get('refresh_anchor') else None
                        offset = restore['row'] // session.PAGE_SIZE * session.PAGE_SIZE if restore else 0
                        event = dict(type='results', id=command['id'], total=session.total,
                            conditions=[], semantic=False, mode='browse', examples=[], restore=restore,
                            cached=cached, **session.page(offset=offset), **session.counts())
                    else:
                        if session is None or session_generation != generation or command['id'] != session.request_id:
                            continue
                        catalog.db.set_progress_handler(lambda: int(self.obsolete(generation)), 1000)
                        event = dict(type='search_page', id=command['id'], view=command.get('view', 0),
                            **session.page(command.get('offset', 0)), **session.counts())
                    event['read_seconds'] = time.perf_counter() - started
                    if not self.obsolete(generation):
                        self.emit(event)
                except Exception as exc:
                    if catalog:
                        # A progress-handler interruption may roll back an INSERT
                        # into the temporary session. The next request rebuilds it.
                        catalog.db.set_progress_handler(None, 0)
                        catalog.db.rollback()
                        # A cancelled SELECT has not changed the cached tables.
                        # Rebuild only if interrupted during their construction.
                        damaged = session is None or getattr(session.presentation,'dirty',False)
                        if views and (damaged or not self.obsolete(generation)):
                            views.discard(catalog)
                        catalog = None
                    session = None
                    if not self.obsolete(generation):
                        self.emit(dict(type='error', id=command['id'], action=command['action'],
                            view=command.get('view'), offset=command.get('offset'), message=str(exc)))
                finally:
                    if catalog:
                        catalog.db.set_progress_handler(None, 0)
        finally:
            if views:
                views.close()
