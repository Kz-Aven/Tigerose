"""User-reviewed memory maintenance endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.db.connection import get_connection
from server.db import repos
from server.runtime import memory_jobs as jobs

def _require_assistant(template_id: str):
    if not repos.get_template(template_id):
        raise HTTPException(404, 'assistant not found')


router = APIRouter(prefix='/api/assistants/{template_id}', tags=['memory-maintenance'],
                   dependencies=[Depends(_require_assistant)])


class Scope(BaseModel):
    scope_kind: str = 'assistant'
    scope_id: str | None = None


class Organize(Scope):
    memory_ids: list[str] = Field(default_factory=list)


class ImportPreview(Scope):
    path: str


class ImportConfirm(BaseModel):
    preview_id: str


def _transaction(action):
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = action(conn)
        conn.commit()
        if isinstance(result, dict) and result.get('status') in ('conflict', 'cancelled'):
            raise HTTPException(409, result)
        return result
    except LookupError as exc:
        conn.rollback()
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        conn.rollback()
        raise HTTPException(400, 'Cannot read the selected memory directory') from exc
    finally:
        conn.close()


@router.get('/memory-jobs')
def list_jobs(template_id: str):
    return _transaction(lambda conn: jobs.list_jobs(conn, template_id))


@router.get('/memory-candidates')
def list_candidates(template_id: str):
    return _transaction(lambda conn: jobs.list_candidates(conn, template_id))


@router.post('/memory-candidates/{candidate_id}/confirm')
def confirm_candidate(template_id: str, candidate_id: str):
    return _transaction(lambda conn: jobs.decide_candidate(conn, template_id, candidate_id, confirm=True))


@router.post('/memory-candidates/{candidate_id}/dismiss')
def dismiss_candidate(template_id: str, candidate_id: str):
    return _transaction(lambda conn: jobs.decide_candidate(conn, template_id, candidate_id, confirm=False))


@router.post('/memory-jobs/{job_id}/retry')
def retry_job(template_id: str, job_id: str):
    return _transaction(lambda conn: jobs.retry_job(conn, template_id, job_id))


@router.post('/memories/organize')
def organize(template_id: str, request: Organize):
    return _transaction(lambda conn: jobs.organize(conn, template_id, request.scope_kind,
                        request.scope_id or template_id, request.memory_ids))


@router.post('/memories/import-preview')
def preview_import(template_id: str, request: ImportPreview):
    return _transaction(lambda conn: jobs.import_preview(conn, template_id, request.path,
                        request.scope_kind, request.scope_id or template_id))


@router.post('/memories/import-confirm')
def confirm_import(template_id: str, request: ImportConfirm):
    return _transaction(lambda conn: jobs.import_confirm(conn, template_id, request.preview_id))
