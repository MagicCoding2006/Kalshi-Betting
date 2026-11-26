"""Small cross-platform nonblocking file lock for local NBA workers."""
import os


def lock_nonblocking(stream):
    """Lock the first byte of an open file or raise BlockingIOError."""
    if os.name == 'nt':
        import msvcrt
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write('\0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Worker lock is already held') from exc
        return

    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
