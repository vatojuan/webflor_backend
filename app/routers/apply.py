# app/routers/apply.py
from datetime import datetime
import traceback

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from app.database import get_db_connection
from app.routers.auth import create_access_token
from app.routers.proposal import deliver            # ← función que envía el mail

router = APIRouter(prefix="/api/job", tags=["apply"])


@router.get("/apply/{token}", summary="Confirmar postulación")
def apply_with_token(token: str, bg: BackgroundTasks):
    """
    1) Verifica el token (match.status = 'sent' sin applied_at).
    2) Inserta la proposal si no existía (pending ó waiting).
    3) Marca el matching como applied y el token como usado.
    4) Si la proposal queda en waiting, agenda deliver(pid) en 5 min.
    5) Devuelve { success, token } con JWT del usuario.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Match válido ------------------------------------------------------
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

        # 2) Info del Job ------------------------------------------------------
        cur.execute('SELECT COALESCE(label, \'manual\') FROM "Job" WHERE id=%s',
                    (job_id,))
        job_label = cur.fetchone()[0]
        proposal_status = "pending" if job_label == "manual" else "waiting"

        # 3) Crear proposal (idempotente) --------------------------------------
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (job_id, applicant_id) DO NOTHING
            RETURNING id
            """,
            (job_id, user_id, job_label, proposal_status),
        )
        pid_row = cur.fetchone()
        pid = pid_row[0] if pid_row else None

        # 4) Match -> applied ---------------------------------------------------
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

        # 5) Token usado --------------------------------------------------------
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

        # 6) Si quedó en waiting, programar envío ------------------------------
        if proposal_status == "waiting" and pid:
            # deliver() ya duerme AUTO_DELAY (5 min) antes de mandar el mail
            bg.add_task(deliver, pid, True)

        # 7) JWT para el frontend ----------------------------------------------
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
