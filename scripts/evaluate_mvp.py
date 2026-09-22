import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.engine import Engine

parser=argparse.ArgumentParser()
parser.add_argument('--retrieval-only',action='store_true')
args=parser.parse_args()
cfg=Settings.load()
engine=Engine(cfg)
gold=json.loads((Path(__file__).resolve().parents[1]/'evaluation/mvp_queries.json').read_text(encoding='utf-8'))
report={'annotation_method':gold['annotation_method'],'queries':[],'pairs':[]}
started=time.perf_counter()
try:
    engine.index.flush_all()
    engine.index.maintain()
    engine.embeddings().text('прогрев')
    tp=fp=fn=tn=uncertain=0
    for qn,item in enumerate(gold['queries']):
        start=time.perf_counter()
        vector=engine.embeddings().text(item['query'])
        results=engine.index.candidates(vector,item['query'],Filters(),limit=20)
        ids=[r['id'] for r in results]
        query={'query':item['query'],'top20':ids,'seconds':time.perf_counter()-start,
               'has_known_positive':bool(set(ids)&set(item['positive'])) if item['positive'] else None}
        report['queries'].append(query)
        if not args.retrieval_only:
            for expected,ids_to_check in [('yes',item['positive']),('no',item['negative'])]:
                for asset_id in ids_to_check:
                    asset=engine.catalog.get(asset_id)
                    start=time.perf_counter()
                    try:
                        result=engine.verify(asset,item['conditions'])
                    except Exception as exc:
                        result={'verdict':'uncertain','error':str(exc)}
                    verdict=result['verdict']
                    pair={'query_number':qn+1,'asset_id':asset_id,'relative_path':asset['relative_path'],
                          'content_hash':asset['content_hash'],'expected':expected,'actual':verdict,
                          'seconds':time.perf_counter()-start,'result':result}
                    report['pairs'].append(pair)
                    uncertain+=int(verdict=='uncertain')
                    if expected=='yes':
                        tp+=int(verdict=='yes');fn+=int(verdict!='yes')
                    else:
                        fp+=int(verdict=='yes');tn+=int(verdict!='yes')
                    (cfg.data_dir/'reports/evaluation_progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'query':qn+1,'hit':query['has_known_positive'],'tp':tp,'fp':fp,'fn':fn,'tn':tn,'uncertain':uncertain}),flush=True)
    evaluated=[q for q in report['queries'] if q['has_known_positive'] is not None]
    report.update(hit_at_20=sum(q['has_known_positive'] for q in evaluated)/len(evaluated),
                  retrieval_p95_seconds=float(np.percentile([q['seconds'] for q in report['queries']],95)),
                  verification_precision=tp/(tp+fp) if tp+fp else None,
                  verification_recall=tp/(tp+fn) if tp+fn else None,
                  confusion={'tp':tp,'fp':fp,'fn':fn,'tn':tn,'uncertain':uncertain},
                  elapsed_seconds=time.perf_counter()-started)
    name='retrieval_evaluation.json' if args.retrieval_only else 'evaluation.json'
    (cfg.data_dir/'reports'/name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'queries','pairs','annotation_method'}},indent=2),flush=True)
finally:
    engine.close()
