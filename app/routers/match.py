# app/routers/match.py
"""
Matchings (coincidencias Job ↔ User)

Endpoints disponibles:

• GET  /api/match/job/{job_id}/match   – calcular y devolver scores sin persistir
• GET  /api/match/user/{user_id}/match – idem desde el lado usuario
• GET  /api/match/admin                – listado de matchings guardados (para el panel admin)
• POST /api/match/resend/{mid}         – re-enviar e-mail / WhatsApp de un matching

Además, la función interna run_matching_for_job(job_id) calcula y guarda todos los matchings
de una oferta nueva en la tabla `matches`. Se recomienda llamarla desde el router de ofertas
cuando se crea o se actualiza una oferta, para que los matchings se regeneren automáticamente.
"""
from __future__ import annotations

import os
import logging
import traceback
from typing import List, Dict, Any

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import get_db_connection
from app.routers.proposal import send_mail, send_whatsapp  # reutilizamos helpers

# ─────────────────── Configuración / Auth ───────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    if not token:
        raise HTTPException(401, "Token requerido")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")


# ─────────────────── Router ───────────────────────────
router = APIRouter(prefix="/api/match", tags=["matchings"])
logger = logging.getLogger(__name__)


# ───────────── Helper: convertir cursor a lista de dicts ─────────────
def qdict(cur) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ═════════════════════════════════════════════
#   Función interna: Run Matching for Job
# ═════════════════════════════════════════════
def run_matching_for_job(job_id: int) -> None:
    """
    Calcula todos los scores de similitud entre el embedding de la oferta (Job)
    y cada usuario que tenga embedding. Guarda (o reemplaza) esos resultados en tabla `matches`.
    Deja estado = 'pending', sin enviado. 
    Usa pgvector en SQL: el operador <=> da la distancia de coseno.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # 1) Obtener embedding de la oferta
        cur.execute(
            'SELECT embedding FROM "Job" WHERE id = %s AND embedding IS NOT NULL',
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.warning("run_matching_for_job: Oferta %d no tiene embedding", job_id)
            return
        job_emb = row[0]

        # 2) Eliminar matchings previos para esta oferta (si existieran)
        cur.execute("DELETE FROM matches WHERE job_id = %s", (job_id,))

        # 3) Insertar nuevos matchings calculados con pgvector
        #    - Asumimos que en la tabla `User` hay columna `embedding` de tipo vector (pgvector)
        #    - El operador <=> en Postgres/pgvector devuelve distancia de coseno = 1 - cos_sim
        #    - Convertimos a score = 1 - distancia
        cur.execute(
            '''
            INSERT INTO matches (job_id, user_id, score, sent_at, status)
            SELECT
              %s AS job_id,
              u.id AS user_id,
              (1.0 - (u.embedding <=> %s)) AS score,
              NULL AS sent_at,
              'pending' AS status
            FROM "User" u
            WHERE u.embedding IS NOT NULL
            ''',
            (job_id, job_emb),
        )
        conn.commit()
        logger.info("run_matching_for_job: Generados %d matchings para job %d",
                    cur.rowcount, job_id)

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Error en run_matching_for_job para job_id=%d", job_id)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═════════════════════════════════════════════
#   Endpoint: Listar matchings para admin
# ═════════════════════════════════════════════
@router.get(
    "/admin",
    summary="Listado de matchings (solo admin)",
    dependencies=[Depends(get_current_admin)],
)
def list_matchings():
    """
    Devuelve todos los registros de la tabla `matches` con
    la información agregada que espera el frontend:
      - job: {id, title}
      - user: {id, email}
      - score (float)
      - sent_at, status
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              m.id,
              m.score,
              m.sent_at,
              m.status,
              json_build_object('id', j.id, 'title', j.title)  AS job,
              json_build_object('id', u.id, 'email', u.email)   AS user
            FROM matches m
            JOIN "Job" j ON j.id = m.job_id
            JOIN "User" u ON u.id = m.user_id
            ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
            """
        )
        return qdict(cur)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═════════════════════════════════════════════
#   Endpoint: Reenviar matching (solo admin)
# ═════════════════════════════════════════════
@router.post(
    "/resend/{mid}",
    summary="Reenviar e-mail/WhatsApp de un matching (solo admin)",
    dependencies=[Depends(get_current_admin)],
)
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Obtener datos necesarios para reenviar
        cur.execute(
            """
            SELECT
              m.score,
              j.title,
              j.contact_email,
              j.contact_phone,
              u.name as user_name,
              u.email as user_email,
              u."cvUrl" as user_cv
            FROM matches m
            JOIN "Job" j ON j.id = m.job_id
            JOIN "User" u ON u.id = m.user_id
            WHERE m.id = %s
            """,
            (mid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")

        score, title, c_email, c_phone, cand_name, cand_mail, cv_url = row
        if not c_email:
            raise HTTPException(400, "Oferta sin e-mail de contacto")

        subject = f"🔄 Reenvío – Matching {cand_name} ↔ «{title}»"
        body = (
            f"El candidato {cand_name} ({cand_mail}) coincide con «{title}» "
            f"con un score de {round(score * 100, 1)} %."
        )

        # 2) Enviar e-mail y WhatsApp
        send_mail(c_email, subject, body, cv_url)
        send_whatsapp(c_phone, body)

        # 3) Actualizar tabla matches
        cur.execute(
            "UPDATE matches SET sent_at = NOW(), status = 'resent' WHERE id = %s", (mid,)
        )
        conn.commit()
        logger.info("Matching %d reenviado", mid)
        return {"message": "reenviado"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Error reenviando matching %d", mid)
        raise HTTPException(500, "Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═════════════════════════════════════════════
#   Endpoint: Calcular matches para una oferta (sin persistir)
# ═════════════════════════════════════════════
@router.get("/job/{job_id}/match", summary="Calcular puntajes para un Job")
def match_for_job(job_id: int):
    """
    Retorna la lista de usuarios con sus puntajes de similitud frente a la oferta
    indicada, sin insertar nada en la tabla. Útil para mostrar previsualización
    de match antes de guardar.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Obtener embedding de la oferta
        cur.execute(
            'SELECT embedding FROM "Job" WHERE id = %s AND embedding IS NOT NULL', (job_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Oferta no encontrada o sin embedding")
        job_emb = row[0]

        # 2) Calcular puntajes against todos los usuarios con embedding
        cur.execute(
            """
            SELECT
              u.id AS userId,
              u.email AS userEmail,
              (1.0 - (u.embedding <=> %s)) AS score
            FROM "User" u
            WHERE u.embedding IS NOT NULL
            ORDER BY score DESC
            LIMIT 100
            """,
            (job_emb,),
        )
        result = [
            {"userId": r[0], "email": r[1], "score": float(r[2])} for r in cur.fetchall()
        ]
        return {"matches": result}

    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Error interno al calcular matching para job")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═════════════════════════════════════════════
#   Endpoint: Calcular matches para un usuario (sin persistir)
# ═════════════════════════════════════════════
@router.get("/user/{user_id}/match", summary="Calcular puntajes para un User")
def match_for_user(user_id: int):
    """
    Retorna la lista de ofertas con sus puntajes de similitud frente a un usuario
    (basado en el embedding del usuario), sin insertar nada.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Obtener embedding del usuario
        cur.execute(
            'SELECT embedding FROM "User" WHERE id = %s AND embedding IS NOT NULL', (user_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Usuario no encontrado o sin embedding")
        user_emb = row[0]

        # 2) Calcular puntajes contra todas las ofertas con embedding
        cur.execute(
            """
            SELECT
              j.id AS jobId,
              j.title AS jobTitle,
              (1.0 - (j.embedding <=> %s)) AS score
            FROM "Job" j
            WHERE j.embedding IS NOT NULL
            ORDER BY score DESC
            LIMIT 100
            """,
            (user_emb,),
        )
        result = [
            {"jobId": r[0], "title": r[1], "score": float(r[2])} for r in cur.fetchall()
        ]
        return {"matches": result}

    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Error interno al calcular matching para usuario")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
