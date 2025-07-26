# app/routers/match.py
"""
Módulo para la lógica de "matching" entre candidatos y ofertas de trabajo.

- run_matching_for_job: Se ejecuta cuando se crea una nueva oferta, buscando candidatos compatibles.
- run_matching_for_user: Se ejecuta cuando se registra un nuevo usuario, buscando ofertas compatibles.
- Endpoints de admin: Para visualizar y gestionar los matchings generados.
"""
from __future__ import annotations

import os
import uuid
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import get_db_connection
# Se utilizan las funciones centralizadas de email_utils
from app.email_utils import send_match_notification, send_admin_alert

# ─────────────────── Configuración & Autenticación ───────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "https://fapmendoza.com").rstrip("/")
oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/match", tags=["matchings"])


def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(status_code=401, detail="Token admin inválido o requerido")


# ─────────────────── Helpers de Base de Datos ───────────────────

def _cur_to_dicts(cur) -> List[Dict[str, Any]]:
    """Convierte el resultado de un cursor psycopg2 a una lista de diccionarios."""
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ═══════════ Matching Batch (Cuando se crea una nueva oferta) ═══════════

def run_matching_for_job(job_id: int) -> None:
    """
    Calcula y notifica los matches para una oferta de trabajo específica.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Obtener embedding de la oferta
        cur.execute(
            'SELECT embedding FROM "Job" WHERE id = %s AND embedding IS NOT NULL',
            (job_id,),
        )
        job_embedding = cur.fetchone()
        if not job_embedding:
            logger.info(f"Matching omitido: Oferta {job_id} no tiene embedding.")
            return

        # 2. Limpiar matches previos para esta oferta
        cur.execute("DELETE FROM matches WHERE job_id = %s", (job_id,))
        logger.info(f"Matches previos para la oferta {job_id} eliminados.")

        # 3. Insertar nuevos matches pendientes basados en similitud de embeddings
        cur.execute("""
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT %s, u.id,
                   (1.0 - (u.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "User" u
             WHERE u.embedding IS NOT NULL AND u.role = 'empleado' AND u.confirmed = TRUE
        """, (job_id, job_embedding[0]))
        conn.commit()
        logger.info(f"Insertados {cur.rowcount} nuevos matches para la oferta {job_id}.")

        # 4. Consultar los matches generados y enviar notificaciones
        cur.execute("""
            SELECT m.id, m.score, u.name, u.email, j.title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.job_id = %s AND (m.score)::float >= 0.80
        """, (job_id,))

        matches_to_notify = cur.fetchall()
        logger.info(f"Enviando {len(matches_to_notify)} notificaciones de match.")

        for match_id, score, user_name, user_email, job_title in matches_to_notify:
            if not user_email:
                continue

            apply_token = str(uuid.uuid4())
            apply_link = f"{FRONTEND_URL}/apply/{apply_token}"

            context = {
                "applicant_name": user_name,
                "job_title": job_title,
                "score": f"{float(score) * 100:.1f}%",
                "apply_link": apply_link,
            }

            try:
                # Llamada a la función centralizada de envío
                send_match_notification(user_email, context)

                # Si el envío es exitoso, actualizar la base de datos
                cur.execute(
                    "UPDATE matches SET apply_token=%s, status='sent', sent_at=NOW() WHERE id=%s",
                    (apply_token, match_id)
                )
                cur.execute("""
                    INSERT INTO apply_tokens (token, job_id, applicant_id, expires_at, used)
                    VALUES (%s, %s, (SELECT user_id FROM matches WHERE id=%s), NOW() + INTERVAL '30 days', FALSE)
                    ON CONFLICT(token) DO NOTHING
                """, (apply_token, job_id, match_id))
                conn.commit()

            except Exception as e:
                logger.exception(f"❌ Error enviando notificación de match {match_id} a {user_email}: {e}")
                cur.execute(
                    "UPDATE matches SET status='error', error_msg=%s WHERE id=%s",
                    (str(e)[:250], match_id),
                )
                conn.commit()

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error crítico en el proceso batch run_matching_for_job para job_id={job_id}")
        send_admin_alert(
            subject="Fallo Crítico en Matching por Oferta",
            details=f"El proceso de matching para la oferta ID {job_id} falló.\nError: {e}"
        )
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ═══════════ Matching Batch (Cuando se registra un usuario nuevo) ═══════════

def run_matching_for_user(user_id: int) -> None:
    """
    Calcula los matches para un nuevo usuario contra todas las ofertas existentes.
    (No envía notificaciones, solo pre-calcula los scores).
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('SELECT embedding FROM "User" WHERE id = %s AND embedding IS NOT NULL', (user_id,))
        user_embedding = cur.fetchone()
        if not user_embedding:
            logger.info(f"Matching omitido: Usuario {user_id} no tiene embedding.")
            return

        cur.execute("DELETE FROM matches WHERE user_id = %s", (user_id,))

        cur.execute("""
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT j.id, %s,
                   (1.0 - (j.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
        """, (user_id, user_embedding[0]))
        conn.commit()
        logger.info(f"Insertados {cur.rowcount} matches para el nuevo usuario {user_id}.")

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error crítico en el proceso batch run_matching_for_user para user_id={user_id}")
        send_admin_alert(
            subject="Fallo Crítico en Matching por Usuario",
            details=f"El proceso de matching para el usuario ID {user_id} falló.\nError: {e}"
        )
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ═══════════ Panel de Admin & Funcionalidad de Reenvío ═══════════

@router.get("/admin", dependencies=[Depends(get_current_admin)], summary="Listado de matchings (score ≥ 0.80)")
def list_matchings():
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.score, m.sent_at, m.status,
                   json_build_object('id', j.id, 'title', j.title) AS job,
                   json_build_object('id', u.id, 'email', u.email) AS user
              FROM matches m
              JOIN "Job"  j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE (m.score)::float >= 0.80
             ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
        """)
        return _cur_to_dicts(cur)
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.post("/resend/{match_id}", dependencies=[Depends(get_current_admin)], summary="Reenviar email de matching")
def resend_matching(match_id: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.score, m.apply_token, u.name, u.email, j.title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.id = %s
        """, (match_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Matching no encontrado")

        score, token, user_name, user_email, job_title = row
        if not user_email:
            raise HTTPException(status_code=400, detail="El candidato no tiene un email registrado.")
        if not token:
            raise HTTPException(status_code=400, detail="Este match no tiene un token de aplicación para reenviar.")

        apply_link = f"{FRONTEND_URL}/apply/{token}"
        context = {
            "applicant_name": user_name,
            "job_title": job_title,
            "score": f"{float(score) * 100:.1f}%",
            "apply_link": apply_link,
        }

        # Llamada a la función centralizada de envío
        send_match_notification(user_email, context)

        cur.execute("UPDATE matches SET sent_at=NOW(), status='resent' WHERE id=%s", (match_id,))
        conn.commit()
        return {"message": "Notificación de matching reenviada exitosamente."}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error inesperado al reenviar el match {match_id}")
        raise HTTPException(status_code=500, detail="Error interno del servidor.")
    finally:
        if cur: cur.close()
        if conn: conn.close()
