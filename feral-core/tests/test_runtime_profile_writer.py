"""Kernel/process exclusion against disposable roots, never personal runtime."""
import os
import signal
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from security.profile_writer import (
    RuntimeProfileBusy, RuntimeProfileOwnerChanged, RuntimeProfileUnsafe,
    acquire_runtime_profile,
)


CORE = Path(__file__).parents[1]



def environment(root, port=9911):
    return {**os.environ, 'FERAL_HOME': str(root), 'FERAL_DATA_HOME':str(root),
        'FERAL_NATIVE_DEFER_VAULT':'1', 'FERAL_PORT':str(port),
        'PYTHONPATH':str(CORE)+os.pathsep+os.environ.get('PYTHONPATH',''), 'PYTHONDONTWRITEBYTECODE':'1'}


def contender(root, port=9911, data_root=None):
    code = '''from security.profile_writer import acquire_runtime_profile, RuntimeProfileBusy
import sys
try:
    lease = acquire_runtime_profile(sys.argv[1], sys.argv[2])
    print("admitted", flush=True)
    lease.release()
except RuntimeProfileBusy:
    print("busy", flush=True)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(root), str(data_root or root)],
        cwd=CORE, env=environment(root, port), capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_same_root_other_port_is_excluded_and_release_keeps_stable_inode(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root, root)
    lock = root/'.runtime-writer.lock'
    inode = lock.stat().st_ino
    try:
        assert contender(root, port=9912) == 'busy'
        assert lock.read_bytes() == b''  # No secret/PID takeover metadata.
    finally:
        lease.release()
    assert lock.stat().st_ino == inode and contender(root) == 'admitted'


def test_same_process_reentrance_does_not_release_outer_owner(tmp_path):
    root = tmp_path/'profile'
    outer = acquire_runtime_profile(root, root)
    inner = acquire_runtime_profile(root, root)
    inner.release()
    inner.release()
    assert contender(root) == 'busy'
    outer.release()
    assert contender(root) == 'admitted'


def test_separate_roots_and_partially_overlapping_layouts(tmp_path):
    a,b,c = (tmp_path/name for name in ('a','b','c'))
    lease = acquire_runtime_profile(a,b)
    try:
        assert contender(c) == 'admitted'
        assert contender(a, data_root=c) == 'busy'
        assert contender(c, data_root=b) == 'busy'
        # Failed multi-root admission releases its earlier non-shared root.
        assert contender(c) == 'admitted'
    finally:
        lease.release()


def test_interrupted_multi_root_acquisition_releases_earlier_root(tmp_path, monkeypatch):
    from security import profile_writer
    a,b = tmp_path/'a', tmp_path/'b'
    actual_open = profile_writer._open_root
    def interrupted(path):
        if path == b:
            raise KeyboardInterrupt()
        return actual_open(path)
    monkeypatch.setattr(profile_writer, '_open_root', interrupted)
    with pytest.raises(KeyboardInterrupt):
        acquire_runtime_profile(a,b)
    assert contender(a) == 'admitted'


def test_symlink_alias_contends_on_same_directory(tmp_path):
    real, alias = tmp_path/'real', tmp_path/'alias'
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    lease = acquire_runtime_profile(real, real)
    try:
        assert contender(alias) == 'busy'
    finally:
        lease.release()


@pytest.mark.parametrize('kind', ['symlink','directory','permissions','hardlink','fifo'])
def test_malformed_lock_never_modifies_foreign_artifact(tmp_path, kind):
    root = tmp_path/'profile'
    root.mkdir()
    lock, foreign = root/'.runtime-writer.lock', tmp_path/'foreign'
    foreign.write_bytes(b'private-fixture-canary')
    if kind == 'symlink':
        lock.symlink_to(foreign)
    elif kind == 'directory':
        lock.mkdir()
    elif kind == 'permissions':
        lock.write_bytes(b'unchanged')
        lock.chmod(0o644)
    elif kind == 'fifo':
        os.mkfifo(lock, 0o600)
    else:
        foreign.chmod(0o600)
        os.link(foreign, lock)
    with pytest.raises(RuntimeProfileUnsafe):
        acquire_runtime_profile(root, root)
    assert foreign.read_bytes() == b'private-fixture-canary'


def test_replaced_lock_inode_refuses_reentrant_admission(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root, root)
    lock = root/'.runtime-writer.lock'
    lock.rename(root/'moved-lock')
    lock.touch(mode=0o600)
    with pytest.raises(RuntimeProfileUnsafe):
        acquire_runtime_profile(root, root)
    # Restore identity solely in this disposable test, then release.
    lock.unlink()
    (root/'moved-lock').rename(lock)
    lease.release()


def test_runtime_lease_descriptors_are_close_on_exec(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root, root)
    try:
        assert all(not os.get_inheritable(fd) for held in lease._roots for fd in (held.fd,held.directory))
        assert contender(root) == 'busy'
    finally:
        lease.release()


@pytest.mark.parametrize('termination', [signal.SIGTERM, signal.SIGKILL])
def test_os_releases_lease_after_actual_owner_crash(tmp_path, termination):
    root = tmp_path/'profile'
    child = subprocess.Popen([sys.executable, '-B', '-c',
        'from security.profile_writer import acquire_runtime_profile; import sys; acquire_runtime_profile(); print("held",flush=True); sys.stdin.read()'],
        cwd=CORE, env=environment(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'held'
        assert contender(root) == 'busy'
        child.send_signal(termination)
        child.wait(timeout=5)
        assert contender(root) == 'admitted'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_fork_child_cannot_unlock_parent_and_does_not_retain_after_parent_release(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root, root)
    reader, writer = os.pipe()
    continue_reader, continue_writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        os.close(continue_writer)
        try:
            try:
                lease.release()
                answer = 'bad'
            except RuntimeProfileOwnerChanged:
                answer = 'fenced'
            os.write(writer, answer.encode())
            os.read(continue_reader, 1)
        finally:
            os._exit(0)
    os.close(writer)
    os.close(continue_reader)
    try:
        assert os.read(reader, 16) == b'fenced'
        assert contender(root) == 'busy'
        lease.release()
        # Child is still alive: its inherited fd must not retain the lock.
        assert contender(root) == 'admitted'
    finally:
        os.close(continue_writer)
        os.close(reader)
        os.waitpid(child, 0)
        lease.release()


def test_two_actual_processes_race_exactly_one_admission(tmp_path):
    root = tmp_path/'profile'
    code = '''from security.profile_writer import acquire_runtime_profile, RuntimeProfileBusy
import sys
sys.stdin.readline()
try:
    acquire_runtime_profile()
    print("admitted",flush=True)
    sys.stdin.readline()
except RuntimeProfileBusy:
    print("busy",flush=True)
'''
    children = [subprocess.Popen([sys.executable,'-B','-c',code], cwd=CORE, env=environment(root),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for child in children:
            child.stdin.write('go\n')
            child.stdin.flush()
        results = [child.stdout.readline().strip() for child in children]
        assert sorted(results) == ['admitted','busy']
    finally:
        for child in children:
            if child.poll() is None:
                child.stdin.write('stop\n')
                child.stdin.flush()
            child.wait(timeout=5)


def test_two_threads_reenter_one_process_owner(tmp_path):
    root = tmp_path/'profile'
    leases = []
    barrier = threading.Barrier(2)
    def acquire():
        barrier.wait()
        leases.append(acquire_runtime_profile(root,root))
    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(leases) == 2 and leases[0]._roots[0] is leases[1]._roots[0]
    for lease in leases:
        lease.release()
    assert contender(root) == 'admitted'


def test_api_import_busy_fails_before_importing_state_or_key_generation(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    try:
        code = '''import sys
from security.profile_writer import RuntimeProfileBusy
try:
    import api.server
except RuntimeProfileBusy:
    print("busy" if "api.state" not in sys.modules and "api.keys" not in sys.modules else "late")
'''
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=CORE, env=environment(root),capture_output=True,text=True,timeout=8)
        assert result.returncode == 0 and result.stdout.strip() == 'busy', result.stderr
        assert {p.name for p in root.iterdir()} == {'.runtime-writer.lock'}
    finally:
        lease.release()


def test_direct_cli_spawn_busy_fails_before_runtime_record_or_thread(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    try:
        code = '''from cli.main import _spawn_brain_server
from security.profile_writer import RuntimeProfileBusy
try:
    _spawn_brain_server("127.0.0.1",9912,{})
except RuntimeProfileBusy:
    print("busy")
'''
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=CORE, env=environment(root),capture_output=True,text=True,timeout=8)
        assert result.returncode == 0 and result.stdout.strip() == 'busy', result.stderr
        assert not (root/'runtime.json').exists()
    finally:
        lease.release()


def test_native_launcher_busy_never_reaches_server_import(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    path = CORE.parents[0]/'desktop-native/native_backend_launcher.py'
    try:
        result = subprocess.run([sys.executable,'-B',str(path)],cwd=CORE, env=environment(root),capture_output=True,text=True,timeout=8)
        assert result.returncode != 0 and 'RuntimeProfileBusy' in result.stderr
        assert 'Native backend process group:' not in result.stdout
        assert {p.name for p in root.iterdir()} == {'.runtime-writer.lock'}
    finally:
        lease.release()


def test_wrong_effective_owner_refuses_existing_directory(tmp_path, monkeypatch):
    from security import profile_writer
    root = tmp_path/'profile'
    root.mkdir()
    current = os.geteuid()
    monkeypatch.setattr(profile_writer.os, 'geteuid', lambda: current + 1)
    with pytest.raises(RuntimeProfileUnsafe):
        acquire_runtime_profile(root, root)
    assert not (root/'.runtime-writer.lock').exists()


def test_xdg_runtime_also_excludes_legacy_api_key_root(tmp_path, monkeypatch):
    home = tmp_path/'home'
    config, data = tmp_path/'config', tmp_path/'data'
    monkeypatch.delenv('FERAL_HOME', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config))
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    lease = acquire_runtime_profile()
    try:
        assert {root.path for root in lease._roots} == {config/'feral', data/'feral', home/'.feral'}
        assert contender(home/'.feral') == 'busy'
    finally:
        lease.release()


def test_directory_permissions_changed_after_admission_fence_owner(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    root.chmod(0o777)
    try:
        with pytest.raises(RuntimeProfileUnsafe):
            lease.assert_owner()
    finally:
        root.chmod(0o700)
        lease.release()


@pytest.mark.parametrize('entry', ['serve','start'])
def test_cli_busy_denies_before_tls_or_first_run_writes(tmp_path, entry):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    code = """import cli.main as main
from security.profile_writer import RuntimeProfileBusy
def forbidden(*args, **kwargs):
    raise AssertionError('profile-write preparation reached')
main._brain_ssl_kwargs = forbidden
main._is_first_run = forbidden
main.cmd_setup = forbidden
if main.httpx:
    main.httpx.get = lambda *args, **kwargs: type('Reply',(),{'status_code':503})()
try:
    main.cmd_serve(host='127.0.0.1',port=9912,tls=True) if ENTRY == 'serve' else main.cmd_start(port=9912,tls=True,foreground=True,no_browser=True)
except RuntimeProfileBusy:
    print('busy')
""".replace('ENTRY', repr(entry))
    try:
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=CORE,env=environment(root),capture_output=True,text=True,timeout=8)
        assert result.returncode == 0 and result.stdout.strip().endswith('busy'), result.stderr
        assert {p.name for p in root.iterdir()} == {'.runtime-writer.lock'}
    finally:
        lease.release()


def test_cli_healthy_client_branch_does_not_acquire_or_prepare_writes(tmp_path):
    root = tmp_path/'profile'
    lease = acquire_runtime_profile(root,root)
    code = """import cli.main as main
from security import profile_writer
def forbidden(*args, **kwargs):
    raise AssertionError('writer startup reached')
profile_writer.acquire_runtime_profile = forbidden
main._brain_ssl_kwargs = forbidden
main._is_first_run = forbidden
main.cmd_setup = forbidden
main.httpx.get = lambda *args, **kwargs: type('Reply',(),{'status_code':200})()
async def readonly_repl():
    print('client-only')
main.repl = readonly_repl
main.cmd_start(port=9912,tls=True,foreground=True,no_browser=True)
"""
    try:
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=CORE,env=environment(root),capture_output=True,text=True,timeout=8)
        assert result.returncode == 0 and result.stdout.strip().endswith('client-only'), result.stderr
        assert {p.name for p in root.iterdir()} == {'.runtime-writer.lock'}
    finally:
        lease.release()


def test_api_admission_source_precedes_global_state_and_lifespan_migrations():
    source = Path(os.environ.get('FERAL_PROFILE_SOURCE_UNDER_TEST',str(CORE/'api/server.py'))).read_text()
    admission = source.index('_RUNTIME_PROFILE_LEASE = acquire_runtime_profile()')
    assert admission < source.index('from api.state import')
    startup = source[source.index('async def startup():'):]
    assert startup.index('_RUNTIME_PROFILE_LEASE.assert_owner()') < startup.index('check_local_bypass_safety()')
