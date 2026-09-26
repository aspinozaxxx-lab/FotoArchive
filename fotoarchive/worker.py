"""Lightweight spawn target: inference dependencies belong in the child process."""


def worker_main(*args):
    from .engine import worker_main as run
    run(*args)
