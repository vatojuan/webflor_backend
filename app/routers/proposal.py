# app/routers/proposal.py

import os
import time
import logging
import smtplib
from dotenv import load_dotenv
from email.message import EmailMessage
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from app.database import engine

load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
)
ALGORITHM = "HS256"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/proposals",
    tags=["proposals"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
        return sub
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def get_db_connection():
    return engine.raw_connection()


def send_proposal_email(to_email: str, subject: str, body: str, attachment_url: str = None) -> bool:
    try:
        smtp_server   = os.getenv("SMTP_SERVER")
        smtp_port     = int(os.getenv("SMTP_PORT", 587))
        smtp_user     = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASS")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to_email

        text = body
        if attachment_url:
            text += f"\n\nRevisa el CV aquí: {attachment_url}"
        msg.set_content(text)

        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.send_message(msg)

        logger.info(f"Email enviado a {to_email}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar email: {e}")
        return False


def send_whatsapp_message(phone: str, message: str) -> bool:
    try:
        logger.info(f"Enviando WhatsApp a {phone}: {message}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar WhatsApp: {e}")
        return False


def process_auto_proposal(proposal_id: int):
    logger.info(f"Background task inicia para propuesta {proposal_id}")
    time.sleep(300)  # 5 minutos

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Obtener estado
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            logger.error(f"No existe propuesta {proposal_id}")
            return
        status, job_id, applicant_id = row
        if status != "waiting":
            logger.info(f"Propuesta {proposal_id} ya no está en 'waiting'")
            return

        # 2) Leer datos de la oferta, incluyendo contacto admin
        cur.execute(
            'SELECT title, source, "contactEmail", "contactPhone" FROM "Job" WHERE id = %s',
            (job_id,)
        )
        job_title, source, contact_email, contact_phone = cur.fetchone()

        # 3) Leer datos del postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()

        # 4) Determinar destinatario según fuente
        if source == "admin":
            employer_email = contact_email
            employer_phone = contact_phone
        else:
            # oferta normal: email/phone del empleador que creó la oferta
            cur.execute(
                'SELECT email, phone FROM "User" WHERE id = (SELECT "userId" FROM "Job" WHERE id = %s)',
                (job_id,)
            )
            employer_email, employer_phone = cur.fetchone()

        # 5) Construir asunto / cuerpo
        subject = f"Nueva propuesta para tu oferta: {job_title}"
        body = (
            f"Hola,\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Contactalo en: {applicant_email}.\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )

        # 6) Envío
        if employer_email:
            send_proposal_email(employer_email, subject, body, attachment_url=cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Hola, tenés una nueva propuesta para '{job_title}'.")

        # 7) Marcar como enviado
        cur.execute("UPDATE proposals SET status = 'sent', sent_at = NOW() WHERE id = %s", (proposal_id,))
        conn.commit()
        logger.info(f"Propuesta {proposal_id} marcada como 'sent'")
    except Exception as e:
        logger.error(f"Error en process_auto_proposal: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("/create")
def create_proposal(payload: dict, background_tasks: BackgroundTasks):
    job_id       = payload.get("job_id")
    applicant_id = payload.get("applicant_id")
    label        = payload.get("label")
    if not job_id or not applicant_id or not label:
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

    status = "waiting" if label == "automatic" else "pending"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status)
            SELECT %s, %s, %s, %s
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id = %s AND applicant_id = %s
            )
            RETURNING id
            """,
            (job_id, applicant_id, label, status, job_id, applicant_id)
        )
        result = cur.fetchone()
        conn.commit()
        if not result:
            return {"message": "Ya existe una propuesta para este usuario y oferta"}
        proposal_id = result[0]
        logger.info(f"Propuesta {proposal_id} creada con status '{status}'")
        if label == "automatic":
            background_tasks.add_task(process_auto_proposal, proposal_id)
        return {"message": "Propuesta creada", "proposal_id": proposal_id}
    except Exception as e:
        conn.rollback()
        logger.error(f"Error al crear propuesta: {e}")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        cur.close()
        conn.close()


@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual_proposal(proposal_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Verificar estado
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Propuesta no encontrada")
        status, job_id, applicant_id = row
        if status != "pending":
            raise HTTPException(status_code=400, detail="No está en status 'pending'")

        # 2) Leer datos de la oferta
        cur.execute(
            'SELECT title, source, "contactEmail", "contactPhone" FROM "Job" WHERE id = %s',
            (job_id,)
        )
        job_title, source, contact_email, contact_phone = cur.fetchone()

        # 3) Leer datos del postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()

        # 4) Determinar destinatario
        if source == "admin":
            employer_email = contact_email
            employer_phone = contact_phone
        else:
            cur.execute(
                'SELECT email, phone FROM "User" WHERE id = (SELECT "userId" FROM "Job" WHERE id = %s)',
                (job_id,)
            )
            employer_email, employer_phone = cur.fetchone()

        # 5) Construir mensaje
        subject = f"Nueva propuesta para tu oferta: {job_title}"
        body = (
            f"Hola,\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Contactalo en: {applicant_email}.\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )

        # 6) Envío
        if employer_email:
            send_proposal_email(employer_email, subject, body, attachment_url=cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Hola, tenés una nueva propuesta para '{job_title}'.")

        # 7) Actualizar estado
        cur.execute("UPDATE proposals SET status = 'sent', sent_at = NOW() WHERE id = %s", (proposal_id,))
        conn.commit()
        return {"message": "Propuesta enviada correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error al enviar manual: {e}")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        cur.close()
        conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
              p.id,
              p.label,
              p.status,
              p.created_at,
              p.sent_at,
              p.notes,
              j.id       AS job_id,
              j.title    AS job_title,
              j.label    AS job_label,
              j.source   AS proposal_source,
              ua.name    AS applicant_name,
              ua.email   AS applicant_email
            FROM proposals p
            JOIN "Job"   j  ON p.job_id      = j.id
            JOIN "User" ua  ON p.applicant_id = ua.id
            ORDER BY p.created_at DESC
            """
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {"proposals": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        logger.error(f"Error al listar propuestas: {e}")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        cur.close()
        conn.close()
