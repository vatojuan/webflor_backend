# app/routers/apply.py
from __future__ import annotations

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
    Flujo de confirmación de postulación desde el enlace del e-mail.

    1. Verifica el token en apply_tokens (vigente y sin usar).
    2. Inserta la proposal si todavía no existe:
       • label  = label del Job (fallback 'manual')
       • status = 'pending'  si label == 'manual'
                  'waiting'  para el resto
    3. Marca el token como usado y el matching como 'applied'.
    4. Devuelve { success: true, token: <JWT empleado> }.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # ── 1) Token válido en apply_tokens ───────────────────────────
        cur.execute(
            """
            SELECT job_id,
                   applicant_id,      -- user_id
                   used,
                   expires_at
              FROM apply_tokens
             WHERE token = %s
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return JSONResponse(404, {"detail": "Token inválido o inexistente"})

        job_id, user_id, used, expires_at = row
        if used or expires_at < datetime.utcnow():
            return JSONResponse(400, {"detail": "Token expirado o ya utilizado"})

        # ── 2) Datos del Job  (para label) ────────────────────────────
        cur.execute(
            'SELECT COALESCE(label, \'manual\') FROM "Job" WHERE id = %s',
            (job_id,),
        )
        job_label = cur.fetchone()[0]   # p.ej. manual / automatic / instagram
        proposal_status = "pending" if job_label == "manual" else "waiting"

        # ── 3) Crear proposal (idempotente) ───────────────────────────
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (job_id, applicant_id) DO NOTHING
            """,
            (job_id, user_id, job_label, proposal_status),
        )

        # ── 4) Actualizar token y matching ────────────────────────────
        cur.execute(
            """
            UPDATE apply_tokens
               SET used     = TRUE,
                   used_at  = NOW()
             WHERE token = %s
            """,
            (token,),
        )

        cur.execute(
            """
            UPDATE matches
               SET status            = 'applied',
                   apply_token_used  = TRUE,
                   applied_at        = NOW()
             WHERE job_id  = %s
               AND user_id = %s
            """,
            (job_id, user_id),
        )

        conn.commit()

        # ── 5) JWT para el frontend empleado ─────────────────────────
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
