"""Contain the backend in an owned group and stop it if its native host exits."""
import os
import runpy
import signal
import threading


def prepare_native_child():
    # Foundation can already make the child a process-group leader.
    if os.getpgrp() != os.getpid():
        os.setsid()
    if os.getpgrp() != os.getpid():
        raise RuntimeError('The native backend could not create its own process group')

    def watch_lifeline():
        try:
            while os.read(0, 1):
                pass
        finally:
            # EOF means the host closed its writing end or exited. Only this
            # group's backend and its owned coding children receive the signal.
            os.killpg(os.getpgrp(), signal.SIGTERM)

    threading.Thread(target=watch_lifeline, name='native-host-lifeline', daemon=True).start()


if __name__ == '__main__':
    # Native startup never invokes an OS credential dialog. Stored secrets
    # require the explicitly reviewed coordinator unlock path.
    os.environ["FERAL_NATIVE_DEFER_VAULT"] = "1"
    # First chat must not silently download an embedding model. Cached local
    # models remain usable; a reviewed model installation can opt in separately.
    os.environ.setdefault("FERAL_EMBED_MODEL_CACHE_ONLY", "1")
    prepare_native_child()
    print('Native backend process group:', os.getpid(), os.getpgrp(), flush=True)
    runpy.run_module('api.server', run_name='__main__')
