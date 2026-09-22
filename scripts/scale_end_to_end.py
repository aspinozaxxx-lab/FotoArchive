"""Catalog + text embedding + hybrid retrieval benchmark on 200k synthetic rows."""
import os
os.environ.setdefault('RAYON_NUM_THREADS','6')
import json
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import lancedb
import numpy as np
from lancedb.index import FTS
from fotoarchive.config import Settings
from fotoarchive.catalog import Catalog,Filters
from fotoarchive.search import SearchIndex
from fotoarchive.inference import Embedder

cfg=Settings.load()
folder=cfg.data_dir/'reports/scale_200k'
synthetic_cfg=Settings(data_dir=folder/'catalog',root=folder/'synthetic_source')
catalog=Catalog(synthetic_cfg)
db=lancedb.connect(str(folder/'lance'))
table=db.open_table('synthetic')
if catalog.stats()['total']!=200000:
    for start in range(0,200000,5000):
        rows=[]
        for i in range(start,start+5000):
            rows.append((i,catalog.source_id,f'year_{2000+i%25}/img_{i}.jpg',f'synthetic_{i}',f'synthetic_{i}',f'year_{2000+i%25}',f'img_{i}.jpg','.jpg',500000,0,1,1600,1200,1920000,f'{2000+i%25}-05-09T12:00:00','Synthetic camera','landscape',time.time()))
        with catalog.db:
            catalog.db.executemany('''INSERT OR IGNORE INTO assets(id,source_id,relative_path,path_key,path,folder,filename,extension,size,mtime_ns,metadata_ready,width,height,pixels,captured_at,camera,orientation,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
table.create_index('search_text',config=FTS(language='Russian',num_workers=4,memory_limit=256*1024**2),replace=True)
catalog.set_state('ann_rows',200000)
index=object.__new__(SearchIndex)
index.catalog=catalog;index.connection=db;index.table=table
embed=Embedder(cfg)
embed.text('прогрев')
times=[];browse=[]
for i in range(20):
    query=['люди в спортивном зале','друзья на берегу реки','салют в ночном небе','автомобиль у дороги'][i%4]
    start=time.perf_counter()
    vector=embed.text(query)
    results=index.candidates(vector,query,Filters(),limit=200)
    times.append(time.perf_counter()-start)
    assert results
    start=time.perf_counter()
    rows,total=catalog.browse(Filters(date_from='2010-01-01'),limit=200)
    browse.append(time.perf_counter()-start)
    assert total==120000
report={'rows':200000,'end_to_end_p95_seconds':float(np.percentile(times,95)),'end_to_end_median_seconds':float(np.median(times)),
        'filtered_browse_p95_seconds':float(np.percentile(browse,95)),'scope':'Russian text encoding on DirectML + vector and text retrieval + materialization from SQLite; 200 results'}
report['passes']=report['end_to_end_p95_seconds']<2
(folder/'end_to_end.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2),flush=True)
catalog.close()
