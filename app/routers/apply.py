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
    1) Verifica el token en matches con status='sent'.
    2) Inserta proposal si no existe (status según label del Job).
    3) Marca matching como 'applied'.
    4) Marca apply_token como usado.
    5) Devuelve { success, token }.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # ── 1) Validar token en MATCHES ──────────────────────────────
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

        # ── 2) Obtener label del Job ─────────────────────────────────
        cur.execute('SELECT COALESCE(label, \'manual\') FROM "Job" WHERE id = %s', (job_id,))
        job_label = cur.fetchone()[0]
        proposal_status = "pending" if job_label == "manual" else "waiting"

        # ── 3) Crear proposal si no existía ──────────────────────────
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s, %s, %s, %s, NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id = %s AND applicant_id = %s
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

        # ── 4) Marcar matching como 'applied' ────────────────────────
        cur.execute(
            """
            UPDATE matches
               SET status = 'applied',
                   apply_token_used = TRUE,
                   applied_at = NOW()
             WHERE id = %s
            """,
            (match_id,),
        )

        # ── 5) Marcar apply_token como usado ─────────────────────────
        cur.execute("""
            UPDATE apply_tokens
            SET used = TRUE
            WHERE token = %s
        """, (token,))

        conn.commit()

        # ── 6) Devolver token JWT del usuario ───────────────────────
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
