"""Self-healing repair for mojibake in server-side Hermes data.

Mirror of the Stash OS frontend repair (src/lib/persistedMojibake.ts):
the app's file-writing path once saved UTF-8 bytes read as Latin-1/cp1252, so
real punctuation ("—", "…", "→", "✓") got stored as double-encoded fragments
(an em dash as the visible text "â€""). The frontend cleans browser-persisted
data; this module cleans the SERVER side: chat transcripts in the state store
(messages table: content / reasoning / reasoning_content / api_content) and
memory/skill files on disk (*.md / *.json / *.yaml under ~/.hermes).

Safety (identical to the frontend):
  - every repair is marker-gated (only strings that actually look like
    mojibake are touched),
  - decoded bytes must be valid UTF-8 (fatal decode) AND fully clean,
  - layered corruption is stripped up to 4 passes,
  - legit cp1252 text (e.g. "Rosé") never matches the markers.

One-shot use:
    python mojibake_repair.py            # scan + repair, print a report

Programmatic use (e.g. from the gateway at boot):
    from mojibake_repair import run_server_mojibake_migration
    report = run_server_mojibake_migration()
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

# Markers of the corruption seen in this codebase: U+FFFD replacement chars,
# C1 control bytes (U+0080–U+009F), and the known cp1252 fragment pairs
# (â/Ã/Â followed by a UTF-8 continuation byte, plus the specific sequences
# the double-encoding produces, e.g. â€ for an em dash's E2 80 prefix).
_MARKER_RE = re.compile(
    "[\ufffd\u0080-\u009f]"
    "|\u00e2[\u0080-\u009f\u20ac]"
    "|\u00e2[\u0152\u0153\u0160\u0161\u0178\u201a\u201e\u2026\u2020\u2021\u2030\u2039\u203a]"
    "|\u00c3[\u0080-\u009f\u00a2\u00a9\u00a8\u00b1]"
    "|\u00c2[\u0080-\u009f\u00b7]"
)

# Columns in the messages table that carry user-visible prose.
_MESSAGE_TEXT_COLUMNS = ("content", "reasoning", "reasoning_content", "api_content")

# File extensions worth scanning under the Hermes home directory.
_FILE_EXTENSIONS = (".md", ".json", ".yaml", ".yml", ".txt")

# Directories that are pure caches / volatile state — never touch.
_SKIP_DIRS = {"audio_cache", "cache", "node_modules", "__pycache__", "workspace", "backups"}

# Files larger than this are skipped (a transcript of that size is either a
# binary accident or not worth a synchronous repair pass).
_MAX_FILE_BYTES = 2_000_000

# Files whose CONTENT intentionally documents mojibake (e.g. a skill teaching
# how to filter "�" Whisper tokens). Inside a fenced code block or an inline
# code span those markers are sample text — the repair skips the file if the
# only hits are inside code fences.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


@dataclass
class RepairReport:
    repaired_strings: int = 0
    repaired_rows: int = 0
    repaired_files: int = 0
    scanned_rows: int = 0
    scanned_files: int = 0
    details: list[str] = field(default_factory=list)

    def as_line(self) -> str:
        return (
            f"[mojibake] server repair: {self.repaired_strings} string(s) in "
            f"{self.repaired_rows} message row(s), {self.repaired_files} file(s); "
            f"scanned {self.scanned_rows} row(s), {self.scanned_files} file(s)"
        )


def _has_marker(text: str) -> bool:
    return bool(_MARKER_RE.search(text))


def _decode_layer(text: str) -> str | None:
    """Decode one cp1252 layer. None when the text isn't pure cp1252 bytes or
    the bytes aren't valid UTF-8 (so legit single-byte text is untouched).

    cp1252, not latin-1: the classic fragment form contains U+20AC (€),
    U+201C ("), U+201A (‚) — cp1252-only chars that latin-1 can't encode,
    and latin-1 is identical to cp1252 everywhere else that matters."""
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError:
        return None  # chars outside cp1252 — real multi-byte text, not this form
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None  # invalid UTF-8 — not mojibake


def repair_string(raw: str) -> str | None:
    """Repair a single string. Returns the cleaned string, or None if untouched."""
    if not raw or not _has_marker(raw):
        return None
    # BOM guard: U+FEFF is real UTF-8 (not corruption) but blocks the cp1252
    # re-encode — peel it off, repair the body, and put it back.
    prefix = ""
    cur = raw
    if cur.startswith("\ufeff"):
        prefix = "\ufeff"
        cur = cur[1:]
    changed = False
    for _ in range(4):  # some values went through the corruption twice
        if not _has_marker(cur):
            break
        dec = _decode_layer(cur)
        if dec is None or dec == cur:
            break
        cur = dec
        changed = True
    # Targeted pass for fragments a clean decode can never fix, and for
    # marker-holding lines whose decode fails because of OTHER junk on the
    # same line. Only unambiguous E2-triple fragment forms are rewritten,
    # only when markers are still present (a clean string is never touched):
    #   â€– â€" (E2 80 xx) — 20xx-plane dashes; â€¦ ellipsis
    #   â†" (E2 86 22)   — arrows-plane glyph whose tail became "; prose
    #                     usage here is "points to" (→)
    #   â†³ (E2 86 B3)   — ↳ bullet; âœ“ (E2 9C 93) — checkmark
    # None of these three-char sequences can occur in legitimate prose, so
    # literal replacement cannot corrupt clean text.
    if _has_marker(cur):
        targeted = cur
        for frag, fix in (
            ("\u00e2\u20ac\u0022", "\u2014"),   # â€" straightened tail -> em dash
            ("\u00e2\u2020\u0022", "\u2192"),   # â†" straightened tail -> →
            ("\u00e2\u20ac\u201c", "\u2013"),   # â€" -> en dash
            ("\u00e2\u20ac\u201d", "\u2014"),   # â€" -> em dash
            ("\u00e2\u20ac\u00a6", "\u2026"),   # â€¦ -> ellipsis
            ("\u00e2\u2020\u00b3", "\u21b3"),   # â†³ -> ↳
            ("\u00e2\u0153\u201c", "\u2713"),   # âœ" -> ✓
        ):
            targeted = targeted.replace(frag, fix)
        if targeted != cur:
            cur = targeted
            changed = True
    if not changed:
        return None
    if _has_marker(cur) or "\ufffd" in cur:
        return None  # only accept a fully clean result
    return prefix + cur


def _markers_outside_code_fences(text: str) -> bool:
    """True when marker hits exist OUTSIDE fenced code blocks (fenced markers
    are sample text — e.g. a skill documenting how to filter � tokens)."""
    stripped = _CODE_FENCE_RE.sub("", text)
    return _has_marker(stripped)


def repair_file_text(text: str) -> str | None:
    """Line-level repair for whole documents.

    Documents mix clean UTF-8 and corrupted fragments, so whole-string repair
    bails the moment one real multi-byte char blocks the cp1252 re-encode.
    Repairing line-by-line sidesteps that — cp1252 fragments never span a
    newline. Lines with intentional markers (a skill documenting how to filter
    \ufffd Whisper tokens) are naturally protected: U+FFFD and sample glyphs
    like ♪ cannot be encoded to cp1252, so their lines never decode. The
    file-level gate still skips files whose corruption lives only inside code
    fences (pure sample text). Returns the new document, or None when nothing
    changed."""
    if not _markers_outside_code_fences(text):
        return None
    out_lines: list[str] = []
    changed = False
    for line in text.split("\n"):
        fixed = repair_string(line)
        if fixed is not None:
            out_lines.append(fixed)
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        return None
    return "\n".join(out_lines)


def repair_db_text(value: str | None) -> tuple[str | None, bool]:
    """Repair one DB text value. Returns (new_value, changed); new_value is the
    original when untouched. Layered, marker-gated, all-or-nothing."""
    if not value:
        return value, False
    fixed = repair_string(value)
    if fixed is None:
        return value, False
    return fixed, True


# ---- state store (SQLite mirror + optional Postgres) -----------------------


def _hermes_home() -> str:
    """Resolve the active Hermes home directory.

    Mirrors ``hermes_constants.get_hermes_home()``: the ``HERMES_HOME`` env
    var wins, then the platform-native default (``~/.hermes`` on POSIX,
    ``%LOCALAPPDATA%\\hermes`` on Windows). Falls back to a local copy of
    the platform logic when the hermes package is not importable (the
    standalone CLI entry point must keep working). Never hardcode
    ``~/.hermes`` — on native Windows that is NOT where Hermes lives.
    """
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return env
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        pass
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return os.path.join(base, "hermes")
    return os.path.join(os.path.expanduser("~"), ".hermes")


def _iter_state_db_paths() -> list[str]:
    home = _hermes_home()
    paths = []
    for name in ("state.db", "kanban.db", "response_store.db"):
        p = os.path.join(home, name)
        if os.path.exists(p):
            paths.append(p)
    return paths


def repair_sqlite_message_rows(db_path: str, report: RepairReport) -> None:
    """Repair text columns in the `messages` table of one SQLite database.
    Only marker-gated values are rewritten; everything else is left byte-exact."""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        if cur.fetchone() is None:
            conn.close()
            return
        for col in _MESSAGE_TEXT_COLUMNS:
            cur.execute(f"SELECT id, {col} FROM messages WHERE {col} IS NOT NULL")
            rows = cur.fetchall()
            report.scanned_rows += len(rows)
            updates: list[tuple[str, int]] = []
            for row_id, value in rows:
                fixed, changed = repair_db_text(value)
                if changed:
                    updates.append((fixed, row_id))
            if updates:
                cur.executemany(
                    f"UPDATE messages SET {col} = ? WHERE id = ?",
                    updates,
                )
                report.repaired_rows += len(updates)
                report.repaired_strings += len(updates)
                report.details.append(f"{os.path.basename(db_path)}:{col}: {len(updates)} row(s)")
        conn.commit()
        conn.close()
    except Exception as exc:  # never break boot over a repair
        report.details.append(f"{db_path}: skipped ({exc})")


def repair_postgres_message_rows(url: str, report: RepairReport) -> None:
    """Repair the Neon/Postgres mirror of the messages table when reachable.
    Best-effort: an unreachable or unauthenticated store is reported, not fatal."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return  # driver not installed — the SQLite pass still ran
    try:
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='messages'"
        )
        cols = {r[0] for r in cur.fetchall()}
        for col in _MESSAGE_TEXT_COLUMNS:
            if col not in cols:
                continue
            cur.execute(f"SELECT id, {col} FROM messages WHERE {col} IS NOT NULL")
            rows = cur.fetchall()
            report.scanned_rows += len(rows)
            updates: list[tuple[str, int]] = []
            for row_id, value in rows:
                fixed, changed = repair_db_text(value)
                if changed:
                    updates.append((fixed, row_id))
            if updates:
                cur.executemany(
                    f"UPDATE messages SET {col} = %s WHERE id = %s",
                    updates,
                )
                report.repaired_rows += len(updates)
                report.repaired_strings += len(updates)
                report.details.append(f"postgres:{col}: {len(updates)} row(s)")
        conn.commit()
        conn.close()
    except Exception as exc:
        report.details.append(f"postgres: skipped ({exc})")


def _postgres_url_from_env() -> str | None:
    env_path = os.path.join(_hermes_home(), ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("HERMES_POSTGRES_URL="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.environ.get("HERMES_POSTGRES_URL")


# ---- memory / skill files ---------------------------------------------------


def repair_memory_files(report: RepairReport) -> None:
    """Repair marker-gated text files under the Hermes home (memory, skills,
    config docs). Files whose marker hits live only inside code fences are left
    alone (the markers are documentation, not corruption)."""
    home = _hermes_home()
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(_FILE_EXTENSIONS):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > _MAX_FILE_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="strict") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue  # unreadable or not UTF-8 — not our corruption form
            report.scanned_files += 1
            if not _markers_outside_code_fences(text):
                continue
            fixed = repair_file_text(text)
            if fixed is None:
                continue
            try:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(fixed)
                report.repaired_files += 1
                report.details.append(f"file: {path}")
            except OSError as exc:
                report.details.append(f"file: {path}: write failed ({exc})")


def run_server_mojibake_migration() -> RepairReport:
    """One-time server-side cleanup: state DBs (SQLite + Postgres mirror) and
    memory/skill files. Idempotent and marker-gated — a clean store costs one
    scan pass."""
    report = RepairReport()
    for db_path in _iter_state_db_paths():
        repair_sqlite_message_rows(db_path, report)
    url = _postgres_url_from_env()
    if url:
        repair_postgres_message_rows(url, report)
    repair_memory_files(report)
    if report.repaired_strings or report.repaired_files:
        print(report.as_line(), file=sys.stderr)
        for detail in report.details[:10]:
            print(f"  - {detail}", file=sys.stderr)
    return report


if __name__ == "__main__":
    rep = run_server_mojibake_migration()
    print(rep.as_line())
    for detail in rep.details:
        print(f"  - {detail}")
