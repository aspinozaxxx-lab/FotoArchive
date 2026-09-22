"""Independent device controls and compact processing telemetry."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QHBoxLayout,QVBoxLayout,QCheckBox,QLabel


def amount(value):
    for unit in ('Б','КБ','МБ','ГБ'):
        if value<1024 or unit=='ГБ':
            return f'{value:.1f} {unit}'
        value /= 1024


class RemotePanel(QWidget):
    def __init__(self,cfg,backend,parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,3,0,3)
        row = QHBoxLayout()
        self.local = QCheckBox('На этом компьютере')
        self.local.setChecked(cfg.local_enabled)
        self.local.setToolTip('Фоновое распознавание на видеокарте этого компьютера')
        self.enabled = QCheckBox('На сервере')
        self.enabled.setChecked(cfg.remote_enabled)
        def changed(field,enabled):
            setattr(cfg,field,enabled)
            backend.send(action=field,enabled=enabled)
        self.local.toggled.connect(lambda enabled:changed('local_enabled',enabled))
        self.enabled.toggled.connect(lambda enabled:changed('remote_enabled',enabled))
        row.addWidget(self.local)
        row.addWidget(self.enabled)
        self.state = QLabel('Подключение…' if cfg.remote_enabled else 'Сервер выключен')
        self.state.setTextFormat(Qt.PlainText)
        row.addWidget(self.state,1)
        layout.addLayout(row)
        self.details = QLabel()
        self.details.setObjectName('muted')
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.PlainText)
        layout.addWidget(self.details)

    def update_status(self,status):
        names = dict(disabled='Сервер выключен',paused='Сервер на паузе',disconnected='Нет соединения',
            waiting_training='Ожидает завершения обучения · кэш пополняется',ready='Сервер готов',
            working='Сервер обрабатывает',gpu_unavailable='GPU временно недоступна',retrying='Повтор подключения к GPU')
        self.state.setText(names.get(status.get('state'),'Подключение…'))
        self.state.setToolTip(status.get('error',''))
        gpu = status.get('gpu',{})
        values = [f"В очереди {status.get('queued',0)} · в работе {status.get('running',0)} · готово {status.get('completed',0)}",
                  f"↑ {amount(status.get('sent',0))} · ↓ {amount(status.get('received',0))} · {amount(status.get('upload_bps',0))}/с",
                  f"{status.get('rate',0):.1f} кадров/мин · кэш {amount(status.get('cache_bytes',0))}"]
        if gpu:
            values.append(f"GPU {gpu['utilization']}% · память {gpu['memory_mb']/1024:.1f}/{gpu['total_mb']/1024:.0f} ГБ")
        self.details.setText('   |   '.join(values))
        self.details.setVisible(status.get('state') not in ('disabled','paused'))
