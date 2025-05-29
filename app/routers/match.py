# app/routers/match.py

import logging

from fastapi import APIRouter, HTTPException
from pgvector.psycopg2 import register_vector
from openai import OpenAI
import numpy as np

from app.database import get_db_connection
from app.email_utils import send_match_email

router = APIRouter(tags=["match"])
logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.80  # Mínimo 80% de similitud para considerar match
client = OpenAI()            # Para futuras integraciones con OpenAI si fuera necesario


# ──────────────────────── Utilidades ────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Devuelve la similitud coseno entre dos vectores.
    """
    a_arr = np.array(a, dtype=float)
    b_arr = np.array(b, dtype=float)
    if a_arr.size == 0 or b_arr.size == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr)))


def _fetch_embeddings(cur, table: str, id_col: str, vec_col: str) -> list[tuple[int, list[float]]]:
    """
    Recupera todos los IDs y embeddings no nulos de una tabla.
    """
    cur.execute(f'SELECT {id_col}, {vec_col} FROM "{table}" WHERE {vec_col} IS NOT NULL;')
    return cur.fetchall()  # List[ (id, vector) ]


def _upsert_match(cur, job_id: int, user_id: int, score: float) -> None:
    """
    Inserta un match en la tabla, evitando duplicados.
    """
    cur.execute("""
        INSERT INTO matches (job_id, user_id, score, sent_at, status)
        VALUES (%s, %s, %s, NOW(), 'sent')
        ON CONFLICT (job_id, user_id) DO NOTHING;
    """, (job_id, user_id, score))


def _notify_user(cur, job_id: int, user_id: int, score: float) -> None:
    """
    Envía un email al usuario con los datos de la oferta.
    """
    cur.execute('SELECT email, name FROM "User" WHERE id = %s;', (user_id,))
    email, name = cur.fetchone()
    cur.execute('SELECT title, description FROM "Job" WHERE id = %s;', (job_id,))
    title, description = cur.fetchone()
    send_match_email(email=email,
                     name=name,
                     title=title,
                     description=description,
                     score=score)


# ─────────────────────── Matching Engine ───────────────────────

def run_matching_for_job(job_id: int) -> int:
    """
    Busca usuarios cuyo embedding concuerde con la oferta y les envía notificación.
    Retorna la cantidad de matches generados.
    """
    conn = get_db_connection()
    register_vector(conn)
    cur = conn.cursor()
    try:
        # 1) Obtener embedding de la oferta
        cur.execute('SELECT embedding FROM "Job" WHERE id = %s;', (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Oferta no encontrada")
        job_vec = row[0]

        # 2) Recuperar todos los embeddings de usuarios
        users = _fetch_embeddings(cur, "User", "id", "embedding")

        # 3) Calcular similitudes y generar matches
        match_count = 0
        for user_id, user_vec in users:
            score = cosine_similarity(job_vec, user_vec)
            if score >= SIMILARITY_THRESHOLD:
                _upsert_match(cur, job_id, user_id, score)
                _notify_user(cur, job_id, user_id, score)
                match_count += 1

        conn.commit()
        return match_count

    except Exception as e:
        conn.rollback()
        logger.exception("Error en matching oferta→usuarios")
        raise HTTPException(500, f"Error interno de matching: {e}")
    finally:
        cur.close()
        conn.close()


def run_matching_for_user(user_id: int) -> int:
    """
    Busca ofertas cuyo embedding concuerde con el usuario y le envía notificación.
    Retorna la cantidad de matches generados.
    """
    conn = get_db_connection()
    register_vector(conn)
    cur = conn.cursor()
    try:
        cur.execute('SELECT embedding FROM "User" WHERE id = %s;', (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Usuario no encontrado")
        user_vec = row[0]

        jobs = _fetch_embeddings(cur, "Job", "id", "embedding")

        match_count = 0
        for job_id, job_vec in jobs:
            score = cosine_similarity(user_vec, job_vec)
            if score >= SIMILARITY_THRESHOLD:
                _upsert_match(cur, job_id, user_id, score)
                _notify_user(cur, job_id, user_id, score)
                match_count += 1

        conn.commit()
        return match_count

    except Exception as e:
        conn.rollback()
        logger.exception("Error en matching usuario→ofertas")
        raise HTTPException(500, f"Error interno de matching: {e}")
    finally:
        cur.close()
        conn.close()


# ───────────────────────── Endpoints ─────────────────────────

@router.post("/job/{job_id}/match",
             status_code=202,
             summary="Inicia matching entre una oferta y todos los usuarios")
def api_match_job(job_id: int):
    matches = run_matching_for_job(job_id)
    return {"matches": matches}


@router.post("/user/{user_id}/match",
             status_code=202,
             summary="Inicia matching entre un usuario y todas las ofertas")
def api_match_user(user_id: int):
    matches = run_matching_for_user(user_id)
    return {"matches": matches}
