"""Synthetic 200k-row benchmark; never indexes photos outside the MVP."""
import json
import math
import os
os.environ.setdefault("RAYON_NUM_THREADS", "6")
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lancedb
import numpy as np
import pyarrow as pa
import psutil
from fotoarchive.search import SCHEMA
from fotoarchive.config import Settings, EMBED_VERSION

cfg=Settings.load()
output=cfg.data_dir/'reports/scale_200k'
output.mkdir(parents=True,exist_ok=True)
con=sqlite3.connect(f'file:{cfg.data_dir / "catalog.sqlite3"}?mode=ro',uri=True)
base=np.stack([np.frombuffer(row[0],dtype=np.float32) for row in con.execute('SELECT vector FROM embeddings LIMIT 205')])
con.close()
rng=np.random.default_rng(270917)
count=200_000
vectors=np.lib.format.open_memmap(output/'vectors.npy',mode='w+',dtype=np.float32,shape=(count,768))
db=lancedb.connect(str(output/'lance'))
table=db.create_table('synthetic',schema=SCHEMA,mode='overwrite')
started=time.perf_counter()
for offset in range(0,count,5000):
    n=min(5000,count-offset)
    data=base[rng.integers(0,len(base),n)] + rng.normal(0,0.02,(n,768)).astype(np.float32)
    data/=np.linalg.norm(data,axis=1,keepdims=True)
    vectors[offset:offset+n]=data
    rows=[]
    for j in range(n):
        i=offset+j
        rows.append({'unit_id':f'{i}:photo','asset_id':i,'version':1,'model_version':EMBED_VERSION,'vector':data[j].tolist(),
            'folder':f'year_{2000+i%25}','captured_at':f'{2000+i%25}-05-09T12:00:00','camera':'Synthetic camera',
            'extension':'.jpg','orientation':'landscape','width':1600,'height':1200,'pixels':1920000,'size':500000,
            'present':1,'metadata_ready':1,'search_text':'Синтетическая фотография для проверки масштабирования '+str(i)})
    table.add(pa.Table.from_pylist(rows,schema=SCHEMA))
    if offset%25000==0: print(f'Created {offset+n} rows',flush=True)
vectors.flush()
report={'rows':count,'dimension':768,'seed':270917,'load_seconds':time.perf_counter()-started}
started=time.perf_counter()
print('Building IVF_HNSW_SQ',flush=True)
table.create_index(metric='cosine',index_type='IVF_HNSW_SQ',num_partitions=round(math.sqrt(count)))
report['build_seconds']=time.perf_counter()-started
latencies=[]; recalls=[]
for n in range(20):
    query=base[n%len(base)]
    truth=set(np.argpartition(vectors@query,-20)[-20:].tolist())
    start=time.perf_counter()
    rows=table.search(query).metric('cosine').nprobes(32).refine_factor(3).select(['asset_id']).limit(20).to_list()
    latencies.append(time.perf_counter()-start)
    recalls.append(len(truth & {r['asset_id'] for r in rows})/20)
report.update(query_p95_seconds=float(np.percentile(latencies,95)),query_median_seconds=float(np.median(latencies)),recall_at_20=float(np.mean(recalls)),rss_mb=psutil.Process().memory_info().rss/1024**2)
report['passes']=report['query_p95_seconds']<2 and report['recall_at_20']>=0.95
(output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2),flush=True)
