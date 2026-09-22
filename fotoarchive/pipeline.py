"""Overlapping directory discovery, CPU processes and one GPU model owner."""
import time

from .preparation import PreparationPool
from .scanning import SourceScanner
from .gpu_preparation import EmbeddingInputs
from .inventory import SourceInventory
from .caption_jobs import CaptionJobs
from .library_check import LibraryCheck


class ProcessingPipeline:
    GPU_STAGES = (('embedding',16),('faces',8),('location',4))

    def __init__(self, engine):
        self.engine, self.catalog = engine, engine.catalog
        self.cpu = PreparationPool(self.catalog, engine.emit)
        self.inputs = EmbeddingInputs(engine)
        self.inventory = SourceInventory(self.catalog, engine.emit)
        self.captions = CaptionJobs(engine)
        self.remote = None
        self.requests = self.catalog.state('scan_queue', [])
        self.scanner = None
        self.scan_state = None
        self.library_check = None
        self.check_finalizer = None
        self.check_status = self.catalog.state('library_check', {})
        if any(request.get('reconcile') for request in self.requests):
            self.check_status = {'phase': 'queued'}
        self.gpu_stage = 0
        self.gpu_remaining = self.GPU_STAGES[0][1]
        self.last_gpu_stage = None
        self.gpu_completed = 0
        self.cpu_completed_during_gpu = 0
        self.gpu_jobs_during_scan = 0

    @property
    def scanning(self):
        return bool(self.requests)

    @property
    def idle(self):
        return not self.scanning and not self.cpu.pending and not self.inputs.pending and not self.captions.pending and not (self.remote and self.remote.pending)

    def request_scan(self, includes=None, skip_includes=(), reconcile=False):
        request = {'includes':list(self.engine.cfg.includes if includes is None else includes),
                   'skip':list(skip_includes)}
        if reconcile:
            if any(item.get('reconcile') for item in self.requests):
                return
            request['reconcile'] = True
            self.check_status = {'phase': 'queued'}
            self.publish_check()
        if request not in self.requests:
            self.requests.append(request)
            self.catalog.set_state('scan_queue',self.requests)
            self.inventory.start()

    def publish_check(self):
        if self.library_check:
            self.check_status = self.library_check.status()
        self.catalog.set_state('library_check', self.check_status)
        self.engine.emit({'type': 'library_check', 'check': self.check_status})

    def scan_tick(self, limit=512):
        try:
            return self._scan_tick(limit)
        except Exception as exc:
            if self.requests and self.requests[0].get('reconcile'):
                if self.scanner:
                    self.scanner.close()
                    self.scanner = None
                self.check_finalizer = None
                if self.library_check:
                    self.library_check.state.update(phase='error', error=str(exc))
                else:
                    self.check_status = {'phase': 'error', 'error': str(exc)}
                self.publish_check()
                self.library_check = None
            raise

    def _scan_done(self):
        self.requests.pop(0)
        self.catalog.set_state('scan_queue', self.requests)
        self.catalog.set_state('last_scan', self.scan_state | {'time': time.time()})
        self.engine.emit({'type': 'scan_done', 'scan': self.scan_state, 'facets': self.catalog.facets()})

    def _scan_tick(self, limit):
        if not self.requests:
            return 0
        if self.check_finalizer is not None:
            try:
                next(self.check_finalizer)
            except StopIteration:
                self.check_finalizer = None
                self.publish_check()
                self.library_check = None
                self._scan_done()
                self.inventory.start(force=True)
            return 0
        if self.scanner is None:
            request = self.requests[0]
            self.library_check = LibraryCheck(self.catalog, request['includes']) if request.get('reconcile') else None
            self.scanner = SourceScanner(self.engine.cfg, request['includes'], request['skip'])
            self.scan_state = {'files':0,'skipped':0,'changed':0,'errors':0}
            if self.library_check:
                self.publish_check()
        count, deadline = 0, time.monotonic()+.025
        while count < limit and time.monotonic() < deadline:
            record = self.scanner.take()
            if record is None:
                break
            if record[0] == 'done':
                self.scanner.close(); self.scanner = None
                if self.library_check:
                    if self.scan_state['errors']:
                        raise OSError('Не все файлы удалось проверить. Удалённые записи сохранены; повторите проверку.')
                    self.check_finalizer = self.library_check.finish()
                else:
                    self._scan_done()
                break
            if record[0] == 'error':
                self.scanner.close(); self.scanner = None
                # Keep the request durable. Resume retries discovery; known files
                # are skipped by version checks instead of being processed again.
                raise OSError(record[1])
            _, path, supported = record
            count += 1
            if supported:
                try:
                    _, changed = (self.library_check.record(path) if self.library_check else self.catalog.register(path))
                    self.scan_state['files'] += 1
                    self.scan_state['changed'] += int(changed)
                except OSError as exc:
                    self.scan_state['errors'] += 1
                    self.engine.emit({'type':'job_error','message':f'{path.name}: {exc}'})
            else:
                self.scan_state['skipped'] += 1
        if self.library_check:
            self.library_check.flush()
        return count

    def process_gpu(self):
        # DirectML sessions stay on their owner thread. One independent Vulkan
        # description runs concurrently, without queuing ahead of user requests.
        for _ in self.GPU_STAGES:
            stage, _ = self.GPU_STAGES[self.gpu_stage]
            unfinished = [future for future in self.cpu.pending if not future.done()]
            job = (self.engine.process_embedding_batch(min(4, self.gpu_remaining))
                   if stage == 'embedding' else self.engine.process_one((stage,)))
            if job:
                self.cpu_completed_during_gpu += sum(future.done() for future in unfinished)
                self.last_gpu_stage = stage
                count = job.get('batch_count', 1)
                self.gpu_jobs_during_scan += count * int(self.scanning)
                self.gpu_completed += count
                self.gpu_remaining -= count
            if not job or not self.gpu_remaining:
                self.gpu_stage = (self.gpu_stage+1) % len(self.GPU_STAGES)
                self.gpu_remaining = self.GPU_STAGES[self.gpu_stage][1]
            if job:
                return job
        return None

    def status(self):
        check = self.library_check.status() if self.library_check else self.check_status
        return self.cpu.status() | {'scanning':self.scanning,'gpu_stage':self.last_gpu_stage,
                                   'gpu_completed':self.gpu_completed,
                                   'cpu_completed_during_gpu':self.cpu_completed_during_gpu,
                                   'gpu_jobs_during_scan':self.gpu_jobs_during_scan,
                                   'prepared_inputs':sum(f.done() for f in self.inputs.pending),
                                   'input_queue':len(self.inputs.pending),
                                   'caption_running':self.captions.pending,
                                   'index_writing':self.engine.index.writing is not None,
                                   'library_check': check}

    def close(self):
        if self.remote:
            self.remote.close()
        self.inventory.close()
        if self.scanner:
            self.scanner.close()
            self.scanner = None
        self.cpu.close()
        self.inputs.close()
        self.captions.close()
