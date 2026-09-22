import numpy as np
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.search_session import SearchSession


def test_long_verification_is_paged_deduplicated_and_retains_results(tmp_path):
    cat = Catalog(Settings(data_dir=tmp_path / "data", root=tmp_path / "source"))
    with cat.db:
        cat.db.executemany("""INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,filename,extension,
                           size,mtime_ns,updated_at,metadata_ready) VALUES(?,?,?,?,?,'2003','test.jpg','.jpg',1,0,0,1)""",
                           [(i, cat.source_id, str(i), str(i), str(i)) for i in range(1, 551)])

    class Index:
        def candidates(self, vector, query, filters, limit, offset, merge_all):
            assert limit == 200 and merge_all
            # Overlap between the vector and description streams must never create repeat checks.
            return [cat.get(i) for i in range(max(1, offset - 20), min(551, offset + 201))]

    session = SearchSession(cat, Index(), 1, Filters(), "query", np.ones(768), ["condition"])
    seen = set()
    while batch := session.pending():
        assert len(batch) <= 40
        for asset in batch:
            assert asset["id"] not in seen
            seen.add(asset["id"])
            session.mark(asset, {"verdict": "yes" if asset["id"] % 2 else "no"})
        assert len(session.page()["items"]) <= 200
    assert len(seen) == 550
    assert session.counts() == {"candidates": 550, "checked": 550}
    assert not session.can_verify()
    page = session.page(offset=200, verdict="yes")
    assert page["page_total"] == 275 and len(page["items"]) == 75
    assert all(a["verification"]["verdict"] == "yes" for a in page["items"])
    cat.close()


def test_ann_head_continues_to_complete_exact_stream(tmp_path):
    cat = Catalog(Settings(data_dir=tmp_path / "data", root=tmp_path / "source"))
    with cat.db:
        cat.db.executemany("""INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,filename,extension,
                           size,mtime_ns,updated_at,metadata_ready) VALUES(?,?,?,?,?,'2003','test.jpg','.jpg',1,0,0,1)""",
                           [(i, cat.source_id, str(i), str(i), str(i)) for i in range(1, 601)])
    cat.set_state("ann_rows", 600)

    class Index:
        def candidates(self, vector, query, filters, limit, offset, merge_all, exact=False):
            if exact:
                return [cat.get(i) for i in range(offset+1, min(601, offset+limit+1))]
            # An approximate index can omit most of the archive and return a different head.
            return [cat.get(i) for i in (599, 600)] if offset == 0 else []

    session = SearchSession(cat, Index(), 1, Filters(), "query", np.ones(768), [])
    found = []
    while True:
        page = session.page(offset=len(found), expand=True)
        found += [a["id"] for a in page["items"]]
        if not page["has_more"] and len(found) >= page["page_total"]:
            break
    assert found[:2] == [599, 600]
    assert len(found) == 600 and len(set(found)) == 600
    cat.close()


def test_browse_pages_stay_stable_as_new_photos_are_indexed(tmp_path):
    cat = Catalog(Settings(data_dir=tmp_path / "data", root=tmp_path / "source"))
    def insert(ids):
        with cat.db:
            cat.db.executemany("""INSERT INTO assets(id,source_id,relative_path,path_key,path,folder,
                filename,extension,size,mtime_ns,updated_at,metadata_ready,captured_at)
                VALUES(?,?,?,?,?,'2003','test.jpg','.jpg',1,0,0,1,'2003-01-01')""",
                [(i, cat.source_id, str(i), str(i), str(i)) for i in ids])
    insert(range(1, 551))
    session = SearchSession(cat, None, 1, Filters(folder="2003"), "", None, [], mode="browse")
    first = session.page(offset=0)
    assert first["metadata_count"] == 550
    insert(range(551, 571))
    # Even a reindexed existing photo keeps its place in the open browsing session.
    with cat.db:
        cat.db.execute("UPDATE assets SET version=2,captured_at='2005-01-01' WHERE id=10")
    second = session.page(offset=200)
    third = session.page(offset=400)
    ids = [a["id"] for page in (first, second, third) for a in page["items"]]
    assert ids == list(range(550, 0, -1))
    assert second["page_total"] == 550
    assert [a["id"] for a in session.page(offset=0)["items"]] == ids[:200]
    refreshed = SearchSession(cat, None, 2, Filters(folder="2003"), "", None, [], mode="browse")
    page = refreshed.page()
    assert page["page_total"] == 570
    assert page["metadata_count"] == 570
    assert [a["id"] for a in page["items"]][:21] == [10] + list(range(570, 550, -1))
    cat.close()
