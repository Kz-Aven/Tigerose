"""Bind accepted runs to a durable session generation."""

from server.runtime.session_history import current_epoch


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS session_run_fences (
        run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL)""")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_runs'").fetchone():
        conn.execute("""INSERT OR IGNORE INTO session_fences(session_id)
            SELECT DISTINCT session_id FROM runtime_runs WHERE session_id != ''""")
        conn.execute("""INSERT OR IGNORE INTO session_run_fences(run_id,session_id,epoch)
            SELECT r.run_id,r.session_id,f.epoch FROM runtime_runs r
            JOIN session_fences f ON f.session_id=r.session_id WHERE f.deleted_at IS NULL""")


def bind(conn, run_id, session_id):
    epoch = current_epoch(conn, session_id)
    conn.execute("INSERT OR IGNORE INTO session_run_fences VALUES (?, ?, ?)",
                 (run_id, session_id, epoch))
    validate(conn, run_id, session_id)
    return epoch


def validate(conn, run_id, session_id):
    epoch = current_epoch(conn, session_id)
    row = conn.execute("SELECT session_id, epoch FROM session_run_fences WHERE run_id=?", (run_id,)).fetchone()
    if row is None and epoch != 0:
        raise RuntimeError("run is not bound to the current session generation")
    if row and (row[0] != session_id or row[1] != epoch):
        raise RuntimeError("session generation changed")
    return epoch
