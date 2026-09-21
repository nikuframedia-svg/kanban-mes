"""A process-lifetime lock shared by Windows web workers and the recovery CLI."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def single_worker(conn):
    database = next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main')
    lock_path = Path(database + '.header-recovery.lock')
    with lock_path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Já existe uma recuperação de cabeçalho em curso.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
