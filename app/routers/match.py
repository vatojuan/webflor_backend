# app/routers/match.py
"""Motor de coincidencias (matching) entre ofertas y usuarios.
- Calcula similitud coseno sobre los embeddings almacenados en pgvector
- Inserta los resultados en `matches`
- Envía al candidato un e‑mail de notificación usando `send_match_email`
Todo se realiza con SQL crudo para máxima velocidad; no rompe ningún flujo existente.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np
from fastapi import APIRouter, HTTPException
from pgvector.psycopg2 import register_vector

from app.database import get_db_connection
from app.email_utils import send_match_email

# ───────────────── Configuración ─────────────────
router = APIRouter(prefix="/api/match", tags=["match"])
logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD: float = 0.80  # ≥ 0.80 = 80 % de similitud

# ───────────────── Utilidades ─────────────────

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Devuelve la similitud coseno entre dos vectores Python lists."""
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    if a_arr.size == 0 or b_arr.size == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr)))


def _fetch_embeddings(cur, table: str, id_col: str, vec_col: str) -> List[Tuple[int, List[float]]]:
    cur.execute(f'SELECT {id_col}, {vec_col} FROM "{table}" WHERE {vec_col} IS NOT NULL;')
    return cur.fetchall()


def _upsert_match(cur, job_id: int, user_id: int, score: float) -> None:
    cur.execute(
        """
        INSERT INTO matches (job_id, user_id, score, sent_at, status)
        VALUES (%s, %s, %s, NOW(), 'sent')
        ON CONFLICT (job_id, user_id) DO NOTHING;
        """,
        (job_id, user_id, score),
    )


def _notify_user(cur, job_id: int, user_id: int, score: float) -> None:
    cur.execute('SELECT email, name FROM "User" WHERE id = %s;', (user_id,))
    email, name = cur.fetchone()
    cur.execute('SELECT title, description FROM "Job" WHERE id = %s;', (job_id,))
    title, description = cur.fetchone()
    send_match_email(email, name, title, description, score)

# ───────────────── Núcleo de matching ─────────────────

def run_matching_for_job(job_id: int) -> int:
    """Compara una oferta contra todos los usuarios y envía notificaciones."""
    conn = get_db_connection(); register_vector(conn); cur = conn.cursor()
    try:
        cur.execute('SELECT embedding FROM "Job" WHERE id = %s;', (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Oferta no encontrada')
        job_vec = row[0]

        matches = 0
        for user_id, user_vec in _fetch_embeddings(cur, 'User', 'id', 'embedding'):
            score = cosine_similarity(job_vec, user_vec)
            if score >= SIMILARITY_THRESHOLD:
                _upsert_match(cur, job_id, user_id, score)
                _notify_user(cur, job_id, user_id, score)
                matches += 1
        conn.commit(); return matches
    except Exception as exc:
        conn.rollback(); logger.exception('Error matching oferta→usuarios')
        raise HTTPException(500, f'Error interno: {exc}')
    finally:
        cur.close(); conn.close()


def run_matching_for_user(user_id: int) -> int:
    """Compara un usuario contra todas las ofertas y envía notificaciones."""
    conn = get_db_connection(); register_vector(conn); cur = conn.cursor()
    try:
        cur.execute('SELECT embedding FROM "User" WHERE id = %s;', (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Usuario no encontrado')
        user_vec = row[0]

        matches = 0
        for job_id, job_vec in _fetch_embeddings(cur, 'Job', 'id', 'embedding'):
            score = cosine_similarity(user_vec, job_vec)
            if score >= SIMILARITY_THRESHOLD:
                _upsert_match(cur, job_id, user_id, score)
                _notify_user(cur, job_id, user_id, score)
                matches += 1
        conn.commit(); return matches
    except Exception as exc:
        conn.rollback(); logger.exception('Error matching usuario→ofertas')
        raise HTTPException(500, f'Error interno: {exc}')
    finally:
        cur.close(); conn.close()

# ───────────────── Endpoints manuales ─────────────────

@router.post('/job/{job_id}/match', status_code=202, summary='Matching oferta→usuarios')
def api_match_job(job_id: int):
    return {"matches": run_matching_for_job(job_id)}


@router.post('/user/{user_id}/match', status_code=202, summary='Matching usuario→ofertas')
def api_match_user(user_id: int):
    return {"matches": run_matching_for_user(user_id)}
