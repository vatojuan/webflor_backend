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
    2) Inserta la proposal si no existía:
       • label  = label del Job        (fallback 'manual')
       • status = 'pending'  si label='manual'
                  'waiting'  en cualquier otro caso
    3) Marca el matching como 'applied'.
    4) Devuelve { success, token=<jwt usuario> }.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # ── 1) Matching válido ─────────────────────────────────────────
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
                content={"detail": "Token inválido o expirado"},
            )
        match_id, job_id, user_id = row

        # ── 2) Datos del Job (label) ───────────────────────────────────
        cur.execute('SELECT COALESCE(label, \'manual\') FROM "Job" WHERE id = %s', (job_id,))
        job_label = cur.fetchone()[0]            # p.ej. manual / automatic / instagram

        proposal_status = "pending" if job_label == "manual" else "waiting"

        # ── 3) Crear proposal (si no existe) ───────────────────────────
        cur.execute(
            """
            INSERT INTO proposals
                  (job_id, applicant_id, label,  status,           created_at)
            SELECT %s,     %s,           %s,     %s,               NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals
               WHERE job_id = %s AND applicant_id = %s
            )
            """,
            (
                job_id,
                user_id,
                job_label,
                proposal_status,
                job_id,
                user_id,
            ),
        )

        # ── 4) Matching → applied ─────────────────────────────────────
        cur.execute(
            """
            UPDATE matches
               SET status='applied', applied_at = NOW()
             WHERE id = %s
            """,
            (match_id,),
        )

        conn.commit()

        # ── 5) JWT para el usuario ────────────────────────────────────
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
