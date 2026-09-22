from types import SimpleNamespace
from fotoarchive.processing_metrics import ProcessingMetrics
from fotoarchive.remote_panel import RemotePanel
from fotoarchive.config import Settings
from test_remote import ready_catalog


def test_committed_stages_are_separate_and_rate_expires(tmp_path):
    clock=[100.]
    metrics=ProcessingMetrics(clock=lambda:clock[0])
    cat=ready_catalog(tmp_path)
    cat.on_job_finished=metrics.record
    job=cat.next_job(('embedding',))
    cat.finish_job(job,1)
    cat.finish_job(job,1)  # Already committed; never inflate the report.
    other=cat.next_job(('faces',))
    cat.finish_job(other,1,source='remote')
    failed=cat.next_job(('caption',))
    cat.finish_job(failed,1,'broken image')
    stats=metrics.snapshot()
    assert stats['local']['total']==stats['local']['rate']==1
    assert stats['local']['errors']==1
    assert stats['remote']['completed']=={'faces':1}
    clock[0]=161
    assert metrics.snapshot()['local']['rate']==0
    assert metrics.snapshot()['local']['total']==1
    assert not metrics.recent
    cat.close()


def test_panel_separates_volumes_rates_and_commit_counts(qtbot,tmp_path):
    commands=[]
    panel=RemotePanel(Settings(data_dir=tmp_path),SimpleNamespace(send=lambda **kw:commands.append(kw)))
    qtbot.addWidget(panel);panel.resize(1200,260);panel.show()
    metrics=ProcessingMetrics();metrics.record({'stage':'faces'});metrics.record({'stage':'caption'},source='remote')
    panel.update_local(dict(processing_metrics=metrics.snapshot(),pipeline={'gpu_stage':'faces'},local_enabled=True))
    panel.update_status(dict(state='working',queued=45,running=1,completed=150,saved=147,awaiting_save=3,rate=27,
        sent=85*1024**2,received=7.5*1024**2,upload_bps=200*1024,download_bps=1000,
        cache_bytes=1024**3,cache_limit=20*1024**3,gpu=dict(utilization=92,memory_mb=8192,total_mb=32768)))
    assert panel.local_card.counts['faces'].text()=='1'
    assert panel.remote_card.counts['caption'].text()=='1'
    assert panel.job_values['saved'].text()=='147'
    assert panel.job_values['awaiting_save'].text()=='3'
    assert panel.transfer_values['upload_rate'].text()=='200.0 КБ/с'
    assert panel.transfer_values['upload_total'].text()=='85.0 МБ'
    assert panel.cache.text()=='Диск: 1.0 ГБ из 20.0 ГБ'
    assert panel.cache_bar.value()==50
    panel.local.setChecked(False);panel.enabled.setChecked(True)
    assert commands==[dict(action='local_enabled',enabled=False),dict(action='remote_enabled',enabled=True)]
    panel.update_status(dict(state='disconnected'))
    assert panel.gpu.text()=='GPU: —' and panel.job_values['completed'].text()=='—'


def test_report_numbers_remain_readable_when_drawer_opens(qtbot,tmp_path):
    from fotoarchive.ui import MainWindow
    from test_ui import FakeBackend
    from PySide6.QtWidgets import QApplication
    window=MainWindow(Settings(data_dir=tmp_path),FakeBackend())
    qtbot.addWidget(window)
    window.pipeline_label.setText('Подготовка CPU · GPU: лица + описания')
    window.updates_label.setText('Проверка завершена');window.updates_label.show()
    window.show()
    for theme in ('light','dark'):
        window.apply_theme(theme,save=False)
        for height in (972,800):
            window.processing_toggle.setChecked(False)
            window.resize(1482,height)
            QApplication.processEvents()
            window.processing_toggle.setChecked(True)
            QApplication.processEvents()
            for card in (window.remote_panel.local_card,window.remote_panel.remote_card):
                for value in card.counts.values():
                    assert value.height()>=value.fontMetrics().height()
    window.close()
