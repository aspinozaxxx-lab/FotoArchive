"""Face-grounded names. The caption model receives labels, never identities."""
from collections import Counter
import json
import re

import numpy as np
from PIL import ImageDraw, ImageFont

IDENTITY_THRESHOLD = .50
IDENTITY_MARGIN = .06
TRACK_THRESHOLD = .55
TOKEN = re.compile(r'\[\[P(\d+)\]\]')


def person_vectors(catalog, person_id):
    from .config import FACE_VERSION
    if not person_id:
        return []
    try:
        return [np.frombuffer(row[0],dtype=np.float32).tolist() for row in catalog.db.execute(
            'SELECT vector FROM person_templates WHERE person_id=? AND model_version=? ORDER BY face_id',(person_id,FACE_VERSION))]
    except __import__('sqlite3').OperationalError:
        return []  # Read-only probes of pre-upgrade catalogues.


def people_snapshot(catalog):
    people = []
    for row in catalog.db.execute('SELECT id,name,examples,rejected FROM people ORDER BY id'):
        examples = person_vectors(catalog,row['id']) or [vector.tolist() for key in json.loads(row['examples'])
                    if (vector := catalog.face_vector(key)) is not None]
        if examples:
            people.append(dict(id=row['id'], name=row['name'], vectors=examples,
                               rejected=json.loads(row['rejected'])))
    return people


def match_identity(vector, people, face_id=None):
    scores = []
    for person in people:
        if face_id in person.get('rejected', ()):
            continue
        if person.get('encoding')=='float16-base64':
            import base64
            vectors = np.frombuffer(base64.b64decode(person['vectors'],validate=True),dtype='<f2').astype(np.float32).reshape(-1,128)
            if not 0<len(vectors)<=16:
                raise ValueError('Слишком много примеров персоны')
        else:
            vectors = np.asarray(person['vectors'], dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != 128 or not np.isfinite(vectors).all():
            raise ValueError('Некорректные примеры именованной персоны')
        scores.append((float(np.max(vectors @ vector)), person['id']))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < IDENTITY_THRESHOLD:
        return None
    if len(scores) > 1 and scores[0][0]-scores[1][0] < IDENTITY_MARGIN:
        return None
    return scores[0][1]


def transport_people(people):
    """Small, diverse reference set; final identity validation uses all local examples."""
    import base64
    result=[]
    for person in people[:32]:
        vectors=np.asarray(person['vectors'],dtype=np.float32)
        chosen=[0]
        while len(chosen)<min(8,len(vectors)):
            similarity=(vectors@vectors[chosen].T).max(axis=1)
            similarity[chosen]=2
            chosen.append(int(similarity.argmin()))
        result.append(dict(id=person['id'],encoding='float16-base64',
            vectors=base64.b64encode(vectors[chosen].astype('<f2').tobytes()).decode()))
    return result


def label_people(images, face_batches, times, people):
    tracks, labelled = [], []
    for image, faces, timestamp in zip(images, face_batches, times):
        copy = image.copy()
        draw = ImageDraw.Draw(copy)
        scale=max(1.,min(image.width,image.height)/768)
        font=ImageFont.load_default(size=round(14*scale))
        used = set()
        for number, face in enumerate(faces):
            vector = np.asarray(face['vector'], dtype=np.float32)
            identity = match_identity(vector, people)
            candidates = []
            for index, track in enumerate(tracks):
                if index in used or (identity and track['person_id'] and identity != track['person_id']):
                    continue
                score = float(np.max(np.asarray(track['vectors']) @ vector))
                if identity and identity == track['person_id']:
                    score = 1.
                if score >= TRACK_THRESHOLD:
                    candidates.append((score, index))
            if candidates:
                _, index = max(candidates)
                track = tracks[index]
                if identity:
                    track['person_id'] = identity
            else:
                index = len(tracks)
                track = dict(token=f'P{index+1}', person_id=identity, vectors=[], evidence=[])
                tracks.append(track)
            used.add(index)
            if len(track['vectors']) < 12:
                track['vectors'].append(vector)
            track['evidence'].append(dict(timestamp_ms=timestamp, face_index=number))
            x1, y1, x2, y2 = [float(v) for v in face['box']]
            box = x1*image.width, y1*image.height, x2*image.width, y2*image.height
            draw.rectangle(box, outline='#38dfff', width=round(2*scale))
            draw.rectangle((box[0], max(0, box[1]-18*scale), box[0]+38*scale, max(18*scale, box[1])), fill='#00252c')
            draw.text((box[0]+2*scale, max(0, box[1]-18*scale)), track['token'], fill='white',font=font)
        labelled.append(copy)
    bindings = [{key: track[key] for key in ('token', 'person_id', 'evidence')} for track in tracks]
    return labelled, bindings


def render_names(value, bindings, names):
    mapping = {binding['token']: names.get(binding.get('person_id'), '') for binding in bindings}
    def replace(match):
        token = 'P'+match[1]
        return mapping.get(token) or 'человек '+match[1]
    if isinstance(value, str):
        return TOKEN.sub(replace, value)
    if isinstance(value, list):
        return [render_names(item, bindings, names) for item in value]
    if isinstance(value, dict):
        return {key: render_names(item, bindings, names) for key, item in value.items()}
    return value


def rebind(catalog, asset_id, bindings, people):
    """Re-evaluate saved face evidence after a rename, rejection, split or merge."""
    faces = {}
    from .config import FACE_VERSION
    timestamps = sorted({e['timestamp_ms'] for b in bindings for e in b.get('evidence', [])})
    if not timestamps:
        return [b | dict(person_id=None) for b in bindings]
    for row in catalog.db.execute('''SELECT f.id,f.vector,u.timestamp_ms FROM faces f
        JOIN face_units fu ON fu.face_id=f.id JOIN units u ON u.id=fu.unit_id
        JOIN assets a ON a.id=f.asset_id WHERE f.asset_id=? AND f.file_version=a.version AND f.model_version=?
        AND u.timestamp_ms>=? AND u.timestamp_ms<=? ORDER BY u.timestamp_ms,f.id''', (asset_id,FACE_VERSION,timestamps[0],timestamps[-1])):
        # Face IDs contain the detection index before their frame suffix.
        number = int(row['id'].split(':')[3])
        faces[row['timestamp_ms'], number] = (row['id'], np.frombuffer(row['vector'], dtype=np.float32))
    result = []
    for binding in bindings:
        votes = Counter()
        for evidence in binding.get('evidence', []):
            face = faces.get((evidence['timestamp_ms'], evidence['face_index']))
            if face and (identity := match_identity(face[1], people, face[0])):
                votes[identity] += 1
        winner = votes.most_common(2)
        identity = winner[0][0] if winner and (len(winner) == 1 or winner[0][1] > winner[1][1]) else None
        result.append(binding | dict(person_id=identity))
    return result
