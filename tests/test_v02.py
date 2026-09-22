import json
import sqlite3
from datetime import date
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from fotoarchive.date_slider import DateRangeSlider
from fotoarchive.gallery import PhotoModel
from fotoarchive.face_widgets import FacePreview
from fotoarchive.location import coordinate, extract_location, Places
from fotoarchive.faces import similarity_transform
from fotoarchive.catalog import Catalog, Filters
from fotoarchive.config import Settings
from fotoarchive.search import SearchIndex
from fotoarchive.search_session import SearchSession


def test_date_slider_handles_and_movable_interval(qtbot):
    slider = DateRangeSlider()
    qtbot.addWidget(slider)
    slider.resize(424, 66)
    slider.setBounds(100, 500)
    slider.setRange(200, 350)
    slider.show()
    qtbot.mousePress(slider, Qt.LeftButton, pos=QPoint(round(slider.x_for(200)), 24))
    qtbot.mouseMove(slider, QPoint(round(slider.x_for(220)), 24))
    qtbot.mouseRelease(slider, Qt.LeftButton)
    assert (slider.lower, slider.upper) == (220, 350)
    qtbot.mousePress(slider, Qt.LeftButton, pos=QPoint(round(slider.x_for(280)), 24))
    qtbot.mouseMove(slider, QPoint(round(slider.x_for(330)), 24))
    qtbot.mouseRelease(slider, Qt.LeftButton)
    assert (slider.lower, slider.upper) == (270, 400)
    qtbot.keyClick(slider, Qt.Key_Right, Qt.ShiftModifier)
    assert (slider.lower, slider.upper) == (271, 401)
    slider.setRange(370, 500)
    qtbot.keyClick(slider, Qt.Key_Right, Qt.ShiftModifier)
    assert (slider.lower, slider.upper) == (370, 500)


def test_gallery_200k_rows_have_bounded_cache_and_reload_evicted(qtbot):
    model = PhotoModel()
    model.reset_result([], 200_000, False)
    requests = []
    model.pageRequested.connect(requests.append)
    for offset in range(0, 200_000, 200):
        model.fetchMore()
        assert requests[-1] == offset
        model.accept_page(offset, [{"id": i} for i in range(offset, offset+200)], 200_000, False)
    assert model.rowCount() == 200_000 and not model.canFetchMore()
    assert sum(map(len, model.blocks.values())) == 1600
    assert model.asset(0) is None and requests[-1] == 0
    model.accept_page(0, [{"id": i} for i in range(200)], 200_000, False)
    assert model.asset(0)["id"] == 0 and len(model.blocks) == 8


def test_face_click_accounts_for_letterboxing(qtbot):
    widget = FacePreview()
    qtbot.addWidget(widget)
    widget.resize(400, 200)
    pixmap = QPixmap(100, 200)
    pixmap.fill(Qt.white)
    widget.setPhoto(pixmap)
    widget.setFaces([{"id": "one", "box": [.2, .2, .6, .6]}])
    clicked = []
    widget.personSelected.connect(clicked.append)
    widget.show()
    qtbot.mouseClick(widget, Qt.LeftButton, pos=QPoint(190, 80))
    assert clicked == ["one"]
    qtbot.mouseClick(widget, Qt.LeftButton, pos=QPoint(20, 80))
    assert clicked == ["one"]


def test_alignment_preserves_landmark_geometry():
    target = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]])
    rotation = np.array([[.8, -.6], [.6, .8]])
    source = target @ rotation * 2.3 + [80, 100]
    affine = similarity_transform(source)
    np.testing.assert_allclose(np.c_[source, np.ones(5)] @ affine.T, target, atol=1e-4)


@pytest.mark.parametrize("value,ref,latitude,expected", [((55,45,0),"N",True,55.75), ((33,30,0),"S",True,-33.5),
    ("37,30,0W","",False,-37.5), ("-120.5","",False,-120.5), ((55,99,0),"N",True,None),
    ("nan","",False,None), (181,"E",False,None), (None,"",True,None)])
def test_coordinates(value, ref, latitude, expected):
    assert coordinate(value, ref, latitude) == expected


def test_exif_gps_and_local_place_provenance(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data")
    cfg.initialize()
    path = tmp_path / "gps.jpg"
    exif = Image.Exif()
    exif[34853] = {1:"N", 2:(55.,45.,0.), 3:"E", 4:(37.,37.,12.), 5:1, 6:12.5}
    Image.new("RGB", (64, 48)).save(path, exif=exif)
    location = extract_location(path)
    assert location["latitude"] == 55.75 and location["longitude"] == 37.62
    assert location["altitude"] == -12.5 and location["geo_source"] == "EXIF GPS"
    db = sqlite3.connect(cfg.data_dir / "geonames/cities.sqlite3")
    db.execute("CREATE TABLE cities(id INTEGER,name,aliases,country,latitude,longitude)")
    db.execute("INSERT INTO cities VALUES(1,'Moscow','Москва','Russia',55.75,37.62)")
    db.commit(); db.close()
    places = Places(cfg)
    enriched = places.enrich(location)
    assert "Moscow" in enriched["geo_text"]
    assert "Москва" in json.loads(enriched["geo_json"])["alternate_names"]
    assert json.loads(enriched["geo_json"])["place_names_are_approximate"]
    places.close()


def test_face_geo_outbox_replay_prefilter_dedup_and_invalidation(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", root=tmp_path / "originals")
    cfg.root.mkdir()
    cat = Catalog(cfg)
    visual = np.zeros(768, np.float32); visual[0] = 1
    face_vector = np.zeros(128, np.float32); face_vector[0] = 1
    ids = []
    for number in range(2):
        path = cfg.root / f"{number}.jpg"
        Image.new("RGB", (80, 60)).save(path)
        asset_id, _ = cat.register(path)
        ids.append(asset_id)
        job = {"asset_id": asset_id, "file_version": 1}
        cat.db.execute("UPDATE assets SET metadata_ready=1,folder=? WHERE id=?", (str(number), asset_id))
        cat.complete_embedding(job, visual)
        for count in (1, 2):
            # The final transaction stores two faces in the same photo; results must still deduplicate it.
            cat.complete_faces(job, [{"box":[.1,.1,.5,.5], "landmarks":[[.2,.2]]*5, "confidence":.9,
                                  "vector":face_vector, "portrait":Image.new("RGB", (112,112))}] * count)
        cat.complete_location(job, {"latitude":55.75, "longitude":37.62, "altitude":None,
                                   "geo_text":"Москва", "geo_source":"fixture", "geo_json":"{}"}, visual)
    index = SearchIndex(cat)
    cat.db.execute("CREATE TRIGGER fail_ack BEFORE DELETE ON outbox BEGIN SELECT RAISE(ABORT,'crash'); END")
    cat.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        index.flush_all()
    cat.db.execute("DROP TRIGGER fail_ack"); cat.db.commit()
    index.flush_all(); index.maintain()
    assert index.faces.count_rows() == 4 and index.contexts.count_rows() == 2
    np.testing.assert_array_equal(cat.vector(ids[0]), visual)
    session = SearchSession(cat, index, 1, Filters(folder="0"), "", face_vector, [], mode="face")
    assert [a["id"] for a in session.page(expand=True)["items"]] == [ids[0]]
    assert [a["id"] for a in index.candidates(visual, "Москва", Filters(folder="1"))] == [ids[1]]
    first_face = cat.faces_for(ids[0])[0]["id"]
    Image.new("RGB", (120,80), "red").save(cfg.root / "0.jpg")
    cat.register(cfg.root / "0.jpg")
    index.flush_all()
    assert cat.face_vector(first_face) is None and cat.faces_for(ids[0]) == []
    assert index.faces.count_rows() == 2 and index.contexts.count_rows() == 1
    cat.close()
