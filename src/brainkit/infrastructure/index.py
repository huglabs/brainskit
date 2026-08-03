from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from brainkit.application.ports import VaultPort
from brainkit.domain.model import SearchHit, SourceRecord, ValidationError, utc_now

#: path, kind, title, body, content_hash — the FTS5 row shape.
_Document = tuple[str, str, str, str, str]


class SqliteFtsIndex:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def rebuild(self, vault: VaultPort) -> int:
        return self.rebuild_snapshot(vault, vault.registry())

    def rebuild_snapshot(
        self, vault: VaultPort, records: dict[str, SourceRecord]
    ) -> int:
        """Rebuild the whole index, holding one document in memory at a time.

        The bodies are streamed into `executemany` rather than collected first:
        a vault is allowed to hold gigabytes of sources, and materialising every
        extracted body in one list made the peak memory of a rebuild scale with
        the size of the corpus rather than with its largest document.

        The trade is that file reads now happen inside the write transaction,
        so a rebuild holds the SQLite write lock for its whole duration. That is
        the right way round here: WAL means readers are never blocked, the only
        thing excluded is a concurrent write, and a rebuild racing a write was
        already a contended operation.
        """

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        counter = _Counter()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            connection.execute("DELETE FROM search_fts")
            connection.executemany(
                """
                INSERT INTO search_fts(path, kind, title, body, content_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                counter.wrap(_iter_documents(vault, records)),
            )
            self._touch_metadata(connection)
            connection.commit()
        return counter.value

    def upsert_raw(self, vault: VaultPort, record: SourceRecord) -> None:
        body = vault.raw_text(record)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            connection.execute(
                "DELETE FROM search_fts WHERE path = ? OR content_hash = ?",
                (record.path, record.content_hash),
            )
            connection.execute(
                """
                INSERT INTO search_fts(path, kind, title, body, content_hash)
                VALUES (?, 'raw', ?, ?, ?)
                """,
                (record.path, record.original_name, body, record.content_hash),
            )
            self._touch_metadata(connection)
            connection.commit()

    def upsert_wiki(self, vault: VaultPort, paths: Sequence[str]) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            for path in paths:
                body = vault.read_text(path)
                title = _markdown_title(body) or PurePosixPath(path).stem
                connection.execute("DELETE FROM search_fts WHERE path = ?", (path,))
                connection.execute(
                    """
                    INSERT INTO search_fts(path, kind, title, body, content_hash)
                    VALUES (?, 'wiki', ?, ?, '')
                    """,
                    (path, title, body),
                )
            self._touch_metadata(connection)
            connection.commit()

    def apply_snapshot(
        self,
        vault: VaultPort,
        records: dict[str, SourceRecord],
        wiki_paths: Sequence[str],
        raw_content_hash: str | None,
    ) -> int:
        if not self.database_path.exists():
            return self.rebuild_snapshot(vault, records)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            for path in wiki_paths:
                body = vault.read_text(path)
                title = _markdown_title(body) or PurePosixPath(path).stem
                connection.execute("DELETE FROM search_fts WHERE path = ?", (path,))
                connection.execute(
                    """
                    INSERT INTO search_fts(path, kind, title, body, content_hash)
                    VALUES (?, 'wiki', ?, ?, '')
                    """,
                    (path, title, body),
                )
            if raw_content_hash:
                record = records[raw_content_hash]
                body = vault.raw_text(record)
                connection.execute(
                    "DELETE FROM search_fts WHERE content_hash = ?",
                    (raw_content_hash,),
                )
                connection.execute(
                    """
                    INSERT INTO search_fts(path, kind, title, body, content_hash)
                    VALUES (?, 'raw', ?, ?, ?)
                    """,
                    (record.path, record.original_name, body, record.content_hash),
                )
            self._touch_metadata(connection)
            count = int(
                connection.execute("SELECT count(*) FROM search_fts").fetchone()[0]
            )
            connection.commit()
        return count

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        match_query = _fts_query(query)
        if not self.database_path.exists():
            return []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT
                        path,
                        kind,
                        title,
                        snippet(search_fts, 3, '⟦', '⟧', ' … ', 28) AS excerpt,
                        -bm25(search_fts, 2.0, 1.0) AS score,
                        NULLIF(content_hash, '') AS content_hash
                    FROM search_fts
                    WHERE search_fts MATCH ?
                    ORDER BY bm25(search_fts, 2.0, 1.0)
                    LIMIT ?
                    """,
                    (match_query, max(1, min(limit, 100))),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise ValidationError(
                "Invalid full-text search query", details={"query": query}
            ) from exc
        return [
            SearchHit(
                path=row["path"],
                kind=row["kind"],
                title=row["title"],
                excerpt=row["excerpt"],
                score=float(row["score"]),
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"documents": 0, "updated_at": None}
        try:
            with closing(self._connect()) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM search_fts"
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT value FROM index_metadata WHERE key = 'updated_at'"
                ).fetchone()
        except sqlite3.OperationalError:
            return {"documents": 0, "updated_at": None}
        return {"documents": count, "updated_at": row[0] if row else None}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                    path UNINDEXED,
                    kind UNINDEXED,
                    title,
                    body,
                    content_hash UNINDEXED,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )
        except sqlite3.OperationalError as exc:
            raise ValidationError(
                "This Python/SQLite build does not include FTS5"
            ) from exc
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _touch_metadata(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO index_metadata(key, value) VALUES('updated_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (utc_now(),),
        )


class _Counter:
    """Counts rows as they stream past, since a generator has no length."""

    def __init__(self) -> None:
        self.value = 0

    def wrap(self, rows: Iterator[_Document]) -> Iterator[_Document]:
        for row in rows:
            self.value += 1
            yield row


def _iter_documents(
    vault: VaultPort, records: dict[str, SourceRecord]
) -> Iterator[_Document]:
    """Yield every indexable document without holding more than one at a time."""

    records_by_path = {record.path: record for record in records.values()}
    for path in vault.raw_files():
        record = records_by_path.get(path)
        if not record:
            continue
        yield (
            path,
            "raw",
            record.original_name,
            vault.raw_text(record),
            record.content_hash,
        )
    for path in vault.wiki_pages():
        body = vault.read_text(path)
        title = _markdown_title(body) or PurePosixPath(path).stem
        yield (path, "wiki", title, body, "")


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    if not tokens:
        raise ValidationError("Search query has no searchable terms")
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _markdown_title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None
