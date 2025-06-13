# app/routers/apply.py
from datetime import datetime
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from psycopg2 import errors as pg_errors      # <─ para detectar UndefinedColumn

from app.database import get_db_connection
from app.routers.auth import create_access_token

router = APIRouter(prefix="/api/job", tags=["apply"])

@router.get("/apply/{token}", summary="Confirmar postulación")
def apply_with_token(token: str):
    """
    1) Verifica el token (matching.sent & !applied).
    2) Crea la proposal si no existe aún.
    3) Marca el matching como applied.
    4) Actualiza last_application y, si existe, applicants.
    5) Devuelve {success, token=<jwt usuario>}.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Matching válido
        cur.execute("""
            SELECT id, job_id, user_id
              FROM matches
             WHERE trim(apply_token) = trim(%s)
               AND status = 'sent'
               AND applied_at IS NULL
        """, (token,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404,
                                content={"detail": "Token inválido o expirado"})
        match_id, job_id, user_id = row

        # 2) Proposal (si no existe)
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s, %s,
                   COALESCE((SELECT label FROM "Job" WHERE id=%s), 'manual'),
                   'pending', NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals
               WHERE job_id=%s AND applicant_id=%s)
        """, (job_id, user_id, job_id, job_id, user_id))

        # 3) Matching → applied
        cur.execute("""
            UPDATE matches
               SET status='applied', applied_at=NOW()
             WHERE id=%s
        """, (match_id,))

        # 4) last_application  (+ applicants si la columna existe)
        try:
            cur.execute("""
                UPDATE "Job"
                   SET applicants       = COALESCE(applicants,0)+1,
                       last_application = %s
                 WHERE id = %s
            """, (datetime.utcnow(), job_id))
        except pg_errors.UndefinedColumn:
            # La columna aún no existe: sólo actualizamos last_application
            conn.rollback()           # limpia el fallo previo
            cur.execute("""
                UPDATE "Job"
                   SET last_application = %s
                 WHERE id = %s
            """, (datetime.utcnow(), job_id))

        conn.commit()

        # 5) JWT para el usuario
        jwt_token = create_access_token({"sub": str(user_id)})
        return {"success": True, "token": jwt_token}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error interno al procesar la postulación")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
