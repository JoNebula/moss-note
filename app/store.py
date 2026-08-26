from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class NoteStore:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    media_type TEXT,
                    source_path TEXT NOT NULL,
                    normalized_path TEXT,
                    status TEXT NOT NULL,
                    duration REAL,
                    chunk_count INTEGER NOT NULL DEFAULT 1,
                    processed_chunks INTEGER NOT NULL DEFAULT 0,
                    language TEXT,
                    hotwords TEXT,
                    raw_transcript TEXT,
                    raw_segments_json TEXT NOT NULL DEFAULT '[]',
                    segments_json TEXT NOT NULL DEFAULT '[]',
                    correction_source_segments_json TEXT NOT NULL DEFAULT '[]',
                    corrected_segments_json TEXT NOT NULL DEFAULT '[]',
                    correction_partial_json TEXT NOT NULL DEFAULT '{}',
                    correction_status TEXT NOT NULL DEFAULT 'idle',
                    correction_error TEXT,
                    correction_total_windows INTEGER NOT NULL DEFAULT 0,
                    correction_processed_windows INTEGER NOT NULL DEFAULT 0,
                    correction_changes INTEGER NOT NULL DEFAULT 0,
                    correction_model TEXT,
                    speaker_names_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(notes)").fetchall()
            }
            if "chunk_count" not in columns:
                connection.execute(
                    "ALTER TABLE notes ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 1"
                )
            if "processed_chunks" not in columns:
                connection.execute(
                    "ALTER TABLE notes ADD COLUMN processed_chunks INTEGER NOT NULL DEFAULT 0"
                )
            migrations = {
                "raw_segments_json": "TEXT NOT NULL DEFAULT '[]'",
                "correction_source_segments_json": "TEXT NOT NULL DEFAULT '[]'",
                "corrected_segments_json": "TEXT NOT NULL DEFAULT '[]'",
                "correction_partial_json": "TEXT NOT NULL DEFAULT '{}'",
                "correction_status": "TEXT NOT NULL DEFAULT 'idle'",
                "correction_error": "TEXT",
                "correction_total_windows": "INTEGER NOT NULL DEFAULT 0",
                "correction_processed_windows": "INTEGER NOT NULL DEFAULT 0",
                "correction_changes": "INTEGER NOT NULL DEFAULT 0",
                "correction_model": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE notes ADD COLUMN {name} {definition}"
                    )
            # Preserve the ASR result of existing notes before future manual/AI edits.
            connection.execute(
                """
                UPDATE notes SET raw_segments_json = segments_json
                WHERE status = 'done'
                  AND (raw_segments_json IS NULL OR raw_segments_json = '[]')
                """
            )

    def create(self, note: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        values = {
            "normalized_path": None,
            "duration": None,
            "chunk_count": 1,
            "processed_chunks": 0,
            "raw_transcript": None,
            "raw_segments_json": "[]",
            "segments_json": "[]",
            "correction_source_segments_json": "[]",
            "corrected_segments_json": "[]",
            "correction_partial_json": "{}",
            "correction_status": "idle",
            "correction_error": None,
            "correction_total_windows": 0,
            "correction_processed_windows": 0,
            "correction_changes": 0,
            "correction_model": None,
            "speaker_names_json": "{}",
            "error": None,
            "created_at": now,
            "updated_at": now,
            **note,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO notes ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return self.get(note["id"])

    def get(self, note_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return self._deserialize(row)

    def list(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notes ORDER BY created_at DESC"
            ).fetchall()
        return [self._deserialize(row) for row in rows]

    def pending_ids(self) -> list[str]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM notes WHERE status IN ('queued', 'processing') ORDER BY created_at"
            ).fetchall()
            connection.execute(
                "UPDATE notes SET status = 'queued' WHERE status = 'processing'"
            )
        return [row["id"] for row in rows]

    def pending_correction_ids(self) -> list[str]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM notes
                WHERE correction_status IN ('queued', 'processing')
                ORDER BY updated_at
                """
            ).fetchall()
            connection.execute(
                """
                UPDATE notes SET correction_status = 'queued'
                WHERE correction_status = 'processing'
                """
            )
        return [row["id"] for row in rows]

    def update(self, note_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            return self.get(note_id)
        json_fields = {
            "raw_segments": "raw_segments_json",
            "segments": "segments_json",
            "correction_source_segments": "correction_source_segments_json",
            "corrected_segments": "corrected_segments_json",
            "correction_partial": "correction_partial_json",
            "speaker_names": "speaker_names_json",
        }
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            target = json_fields.get(key, key)
            normalized[target] = (
                json.dumps(value, ensure_ascii=False) if key in json_fields else value
            )
        normalized["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self.lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE notes SET {assignments} WHERE id = ?",
                (*normalized.values(), note_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)
        return self.get(note_id)

    def delete(self, note_id: str) -> dict[str, Any]:
        note = self.get(note_id)
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return note

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["raw_segments"] = json.loads(result.pop("raw_segments_json") or "[]")
        result["segments"] = json.loads(result.pop("segments_json") or "[]")
        result["correction_source_segments"] = json.loads(
            result.pop("correction_source_segments_json") or "[]"
        )
        result["corrected_segments"] = json.loads(
            result.pop("corrected_segments_json") or "[]"
        )
        result["correction_partial"] = json.loads(
            result.pop("correction_partial_json") or "{}"
        )
        result["speaker_names"] = json.loads(result.pop("speaker_names_json") or "{}")
        return result
