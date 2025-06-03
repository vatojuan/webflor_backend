# app/routers/apply.py
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from app.database import get_db_connection

router = APIRouter(tags=["apply"])


@router.get("/apply/{token}", response_class=JSONResponse, summary="Confirmar postulacion")
def apply_with_token(token: str):
    """
    Valida el token, crea la propuesta automática y responde JSON.
    La página React en /apply/[token].js hará el fetch a este endpoint
    y mostrará el mensaje de confirmación o error en pantalla.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Buscar el matching vigente con ese token y status='sent'
        cur.execute(
            """
            SELECT m.id, m.job_id, m.user_id
              FROM matches m
             WHERE m.apply_token = %s
               AND m.status = 'sent'
               AND m.applied_at IS NULL
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Token inválido o ya usado")

        match_id, job_id, user_id = row

        # 2) Crear la propuesta (si no existe aún para este user-job)
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
              SELECT 1 FROM proposals
               WHERE job_id = %s AND applicant_id = %s
            )
            RETURNING id
            """,
            (job_id, user_id, job_id, job_id, user_id),
        )
        cur.fetchone()  # ignoramos el ID, solo evitar duplicados

        # 3) Marcar el matching como “ya aplicado”
        cur.execute(
            """
            UPDATE matches
               SET applied_at = NOW(),
                   status = 'applied'
             WHERE id = %s
            """,
            (match_id,),
        )
        conn.commit()

        return {"success": True, "message": "Postulación confirmada"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        raise HTTPException(500, "Error interno al procesar la postulación")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
