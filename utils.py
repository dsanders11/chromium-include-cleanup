import multiprocessing
import os


def get_worker_count():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return multiprocessing.cpu_count()
