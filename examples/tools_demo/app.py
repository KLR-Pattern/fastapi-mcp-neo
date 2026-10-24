"""A tiny notes app for the tools-mode demo.

Enough surface to show what tools mode derives: typed models, an enum, a
body parameter, an optional query param, and one no-arg route.
"""

from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class NoteKind(str, Enum):
    note = "note"
    todo = "todo"


class NoteOut(BaseModel):
    id: int
    kind: NoteKind
    title: str
    body: str
    tags: list[str] = []


class NoteIn(BaseModel):
    title: str
    body: str
    kind: NoteKind = NoteKind.note
    tags: list[str] = []


_NOTES: dict[int, NoteOut] = {
    1: NoteOut(
        id=1, kind=NoteKind.note, title="First", body="Hello world", tags=["intro"]
    ),
    2: NoteOut(
        id=2, kind=NoteKind.todo, title="Ship 0.15.0", body="Merge the PR",
        tags=["release"],
    ),
}


def create_app() -> FastAPI:
    app = FastAPI(title="tools-demo")

    @app.get("/notes/{note_id}", tags=["notes"])
    async def get_note(note_id: int) -> NoteOut:
        """Fetch one note by id."""
        if note_id not in _NOTES:
            raise HTTPException(status_code=404, detail="no such note")
        return _NOTES[note_id]

    @app.get("/notes", tags=["notes"])
    async def list_notes(kind: NoteKind | None = None, limit: int = 10) -> list[NoteOut]:
        """List notes, optionally filtered by kind."""
        notes = list(_NOTES.values())
        if kind is not None:
            notes = [n for n in notes if n.kind is kind]
        return notes[:limit]

    @app.post("/notes", tags=["notes"])
    async def create_note(payload: NoteIn) -> NoteOut:
        """Create a note."""
        created = NoteOut(id=max(_NOTES, default=0) + 1, **payload.model_dump())
        _NOTES[created.id] = created
        return created

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        """Liveness probe."""
        return {"ok": True}

    return app
