import time
import logging
import smtplib
from email.message import EmailMessage
import os

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from app.database import engine

router = APIRouter(
    prefix="/api/proposals",
    tags=["proposals"]
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
ALGORITHM  = "HS256"
logger     = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(401, "Token inválido o expirado")
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")
    return payload["sub"]


def get_db_connection():
    return engine.raw_connection()


def send_proposal_email(to: str, subject: str, body: str, attachment_url: str = None) -> bool:
    try:
        smtp_server   = os.getenv("SMTP_SERVER")
        smtp_port     = int(os.getenv("SMTP_PORT", 587))
        smtp_user     = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASS")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to
        content = body + (f"\n\nRevisa el CV aquí: {attachment_url}" if attachment_url else "")
        msg.set_content(content)
        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Error enviando email: {e}")
        return False


def send_whatsapp_message(phone: str, message: str) -> bool:
    logger.info(f"WhatsApp a {phone}: {message}")
    return True  # placeholder


def process_auto_proposal(proposal_id: int):
    logger.info(f"BG task para propuesta {proposal_id}")
    time.sleep(300)
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row or row[0] != "waiting":
            return
        _, job_id, app_id = row

        cur.execute('SELECT title, "userId" FROM "Job" WHERE id=%s', (job_id,))
        title, emp_id = cur.fetchone()

        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (app_id,))
        app_name, app_email, cv_url = cur.fetchone()

        cur.execute('SELECT name, email, phone FROM "User" WHERE id=%s', (emp_id,))
        emp_name, emp_email, emp_phone = cur.fetchone()

        subj = f"Nueva propuesta para tu oferta: {title}"
        body = (
            f"Hola {emp_name},\n\n"
            f"El postulante {app_name} ha aplicado a '{title}'.\n"
            f"Contacto: {app_email}\n"
            f"CV: {cv_url}\n\nSaludos."
        )

        send_proposal_email(emp_email, subj, body, attachment_url=cv_url)
        if emp_phone:
            send_whatsapp_message(emp_phone, f"Tienes nueva propuesta para '{title}'")

        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error en bg task: {e}")
    finally:
        cur.close(); conn.close()


@router.post("/create", dependencies=[Depends(get_current_admin)])
def create_proposal(data: dict, background_tasks: BackgroundTasks):
    for f in ("job_id", "applicant_id", "label"):
        if f not in data:
            raise HTTPException(400, f"Falta {f}")
    status = "waiting" if data["label"] == "automatic" else "pending"

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO proposals (job_id, applicant_id, label, status) VALUES (%s,%s,%s,%s) RETURNING id;",
            (data["job_id"], data["applicant_id"], data["label"], status)
        )
        pid = cur.fetchone()[0]; conn.commit()
        if data["label"] == "automatic":
            background_tasks.add_task(process_auto_proposal, pid)
        return {"message": "Propuesta creada", "proposal_id": pid}
    except:
        raise HTTPException(500, "Error al crear propuesta")
    finally:
        cur.close(); conn.close()


@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual_proposal(proposal_id: int):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id, label FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Propuesta no encontrada")
        status_, job_id, app_id, label = row
        if label != "manual" or status_ != "pending":
            raise HTTPException(400, "Solo propuestas manuales pendientes pueden enviarse")

        # reutilizamos la misma lógica de envío:
        cur.execute('SELECT title, "userId" FROM "Job" WHERE id=%s', (job_id,))
        title, emp_id = cur.fetchone()
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (app_id,))
        app_name, app_email, cv_url = cur.fetchone()
        cur.execute('SELECT name, email, phone FROM "User" WHERE id=%s', (emp_id,))
        emp_name, emp_email, emp_phone = cur.fetchone()

        subj = f"Nueva propuesta para tu oferta: {title}"
        body = (
            f"Hola {emp_name},\n\n"
            f"El postulante {app_name} ha aplicado a '{title}'.\n"
            f"Contacto: {app_email}\n"
            f"CV: {cv_url}\n\nSaludos."
        )

        send_proposal_email(emp_email, subj, body, attachment_url=cv_url)
        if emp_phone:
            send_whatsapp_message(emp_phone, f"Tienes nueva propuesta para '{title}'")

        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
        return {"message": "Propuesta enviada manualmente"}
    finally:
        cur.close(); conn.close()


@router.get("", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.id,p.label,p.status,p.created_at,p.sent_at,p.notes,
                   j.title AS job_title, ua.name AS applicant_name, ua.email AS applicant_email,
                   ue.name AS employer_name, ue.email AS employer_email, ue.phone AS employer_phone
              FROM proposals p
              JOIN "Job" j ON p.job_id=j.id
              JOIN "User" ua ON p.applicant_id=ua.id
              JOIN "User" ue ON j."userId"=ue.id
             ORDER BY p.created_at DESC;
        """)
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols,row)) for row in cur]}
    finally:
        cur.close(); conn.close()
