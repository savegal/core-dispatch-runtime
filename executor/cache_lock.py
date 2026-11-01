"""Locks for synchronous compound operations on caches shared by worker threads."""
from functools import wraps


def synchronized(lock):
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            with lock:
                return function(*args, **kwargs)
        return guarded
    return decorate
