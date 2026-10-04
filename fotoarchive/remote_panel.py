"""Comparable device reports and an explicit remote delivery pipeline."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QHBoxLayout,QVBoxLayout,QGridLayout,QCheckBox,QLabel,QFrame,QProgressBar,QLayout,QComboBox,QPushButton
from .processing_metrics import STAGES


def amount(value):
    for unit in ('Б','КБ','МБ','ГБ'):
        if value<1024 or unit=='ГБ':
            return f'{value:.1f} {unit}'
        value /= 1024


def number(value):
    return f'{int(value):,}'.replace(',',' ')


def label(text='',muted=False):
    widget=QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if muted:
        widget.setObjectName('muted')
    return widget


def card():
    frame=QFrame()
    frame.setObjectName('panel')
    layout=QVBoxLayout(frame)
    layout.setContentsMargins(10,7,10,7)
    layout.setSpacing(3)
    return frame,layout


class DeviceCard(QFrame):
    def __init__(self,title,checked):
        super().__init__()
        self.setObjectName('panel')
        self.setToolTip('Считаются успешно сохранённые этапы обработки, а не найденные лица или объекты. Один снимок может дать по одному результату в каждой колонке. Обе видеокарты сравниваются за одинаковые 60 секунд.')
        layout=QVBoxLayout(self)
        layout.setContentsMargins(10,7,10,7)
        layout.setSpacing(3)
        header=QHBoxLayout()
        self.enabled=QCheckBox(title)
        self.enabled.setChecked(checked)
        font=self.enabled.font();font.setBold(True);self.enabled.setFont(font)
        self.state=label('Подключение…')
        header.addWidget(self.enabled)
        header.addWidget(self.state,1,Qt.AlignRight)
        layout.addLayout(header)
        grid=QGridLayout();grid.setHorizontalSpacing(15);grid.setVerticalSpacing(2)
        self.counts,self.rates={},{}
        for col,(stage,name) in enumerate(zip(STAGES,('Поиск','Лица','Описания','Места','Видео'))):
            grid.addWidget(label(name,True),0,col)
            value=label('0');value.setStyleSheet('font-size: 13pt; font-weight: 600;')
            self.counts[stage]=value;grid.addWidget(value,1,col)
            rate=label('0 /мин',True);self.rates[stage]=rate;grid.addWidget(rate,2,col)
            grid.setColumnStretch(col,1)
        layout.addLayout(grid)
        self.summary=label('Сохранено: 0 этапов',True)
        layout.addWidget(self.summary)

    def update_metrics(self,metrics):
        for stage in STAGES:
            self.counts[stage].setText(number(metrics.get('completed',{}).get(stage,0)))
            self.rates[stage].setText(number(metrics.get('rates',{}).get(stage,0))+' /мин')
        self.summary.setText(f"Сохранено: {number(metrics.get('total',0))} этапов    Ошибок: {number(metrics.get('errors',0))}")


class RemotePanel(QWidget):
    def __init__(self,cfg,backend,parent=None):
        super().__init__(parent)
        self.cfg=cfg
        layout=QVBoxLayout(self)
        # The drawer is shown after the main window was laid out while hidden.
        # Enforce the report's minimum so Qt cannot squeeze metric rows below
        # their text height when the gallery also requests vertical space.
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(0,3,0,3);layout.setSpacing(5)
        devices=QHBoxLayout();devices.setSpacing(6)
        self.local_card=DeviceCard('На этом компьютере',cfg.local_enabled)
        self.remote_card=DeviceCard('На сервере',cfg.remote_enabled)
        self.local,self.enabled=self.local_card.enabled,self.remote_card.enabled
        self.state=self.remote_card.state
        self.local.setToolTip('Фоновое распознавание на видеокарте этого компьютера')
        self.enabled.setToolTip('Фоновое распознавание на подключённом сервере')
        self.state.setText('Подключение…' if cfg.remote_enabled else 'Выключено')
        def changed(field,enabled):
            setattr(cfg,field,enabled)
            backend.send(action=field,enabled=enabled)
        self.local.toggled.connect(lambda enabled:changed('local_enabled',enabled))
        self.enabled.toggled.connect(lambda enabled:changed('remote_enabled',enabled))
        devices.addWidget(self.local_card,1);devices.addWidget(self.remote_card,1)
        layout.addLayout(devices)
        video = QHBoxLayout()
        video.addWidget(label('Описание видео'))
        self.video_model = QComboBox()
        self.video_model.addItem('MiniCPM-o 4.5 · видео и звук', 'omni')
        self.video_model.addItem('InternVideo2.5 · видеоряд на сервере', 'internvideo')
        self.video_model.setCurrentIndex(max(0,self.video_model.findData(cfg.video_model)))
        self.video_model.activated.connect(lambda _:backend.send(action='video_model',model=self.video_model.currentData()))
        video.addWidget(self.video_model)
        self.video_progress = label('')
        video.addWidget(self.video_progress,1)
        rebuild = QPushButton('Перестроить видео')
        rebuild.setToolTip('Повторно обработать все видео выбранной моделью. Фотографии и исходные файлы не меняются. Готовые результаты доступны во время обработки.')
        rebuild.clicked.connect(lambda:backend.send(action='rebuild_videos'))
        video.addWidget(rebuild)
        layout.addLayout(video)
        self.details=QWidget();details=QHBoxLayout(self.details)
        details.setContentsMargins(0,0,0,0);details.setSpacing(6)
        jobs,jobs_layout=card();jobs_layout.addWidget(label('Задания сервера'))
        jobs.setToolTip('Одно задание — снимок или отрезок видео до 30 секунд и все его этапы: описание, поиск, лица. Завершённым считается задание без ошибок. «В каталоге» — результат уже сохранён на ПК.')
        job_grid=QGridLayout();job_grid.setVerticalSpacing(0);job_grid.setHorizontalSpacing(12)
        self.job_values={}
        for i,(field,title) in enumerate((('queued','В очереди сервера'),('running','В работе'),('completed','Завершено'),
                                        ('saved','В каталоге'),('awaiting_save','Ещё не сохранено'),('rate','Заданий /мин'),
                                        ('upload_queue','К подготовке'),('preparing','Готовятся на ПК'),('prepared','Готовы к отправке'))):
            row,col=divmod(i,3)
            job_grid.addWidget(label(title,True),row*2,col)
            value=label('—');font=value.font();font.setBold(True);value.setFont(font)
            job_grid.addWidget(value,row*2+1,col);self.job_values[field]=value
        jobs_layout.addLayout(job_grid);details.addWidget(jobs,5)
        traffic,traffic_layout=card();traffic_layout.addWidget(label('Передача данных'))
        grid=QGridLayout();grid.setVerticalSpacing(1);grid.setHorizontalSpacing(12)
        grid.addWidget(label('Сейчас',True),0,1);grid.addWidget(label('За запуск',True),0,2)
        self.transfer_values={}
        for row,(name,title) in enumerate((('upload','Отправка'),('download','Получение')),1):
            grid.addWidget(label(title),row,0)
            for col,suffix in ((1,'rate'),(2,'total')):
                value=label('—');grid.addWidget(value,row,col);self.transfer_values[name+'_'+suffix]=value
        traffic_layout.addLayout(grid)
        self.transfer_state=label('Подключение…',True);self.transfer_state.setWordWrap(True);traffic_layout.addWidget(self.transfer_state)
        self.transfer_file=label('',True);traffic_layout.addWidget(self.transfer_file)
        traffic.setToolTip('Скорости передачи полезных данных усреднены за последние 10 секунд. Объёмы справа накоплены с запуска приложения; это не скорости.')
        details.addWidget(traffic,4)
        resource,resource_layout=card();resource_layout.addWidget(label('Ресурсы сервера'))
        self.cache=label('Диск: —');resource_layout.addWidget(self.cache)
        self.cache_bar=QProgressBar();self.cache_bar.setRange(0,1000);self.cache_bar.setTextVisible(False)
        self.cache_bar.setToolTip('Занято в дисковом кэше / допустимый объём. Готовые снимки вытесняются автоматически; ожидающие обработки защищены.')
        resource_layout.addWidget(self.cache_bar)
        self.cache_details=label('',True);self.cache_details.setWordWrap(True);resource_layout.addWidget(self.cache_details)
        self.gpu=label('GPU: —');resource_layout.addWidget(self.gpu)
        self.memory=label('Видеопамять: —',True);resource_layout.addWidget(self.memory)
        resource.setToolTip('Загрузка и видеопамять всего серверного GPU, включая другие приложения. Дисковый кэш хранит упакованные входные данные и результаты.')
        details.addWidget(resource,4)
        layout.addWidget(self.details)
        note=label('Счётчики этапов — за этот запуск. Темп — сохранённые результаты за последние 60 секунд.',True)
        layout.addWidget(note)
        self.warning=label();self.warning.setWordWrap(True);self.warning.hide();layout.addWidget(self.warning)

    def update_local(self,event):
        if event.get('video_model'):
            self.video_model.setCurrentIndex(max(0,self.video_model.findData(event['video_model'])))
        for source,device in (('local',self.local_card),('remote',self.remote_card)):
            device.update_metrics(event.get('processing_metrics',{}).get(source,{}))
        pipeline=event.get('pipeline',{})
        files=event.get('stats',{}).get('video_files',{})
        segments=event.get('stats',{}).get('video_segments',{})
        self.video_progress.setText(f"Ролики {files.get('done',0)} / {files.get('total',0)} · отрезков готово {segments.get('done',0)}")
        self.video_progress.setToolTip(pipeline.get('video_notice',''))
        if not event.get('local_enabled',True):
            state='Завершает описание' if pipeline.get('caption_running') else 'Выключено'
        elif event.get('paused'):
            state='Пауза'
        else:
            state={'embedding':'Поиск','faces':'Лица','caption':'Описание','location':'Места'}.get(pipeline.get('gpu_stage'),'Ожидание')
            if pipeline.get('caption_running'):
                state+=' + описания'
            if pipeline.get('video_running'):
                state='Видео и звук'
        self.local_card.state.setText(state)

    def update_status(self,status):
        names=dict(disabled='Выключено',paused='Пауза',disconnected='Нет соединения',
            waiting_training='GPU занята другой задачей',ready='Ожидание заданий',working='Работает',
            gpu_unavailable='GPU недоступна',retrying='Повтор подключения к GPU')
        self.state.setText(names.get(status.get('state'),'Подключение…'))
        if status.get('state')=='ready':
            if status.get('preparing'):self.state.setText('Ожидает подготовку медиа')
            elif status.get('prepared') or status.get('uploading_count'):self.state.setText('Ожидает передачу снимков')
            elif status.get('upload_queue'):self.state.setText('Ожидает подготовку снимков')
            elif status.get('awaiting_save'):self.state.setText('Ожидает сохранение результатов')
        self.state.setToolTip(status.get('error',''))
        online=status.get('state') not in ('disabled','paused','disconnected')
        for field,value in self.job_values.items():
            value.setText(number(status[field]) if field in status else '—')
        for prefix,total in (('upload','sent'),('download','received')):
            self.transfer_values[prefix+'_rate'].setText(amount(status.get(prefix+'_bps',0))+'/с' if online else '—')
            self.transfer_values[prefix+'_total'].setText(amount(status.get(total,0)))
        transfer=dict(preparing='Подготовка медиа',uploading='Передача медиа',cache_full='Запас для обработки заполнен',idle='Ожидание следующего задания')
        text=transfer.get(status.get('uploading'),'') if online else 'Передача остановлена'
        if status.get('uploading_count') and online:text+=f" · {status['uploading_count']} одновременно"
        self.transfer_state.setText(text)
        filename=status.get('transfer_file','') if online else ''
        self.transfer_file.setText(self.transfer_file.fontMetrics().elidedText(filename,Qt.ElideMiddle,220))
        self.transfer_file.setToolTip(filename)
        size,limit=status.get('cache_bytes'),status.get('cache_limit')
        self.cache.setText(f'Диск: {amount(size)} из {amount(limit)}' if size is not None and limit else 'Диск: —')
        self.cache_bar.setValue(min(1000,int(size/limit*1000)) if size is not None and limit else 0)
        required=status.get('cache_pending_bytes',0)
        reusable=status.get('cache_reusable_bytes',max(0,(size or 0)-required))
        self.cache_details.setText(f'Для текущих заданий: {amount(required)}\nМожно заменить автоматически: {amount(reusable)}' if size is not None else '')
        stages={'embedding':'поиск','faces':'лица','caption':'описание','location':'место','video':'видео, звук, поиск и лица'}
        current=', '.join(stages.get(stage,stage) for stage in status.get('current_stages',[]))
        if current:self.state.setToolTip('Этапы текущего задания: '+current)
        gpu=status.get('gpu',{}) if online else {}
        self.gpu.setText(f"GPU: {gpu['utilization']}%" if gpu else 'GPU: —')
        self.memory.setText(f"Видеопамять: {gpu['memory_mb']/1024:.1f} из {gpu['total_mb']/1024:.1f} ГБ" if gpu else 'Видеопамять: —')
        problems=status.get('partial',0)+status.get('failed',0)
        discarded=status.get('discarded',0)
        warnings=[]
        if problems:
            warnings.append(f'Сервер: {problems} заданий требуют повторной обработки.')
        if discarded:
            warnings.append(f'Не применено {discarded} результатов: файлы изменились или требуется повтор.')
        self.warning.setText(' '.join(warnings))
        self.warning.setVisible(bool(warnings))
