# app/routers/apply.py

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime
from app.database import get_db_connection
from app.routers.auth import create_access_token
import traceback

router = APIRouter(
    prefix="/api/job",
    tags=["apply"],
    # no dependencies here!
)

@router.get(
    "/apply/{token}",
    summary="Confirmar postulación",
    responses={
        200: {"description": "Postulación confirmada"},
        404: {"description": "Token inválido o expirado"},
        500: {"description": "Error interno"},
    },
)
def apply_with_token(token: str):
    """
    1. Comprueba que el token pertenezca a un matching con status 'sent' y aún no aplicado.
    2. Crea (si no existe) la propuesta para ese user-job.
    3. Marca el matching como applied.
    4. Incrementa el contador de postulantes en la oferta.
    5. Devuelve JSON { success, token } con un JWT del usuario.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # ── 1) Matching vigente ─────────────────────────────────────────
        cur.execute("""
            SELECT m.id, m.job_id, m.user_id
              FROM matches m
             WHERE trim(m.apply_token) = trim(%s)
               AND m.status = 'sent'
               AND m.applied_at IS NULL
        """, (token,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"detail": "Token inválido o expirado"})

        match_id, job_id, user_id = row

        # ── 2) Crear propuesta si no existe ─────────────────────────────
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT
              %s, %s,
              COALESCE((SELECT label FROM "Job" WHERE id = %s), 'manual'),
              'pending',
              NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals
               WHERE job_id = %s AND applicant_id = %s
            )
        """, (job_id, user_id, job_id, job_id, user_id))

        # ── 3) Marcar matching como aplicado ────────────────────────────
        cur.execute("""
            UPDATE matches
               SET applied_at = NOW(),
                   status     = 'applied'
             WHERE id = %s
        """, (match_id,))

        # ── 4) Incrementar contador de candidatos en la oferta ──────────
        cur.execute("""
            UPDATE "Job"
               SET applicants       = COALESCE(applicants, 0) + 1,
                   last_application = %s
             WHERE id = %s
        """, (datetime.utcnow(), job_id))

        conn.commit()

        # ── 5) JWT del usuario ──────────────────────────────────────────
        access_token = create_access_token({"sub": str(user_id)})
        return {"success": True, "token": access_token}

    except HTTPException:
        raise

    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error interno al procesar la postulación")

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
