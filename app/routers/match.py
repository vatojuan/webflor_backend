# app/routers/match.py
from fastapi import APIRouter, HTTPException
from datetime import datetime
import psycopg2, numpy as np
from openai import OpenAI
from pgvector.psycopg2 import register_vector
from app.database import get_db_connection
from app.email_utils import send_match_email

router = APIRouter(prefix="/api/match", tags=["match"])

client = OpenAI()
SIMILARITY_THRESHOLD = 0.80  # ≥ 0.80 se considera match

# ───────────────────────── Utilidades matemáticas ──────────────────────────

def cosine_distance(a, b):
    a = np.array(a); b = np.array(b)
    return 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

# ────────────────────── Motor de coincidencias genérico ────────────────────

def _fetch_vectors(cur, table: str, id_col: str, vec_col: str):
    cur.execute(f'SELECT {id_col}, {vec_col} FROM "{table}" WHERE {vec_col} IS NOT NULL')
    return [(row[0], row[1]) for row in cur.fetchall()]


def _insert_match(cur, job_id: int, user_id: int, score: float):
    cur.execute(
        """
        INSERT INTO matches (job_id, user_id, score, sent_at, status)
        VALUES (%s, %s, %s, NOW(), 'sent')
        ON CONFLICT DO NOTHING
        """,
        (job_id, user_id, score),
    )


def _notify_user(cur, job_id: int, user_id: int, score: float):
    # Extrae datos para el mail
    cur.execute('SELECT email, name FROM "User" WHERE id = %s', (user_id,))
    email, name = cur.fetchone()
    cur.execute('SELECT title, description FROM "Job" WHERE id = %s', (job_id,))
    title, description = cur.fetchone()
    send_match_email(email, name, title, description, score)

# ────────────────────────── Funciones públicas ─────────────────────────────

def run_matching_for_job(job_id: int) -> int:
    """Lanza búsqueda oferta→usuarios y notifica. Devuelve cantidad de matches."""
    conn = get_db_connection(); register_vector(conn); cur = conn.cursor()
    try:
        cur.execute('SELECT embedding FROM "Job" WHERE id = %s', (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Oferta no encontrada')
        job_vector = row[0]
        users = _fetch_vectors(cur, 'User', 'id', 'embedding')
        matches = []
        for uid, uvec in users:
            if not uvec: continue
            score = 1 - cosine_distance(job_vector, uvec)
            if score >= SIMILARITY_THRESHOLD:
                _insert_match(cur, job_id, uid, score)
                _notify_user(cur, job_id, uid, score)
                matches.append(uid)
        conn.commit()
        return len(matches)
    finally:
        cur.close(); conn.close()


def run_matching_for_user(user_id: int) -> int:
    """Lanza búsqueda usuario→ofertas y notifica. Devuelve cantidad de matches."""
    conn = get_db_connection(); register_vector(conn); cur = conn.cursor()
    try:
        cur.execute('SELECT embedding FROM "User" WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, 'Usuario no encontrado')
        user_vector = row[0]
        jobs = _fetch_vectors(cur, 'Job', 'id', 'embedding')
        matches = []
        for jid, jvec in jobs:
            if not jvec: continue
            score = 1 - cosine_distance(user_vector, jvec)
            if score >= SIMILARITY_THRESHOLD:
                _insert_match(cur, jid, user_id, score)
                _notify_user(cur, jid, user_id, score)
                matches.append(jid)
        conn.commit()
        return len(matches)
    finally:
        cur.close(); conn.close()

# Endpoints que delegan en las funciones anteriores

@router.post('/job/{job_id}/match', status_code=202)
def api_match_job(job_id: int):
    return {"matches": run_matching_for_job(job_id)}

@router.post('/user/{user_id}/match', status_code=202)
def api_match_user(user_id: int):
    return {"matches": run_matching_for_user(user_id)}

