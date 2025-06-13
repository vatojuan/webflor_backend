# app/routers/apply.py
from datetime import datetime
import traceback

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from app.database import get_db_connection
from app.routers.auth     import create_access_token
from app.routers.proposal import deliver          # función que envía el mail

router = APIRouter(prefix="/api/job", tags=["apply"])


@router.get("/apply/{token}", summary="Confirmar postulación")
def apply_with_token(token: str, bg: BackgroundTasks):
    """
    1) Valida el token (match.status='sent' y sin applied_at).
    2) Crea la proposal si no existe   → pending ó waiting.
    3) Pasa el matching a applied y marca el token como usado.
    4) Si quedó en waiting agenda deliver(pid) en 5 min.
    5) Devuelve { success, token } con JWT para el front.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # ── 1) Match válido ─────────────────────────────────────────
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
            return JSONResponse(404, {"detail": "Token inválido o expirado"})

        match_id, job_id, user_id = row

        # ── 2) Datos del Job ────────────────────────────────────────
        cur.execute(
            'SELECT COALESCE(label, \'manual\') FROM "Job" WHERE id = %s',
            (job_id,),
        )
        job_label = cur.fetchone()[0]
        proposal_status = "pending" if job_label == "manual" else "waiting"

        # ── 3) Insertar proposal (idempotente) ──────────────────────
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING          -- sin columnas → nunca falla
            RETURNING id
            """,
            (job_id, user_id, job_label, proposal_status),
        )
        row = cur.fetchone()
        if row:
            pid = row[0]
        else:
            # Ya existía; la buscamos
            cur.execute(
                "SELECT id FROM proposals WHERE job_id=%s AND applicant_id=%s",
                (job_id, user_id),
            )
            pid = cur.fetchone()[0]

        # ── 4) Actualizar match ─────────────────────────────────────
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

        # ── 5) Marcar token como usado ─────────────────────────────
        cur.execute(
            """
            UPDATE apply_tokens
               SET used = TRUE,
                   used_at = NOW()
             WHERE token = %s
            """,
            (token,),
        )

        conn.commit()

        # ── 6) Si quedó en waiting, programar envío ────────────────
        if proposal_status == "waiting":
            bg.add_task(deliver, pid, True)  # deliver duerme 5 minutos

        # ── 7) JWT para el usuario ─────────────────────────────────
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
