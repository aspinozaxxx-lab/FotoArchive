"""Committed work by device; one bounded, shared rolling-minute definition."""
from collections import Counter, deque
import time

STAGES = ('embedding', 'faces', 'caption', 'location')


class ProcessingMetrics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.completed = {source: Counter() for source in ('local', 'remote')}
        self.errors = Counter()
        self.recent = deque(maxlen=61)

    def record(self, job, error=None, source='local'):
        stage = job['stage']
        if stage not in STAGES or source not in self.completed:
            return
        if error:
            self.errors[source] += 1
            return
        self.completed[source][stage] += 1
        second = int(self.clock())
        if not self.recent or self.recent[-1][0] != second:
            self.recent.append((second, Counter()))
        self.recent[-1][1][source, stage] += 1

    def snapshot(self):
        cutoff = int(self.clock()) - 60
        while self.recent and self.recent[0][0] <= cutoff:
            self.recent.popleft()
        window = Counter()
        for _, counts in self.recent:
            window.update(counts)
        return {source: dict(completed=dict(self.completed[source]),
                    rates={stage: window[source, stage] for stage in STAGES},
                    total=sum(self.completed[source].values()),
                    rate=sum(window[source, stage] for stage in STAGES),
                    errors=self.errors[source]) for source in self.completed}
