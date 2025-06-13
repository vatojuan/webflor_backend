# app/routers/apply.py

from datetime import datetime
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.database import get_db_connection
from app.routers.auth import create_access_token

router = APIRouter(prefix="/api/job", tags=["apply"])


@router.get("/apply/{token}", summary="Confirmar postulación")
def apply_with_token(token: str):
    """
    1) Verifica el token (matching.status='sent' & applied_at IS NULL).
    2) Inserta la proposal si no existía.
    3) Marca el matching como applied.
    4) Devuelve { success, token=<jwt usuario> }.
       (El trigger en BD se encarga de applicants y last_application).
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Matching válido
        cur.execute(
            """
            SELECT id, job_id, user_id
              FROM matches
             WHERE trim(apply_token) = trim(%s)
               AND status = 'sent'
               AND applied_at IS NULL
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content={"detail": "Token inválido o expirado"}
            )
        match_id, job_id, user_id = row

        # 2) Crear proposal (si no existe aún)
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT
              %s AS job_id,
              %s AS applicant_id,
              COALESCE((SELECT label FROM "Job" WHERE id = %s), 'manual') AS label,
              'pending' AS status,
              NOW() AS created_at
            WHERE NOT EXISTS (
              SELECT 1
                FROM proposals
               WHERE job_id = %s AND applicant_id = %s
            )
            """,
            (job_id, user_id, job_id, job_id, user_id),
        )

        # 3) Marcar matching como applied
        cur.execute(
            """
            UPDATE matches
               SET status     = 'applied',
                   applied_at = NOW()
             WHERE id = %s
            """,
            (match_id,),
        )

        conn.commit()

        # 4) Generar JWT del usuario
        jwt_token = create_access_token({"sub": str(user_id)})
        return {"success": True, "token": jwt_token}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error interno al procesar la postulación")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
