"""Four bounded search contexts keep exact history positions and checked results."""
from collections import OrderedDict
import json


class SearchHistory:
    def __init__(self):
        self.sessions = OrderedDict()

    @staticmethod
    def key(command):
        options = {k:v for k,v in command.get('presentation',{}).items() if k!='expanded'}
        return json.dumps({k:command.get(k) for k in ('action','query','filters','complex','asset_id',
            'unit_id','face_ids','excluded_faces','people','people_mode')}|{'presentation':options},sort_keys=True)

    def get(self,command):
        key = self.key(command)
        session = self.sessions.get(key) if command.get('refresh_anchor') else None
        if session:
            self.sessions.move_to_end(key)
            session.request_id = command['id']
            session.configure_presentation(command.get('presentation',{}))
        return session

    def put(self,command,session):
        key = self.key(command)
        old = self.sessions.pop(key,None)
        if old:
            old.catalog.close()
        self.sessions[key] = session
        while len(self.sessions)>4:
            _,old = self.sessions.popitem(last=False)
            old.catalog.close()

    def close(self):
        for session in self.sessions.values():
            session.catalog.close()
        self.sessions.clear()
