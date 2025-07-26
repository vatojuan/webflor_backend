# app/routers/apply.py
"""
Módulo para procesar la postulación de un candidato a través de un token.

Este endpoint es el destino del "call to action" en los correos de matching.
Valida el token, crea la propuesta formalmente y notifica al candidato.
"""
import logging

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from app.database import get_db_connection
from app.routers.auth import create_access_token
from app.routers.proposal import deliver  # Necesario para la tarea de fondo

# Importa la nueva función de notificación desde el módulo centralizado
from app.email_utils import send_application_confirmation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/job", tags=["apply"])


@router.get("/apply/{token}", summary="Confirmar postulación del candidato")
def apply_with_token(token: str, bg: BackgroundTasks):
    """
    Valida un token de postulación, formaliza la propuesta y notifica al candidato.

    Flujo de trabajo:
    1. Valida que el token exista, sea para un match 'sent' y no haya sido usado.
    2. Crea una 'proposal' (o la reutiliza si ya existía por algún reintento).
    3. Actualiza el estado del 'match' a 'applied'.
    4. Marca el 'apply_token' como usado para prevenir reutilización.
    5. Si la propuesta es automática, agenda su envío al empleador con un retraso.
    6. **Nuevo**: Agenda un email de confirmación inmediato para el candidato.
    7. Devuelve un JWT al frontend para iniciar sesión al usuario.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Validar el token en la tabla 'matches'
        cur.execute(
            """
            SELECT m.id, m.job_id, m.user_id, j.label, j.title, u.name, u.email
              FROM matches m
              JOIN "Job" j ON m.job_id = j.id
              JOIN "User" u ON m.user_id = u.id
             WHERE trim(m.apply_token) = trim(%s)
               AND m.status IN ('sent', 'resent')
               AND m.applied_at IS NULL
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"detail": "Token de postulación inválido, expirado o ya utilizado."})

        match_id, job_id, user_id, job_label, job_title, applicant_name, applicant_email = row
        proposal_status = "pending" if job_label == "manual" else "waiting"

        # 2. Insertar la propuesta (de forma idempotente)
        # Se busca primero para evitar conflictos si el usuario hace doble clic rápido.
        cur.execute("SELECT id FROM proposals WHERE job_id=%s AND applicant_id=%s", (job_id, user_id))
        proposal_row = cur.fetchone()
        if proposal_row:
            proposal_id = proposal_row[0]
        else:
            cur.execute(
                "INSERT INTO proposals (job_id, applicant_id, label, status, created_at) VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
                (job_id, user_id, job_label, proposal_status),
            )
            proposal_id = cur.fetchone()[0]

        # 3. Actualizar el estado del match y del token
        cur.execute("UPDATE matches SET status='applied', apply_token_used=TRUE, applied_at=NOW() WHERE id=%s", (match_id,))
        cur.execute("UPDATE apply_tokens SET used=TRUE, used_at=NOW() WHERE token=%s", (token,))

        conn.commit()
        logger.info(f"✅ Postulación confirmada para usuario {user_id} a la oferta {job_id} (Propuesta ID: {proposal_id}).")

        # 4. Agendar tareas en segundo plano (emails)
        # Si la propuesta es automática, se agenda el envío al empleador con retraso.
        if proposal_status == "waiting":
            bg.add_task(deliver, proposal_id, True)
            logger.info(f"📨 Propuesta {proposal_id} agendada para envío automático al empleador.")

        # **Nuevo**: Enviar email de confirmación al candidato inmediatamente.
        if applicant_email:
            context = {"applicant_name": applicant_name, "job_title": job_title}
            try:
                bg.add_task(send_application_confirmation, applicant_email, context)
                logger.info(f"📨 Email de confirmación de postulación agendado para {applicant_email}.")
            except Exception as e:
                # No fallar la petición si el email no se puede encolar, pero sí registrarlo.
                logger.error(f"Fallo al encolar email de confirmación para {applicant_email}: {e}")
        
        # 5. Devolver JWT para el frontend
        jwt_token = create_access_token({"sub": str(user_id)})
        return {"success": True, "token": jwt_token}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error interno al procesar token de postulación: {token}")
        raise HTTPException(status_code=500, detail="Error interno al procesar la postulación.")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
