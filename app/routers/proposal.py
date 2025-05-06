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

# --------------------------------------------------
# Carga de .env y configuración
# --------------------------------------------------
load_dotenv()

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
)
ALGORITHM = "HS256"

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Router con prefijo /api/proposals
router = APIRouter(
    prefix="/api/proposals",
    tags=["proposals"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# --------------------------------------------------
# Dependencia para endpoints protegidos (admin)
# --------------------------------------------------
def get_current_admin(token: str = Depends(oauth2_scheme)):
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

# --------------------------------------------------
# Conexión a la base de datos (raw cursor)
# --------------------------------------------------
def get_db_connection():
    """
    Devuelve raw_connection() de SQLAlchemy para cursores manuales.
    """
    return engine.raw_connection()

# --------------------------------------------------
# Funciones de envío (email / WhatsApp)
# --------------------------------------------------
def send_proposal_email(employer_email: str, subject: str, body: str, attachment_url: str = None) -> bool:
    try:
        smtp_server   = os.getenv("SMTP_SERVER")
        smtp_port     = int(os.getenv("SMTP_PORT", 587))
        smtp_user     = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASS")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = employer_email

        text = body
        if attachment_url:
            text += f"\n\nRevisa el CV aquí: {attachment_url}"
        msg.set_content(text)

        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.send_message(msg)

        logger.info(f"Email enviado a {employer_email}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar email: {e}")
        return False


def send_whatsapp_message(phone: str, message: str) -> bool:
    try:
        logger.info(f"Enviando WhatsApp a {phone}: {message}")
        # Placeholder para integración real
        return True
    except Exception as e:
        logger.error(f"Error al enviar WhatsApp: {e}")
        return False

# --------------------------------------------------
# Background task para propuestas automáticas
# --------------------------------------------------
def process_auto_proposal(proposal_id: int):
    logger.info(f"Background task inicia para propuesta {proposal_id}")
    time.sleep(300)  # 5 minutos

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            logger.error(f"No existe propuesta {proposal_id}")
            return
        status, job_id, applicant_id = row
        if status != "waiting":
            logger.info(f"Propuesta {proposal_id} ya no está en 'waiting'")
            return

        cur.execute('SELECT title, "userId" FROM "Job" WHERE id = %s', (job_id,))
        job_title, employer_id = cur.fetchone()
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()
        cur.execute('SELECT name, email, phone FROM "User" WHERE id = %s', (employer_id,))
        employer_name, employer_email, employer_phone = cur.fetchone()

        subject = f"Nueva propuesta para tu oferta: {job_title}"
        body = (
            f"Hola {employer_name},\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Contactalo en: {applicant_email}.\n"
            f"Revisa el CV aquí: {cv_url}\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )
        send_proposal_email(employer_email, subject, body, attachment_url=cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Hola {employer_name}, tenés una nueva propuesta para '{job_title}'.")

        cur.execute("UPDATE proposals SET status = 'sent', sent_at = NOW() WHERE id = %s", (proposal_id,))
        conn.commit()
        logger.info(f"Propuesta {proposal_id} marcada como 'sent'")
    except Exception as e:
        logger.error(f"Error en process_auto_proposal: {e}")
    finally:
        cur.close()
        conn.close()

# --------------------------------------------------
# Endpoints
# --------------------------------------------------

@router.post("/create")
def create_proposal(payload: dict, background_tasks: BackgroundTasks):
    """
    Crea una propuesta automática o manual. Evita duplicados.
    """
    job_id = payload.get("job_id")
    applicant_id = payload.get("applicant_id")
    label = payload.get("label")
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
              SELECT 1 FROM proposals
              WHERE job_id = %s AND applicant_id = %s
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
    """
    Envía inmediatamente una propuesta manual (status 'pending').
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Propuesta no encontrada")
        status, job_id, applicant_id = row
        if status != "pending":
            raise HTTPException(status_code=400, detail="No está en status 'pending'")

        cur.execute('SELECT title, "userId" FROM "Job" WHERE id = %s', (job_id,))
        job_title, employer_id = cur.fetchone()
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()
        cur.execute('SELECT name, email, phone FROM "User" WHERE id = %s', (employer_id,))
        employer_name, employer_email, employer_phone = cur.fetchone()

        subject = f"Nueva propuesta para tu oferta: {job_title}"
        body = (
            f"Hola {employer_name},\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Contactalo en: {applicant_email}.\n"
            f"Revisa el CV aquí: {cv_url}\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )
        send_proposal_email(employer_email, subject, body, attachment_url=cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Hola {employer_name}, tenés una nueva propuesta para '{job_title}'.")

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
    """
    Lista todas las propuestas ordenadas por fecha.
    """
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
              p.applicant_id,
              ua.name    AS applicant_name,
              ua.email   AS applicant_email
            FROM proposals p
            JOIN "Job"  j  ON p.job_id      = j.id
            JOIN "User" ua ON p.applicant_id = ua.id
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
