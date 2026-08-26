"""P3a: WAL journal-mode helper that degrades gracefully on filesystems
without shared-memory support (e.g. WSL drvfs /mnt/*), where WAL raises
"disk I/O error" and bricks every later read of the database.

Single-machine workload: DELETE journal mode is functionally equivalent,
just without concurrent-reader overlap. Prefer WAL where it works.
"""
import sqlite3

_WAL_UNSUPPORTED_MARKERS = ("disk i/o error", "unable to open database")


def set_journal_mode(conn, mode="WAL"):
    """Set journal mode; fall back to DELETE when the FS can't do WAL.

    Returns the effective mode string ("wal" or "delete").
    """
    try:
        row = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        return (row[0] if row else "").lower()
    except sqlite3.OperationalError as exc:
        low = str(exc).lower()
        if any(m in low for m in _WAL_UNSUPPORTED_MARKERS) and mode.upper() == "WAL":
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                return "delete"
            except sqlite3.OperationalError:
                # Both WAL and DELETE fail (e.g. drvfs with WAL held by Windows process).
                # Return whatever the connection defaulted to.
                try:
                    row = conn.execute("PRAGMA journal_mode").fetchone()
                    return (row[0] if row else "delete").lower()
                except Exception:
                    return "delete"
        raise
