# proposal.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from datetime import datetime
import smtplib
from email.message import EmailMessage
import os
import logging

from database import get_db_connection

router = APIRouter(
    prefix="/api/proposals",
    tags=["proposals"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Función para enviar email de propuesta ---
def send_proposal_email(employer_email: str, subject: str, body: str, attachment_url: str = None) -> bool:
    """
    Envía un email de propuesta al empleador. Si se provee attachment_url,
    se adjunta como un link (o se puede implementar descarga y envío de archivo).
    """
    try:
        # Configuración SMTP (ejemplo usando variables de entorno)
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASSWORD")
        
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = employer_email
        # Construimos el body con los datos
        email_body = body
        if attachment_url:
            email_body += f"\n\nPuedes ver el CV aquí: {attachment_url}"
        msg.set_content(email_body)
        
        # Si quisieras adjuntar el archivo real, aquí se descargaría y se adjuntaría.
        # Por ahora usamos el link como referencia.
        
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
    Envía un mensaje de WhatsApp. Esta función es un placeholder; se debe implementar
    la integración con la API de WhatsApp (por ejemplo, con Baileys o similar).
    """
    try:
        # Aquí colocarías la lógica real de integración.
        logger.info(f"Enviando WhatsApp a {phone}: {message}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar WhatsApp: {e}")
        return False

# --- Endpoint: Listar todas las propuestas ---
@router.get("/")
def get_all_proposals(token: str = Depends(oauth2_scheme)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Se obtiene info de propuesta, oferta y postulante; y además el empleador de la oferta
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
            JOIN "User" ue ON j.userId = ue.id
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

# --- Endpoint: Enviar propuesta ---
@router.patch("/{proposal_id}/send")
def send_proposal(proposal_id: int, token: str = Depends(oauth2_scheme)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Se valida que la propuesta exista y esté pendiente y sea manual
        cur.execute("""
            SELECT status, label, job_id, applicant_id
            FROM proposals
            WHERE id = %s
        """, (proposal_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Propuesta no encontrada")
        status, proposal_label, job_id, applicant_id = result
        if proposal_label != "manual" or status != "pending":
            raise HTTPException(status_code=400, detail="La propuesta no puede ser enviada (no es manual o ya fue enviada)")

        # Obtener información de la oferta (Job) y del empleador
        cur.execute("""
            SELECT title, "userId"
            FROM "Job"
            WHERE id = %s
        """, (job_id,))
        job_result = cur.fetchone()
        if not job_result:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")
        job_title, employer_id = job_result

        # Obtener datos del postulante
        cur.execute("""
            SELECT name, email, "cvUrl"
            FROM "User"
            WHERE id = %s
        """, (applicant_id,))
        applicant_result = cur.fetchone()
        if not applicant_result:
            raise HTTPException(status_code=404, detail="Postulante no encontrado")
        applicant_name, applicant_email, cv_url = applicant_result

        # Obtener datos del empleador
        cur.execute("""
            SELECT name, email, phone
            FROM "User"
            WHERE id = %s
        """, (employer_id,))
        employer_result = cur.fetchone()
        if not employer_result:
            raise HTTPException(status_code=404, detail="Empleador no encontrado")
        employer_name, employer_email, employer_phone = employer_result

        # Preparar el email con la plantilla
        email_subject = f"Nueva propuesta para tu oferta: {job_title}"
        email_body = (
            f"Hola {employer_name},\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta '{job_title}'.\n"
            f"Puedes contactar a {applicant_name} en: {applicant_email}.\n"
            f"Revisa su CV aquí: {cv_url}\n\n"
            "Saludos,\nTu equipo de FAP Mendoza"
        )

        # Enviar email
        email_sent = send_proposal_email(employer_email, email_subject, email_body, attachment_url=cv_url)
        if not email_sent:
            raise HTTPException(status_code=500, detail="Error al enviar el email al empleador")

        # Enviar WhatsApp (si se tiene número y se quiere integrar)
        if employer_phone:
            whatsapp_message = (
                f"Hola {employer_name}, tenés una nueva propuesta para tu oferta '{job_title}'. "
                f"Revisa tu correo para más detalles."
            )
            send_whatsapp_message(employer_phone, whatsapp_message)

        # Actualizar la propuesta: marcar como enviada y registrar el timestamp
        cur.execute("""
            UPDATE proposals
            SET status = 'sent', sent_at = NOW()
            WHERE id = %s
        """, (proposal_id,))
        conn.commit()

        logger.info(f"Propuesta {proposal_id} enviada correctamente a {employer_email}")
        return {"message": "Propuesta enviada exitosamente"}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error al enviar propuesta: {e}")
        raise HTTPException(status_code=500, detail="Error interno al enviar propuesta")
    finally:
        cur.close()
        conn.close()
