import time
import logging
import smtplib
from email.message import EmailMessage
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from app.database import engine  # Importamos el engine de SQLAlchemy
from backend.auth import get_current_admin  # Asegurate de tener esta función en tu módulo de autenticación

router = APIRouter(
    prefix="/api/proposals",
    tags=["proposals"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Función para obtener conexión usando el engine de SQLAlchemy ---
def get_db_connection():
    """
    Retorna una conexión raw (DBAPI) a la base de datos utilizando el engine de SQLAlchemy.
    """
    return engine.raw_connection()

# --- Función para enviar email de propuesta ---
def send_proposal_email(employer_email: str, subject: str, body: str, attachment_url: str = None) -> bool:
    """
    Envía un email de propuesta al empleador. Si se provee attachment_url,
    se incluye en el body.
    """
    try:
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASS")  # Usá SMTP_PASS según tu variable
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = employer_email
        email_body = body
        if attachment_url:
            email_body += f"\n\nRevisa el CV aquí: {attachment_url}"
        msg.set_content(email_body)
        
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info(f"Email enviado a {employer_email}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar email: {e}")
        return False

# --- Función placeholder para enviar mensaje de WhatsApp ---
def send_whatsapp_message(phone: str, message: str) -> bool:
    """
    Envía un mensaje de WhatsApp. Esta función es un placeholder y debe adaptarse
    a la API real (por ejemplo, con Baileys).
    """
    try:
        logger.info(f"Enviando WhatsApp a {phone}: {message}")
        # Implementación real aquí.
        return True
    except Exception as e:
        logger.error(f"Error al enviar WhatsApp: {e}")
        return False

# --- Función para procesar la propuesta automática en background ---
def process_auto_proposal(proposal_id: int):
    """
    Espera 5 minutos y, si la propuesta sigue en 'waiting', la marca como 'sent'
    y envía el email (y WhatsApp si aplica).
    """
    logger.info(f"Iniciando background task para propuesta {proposal_id}")
    time.sleep(300)  # Espera 5 minutos (300 segundos)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verificar el estado actual de la propuesta
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            logger.error(f"Propuesta {proposal_id} no encontrada en background task.")
            return
        status, job_id, applicant_id = row
        if status != "waiting":
            logger.info(f"La propuesta {proposal_id} ya no está en estado 'waiting' (actual: {status}).")
            return
        
        # Obtener datos del Job (oferta) para armar el email
        cur.execute("SELECT title, \"userId\" FROM \"Job\" WHERE id = %s", (job_id,))
        job_row = cur.fetchone()
        if not job_row:
            logger.error(f"Oferta {job_id} no encontrada para la propuesta {proposal_id}.")
            return
        job_title, employer_id = job_row

        # Obtener datos del postulante
        cur.execute("SELECT name, email, \"cvUrl\" FROM \"User\" WHERE id = %s", (applicant_id,))
        applicant_row = cur.fetchone()
        if not applicant_row:
            logger.error(f"Postulante {applicant_id} no encontrado para la propuesta {proposal_id}.")
            return
        applicant_name, applicant_email, cv_url = applicant_row

        # Obtener datos del empleador
        cur.execute("SELECT name, email, phone FROM \"User\" WHERE id = %s", (employer_id,))
        employer_row = cur.fetchone()
        if not employer_row:
            logger.error(f"Empleador {employer_id} no encontrado para la propuesta {proposal_id}.")
            return
        employer_name, employer_email, employer_phone = employer_row

        # Preparar plantilla de email
        email_subject = f"Nueva propuesta para tu oferta: {job_title}"
        email_body = (
            f"Hola {employer_name},\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Puedes contactar a {applicant_name} a través de: {applicant_email}.\n"
            f"Revisa el CV aquí: {cv_url}\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )

        # Enviar email
        if not send_proposal_email(employer_email, email_subject, email_body, attachment_url=cv_url):
            logger.error(f"No se pudo enviar el email para la propuesta {proposal_id}.")
            return

        # Enviar WhatsApp si el empleador tiene número
        if employer_phone:
            whatsapp_msg = (
                f"Hola {employer_name}, tenés una nueva propuesta para tu oferta '{job_title}'. "
                "Revisa tu correo para más detalles."
            )
            send_whatsapp_message(employer_phone, whatsapp_msg)

        # Actualizar la propuesta: marcar como 'sent'
        cur.execute("""
            UPDATE proposals
            SET status = 'sent', sent_at = NOW()
            WHERE id = %s
        """, (proposal_id,))
        conn.commit()
        logger.info(f"Propuesta {proposal_id} procesada y enviada exitosamente.")
    except Exception as e:
        logger.error(f"Error en process_auto_proposal para propuesta {proposal_id}: {e}")
    finally:
        cur.close()
        conn.close()

# --- Endpoint para crear propuestas ---
@router.post("/create")
def create_proposal(proposal_data: dict, background_tasks: BackgroundTasks, token: str = Depends(oauth2_scheme)):
    """
    Crea una propuesta. Se espera recibir un JSON con:
      - job_id: ID de la oferta
      - applicant_id: ID del postulante
      - label: 'automatic' o 'manual'
    
    Si la propuesta es automática, se inserta con status 'waiting' y se agenda un task
    para enviarla en 5 minutos.
    """
    required_fields = ["job_id", "applicant_id", "label"]
    if not all(field in proposal_data for field in required_fields):
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")
    
    job_id = proposal_data["job_id"]
    applicant_id = proposal_data["applicant_id"]
    label = proposal_data["label"]

    # Si es automática, establecemos status 'waiting'
    status = "waiting" if label == "automatic" else "pending"
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """, (job_id, applicant_id, label, status))
        proposal_id = cur.fetchone()[0]
        conn.commit()

        logger.info(f"Propuesta {proposal_id} creada con status '{status}'.")

        # Si es automática, agenda la tarea de envío en 5 minutos
        if label == "automatic":
            background_tasks.add_task(process_auto_proposal, proposal_id)

        return {"message": "Propuesta creada", "proposal_id": proposal_id}
    except Exception as e:
        logger.error(f"Error al crear propuesta: {e}")
        raise HTTPException(status_code=500, detail="Error al crear la propuesta")
    finally:
        cur.close()
        conn.close()

# --- Endpoint para listar propuestas ---
@router.get("/", dependencies=[Depends(get_current_admin)])
def get_all_proposals():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 
                p.id,
                p.label,
                p.status,
                p.created_at,
                p.sent_at,
                p.notes,
                p.job_id,
                j.title AS job_title,
                j.label AS job_label,
                j.source,
                j."isPaid",
                p.applicant_id,
                ua.name AS applicant_name,
                ua.email AS applicant_email,
                ue.name AS employer_name,
                ue.email AS employer_email,
                ue.phone AS employer_phone
            FROM proposals p
            JOIN "Job" j ON p.job_id = j.id
            JOIN "User" ua ON p.applicant_id = ua.id
            JOIN "User" ue ON j."userId" = ue.id
            ORDER BY p.created_at DESC;
        """)
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        proposals = [dict(zip(columns, row)) for row in rows]
        return {"proposals": proposals}
    except Exception as e:
        logger.error(f"Error al obtener propuestas: {e}")
        raise HTTPException(status_code=500, detail="Error al obtener propuestas")
    finally:
        cur.close()
        conn.close()
