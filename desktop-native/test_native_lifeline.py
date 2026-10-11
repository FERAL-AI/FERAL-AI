"""Exercise real process groups and parent-pipe EOF without starting a backend."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest


class NativeLifelineTests(unittest.TestCase):
    def test_parent_pipe_closure_terminates_owned_backend_group(self):
        source = Path(__file__).resolve().parent
        code = (
            'import os,time; from native_backend_launcher import prepare_native_child; '
            'prepare_native_child(); print(str(os.getpid())+":"+str(os.getpgrp()),flush=True); time.sleep(300)'
        )
        child = subprocess.Popen([sys.executable, '-c', code], cwd=source, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            pid, group = map(int, child.stdout.readline().strip().split(':'))
            self.assertEqual(pid, child.pid)
            self.assertEqual(group, child.pid)
            child.stdin.close()
            child.wait(timeout=5)
            self.assertEqual(child.returncode, -signal.SIGTERM)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

    def test_live_parent_pipe_does_not_terminate_child(self):
        source = Path(__file__).resolve().parent
        code = 'import time; from native_backend_launcher import prepare_native_child; prepare_native_child(); print("ready",flush=True); time.sleep(0.3); print("still alive",flush=True)'
        child = subprocess.Popen([sys.executable, '-c', code], cwd=source, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.assertEqual(child.stdout.readline().strip(), 'still alive')
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()


if __name__ == '__main__':
    unittest.main()
