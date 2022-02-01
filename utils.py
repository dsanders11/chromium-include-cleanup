import os


def get_worker_count():
    return len(os.sched_getaffinity(0))
