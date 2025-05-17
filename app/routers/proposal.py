# app/routers/proposal.py
import os, time, logging, smtplib
from email.message import EmailMessage
from datetime import datetime
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.database import engine
import psycopg2

load_dotenv()

SECRET_KEY  = os.getenv("SECRET_KEY")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY  = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # 5 min por defecto

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ────────────────────────────────────
# Auth helper
# ────────────────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub     = payload.get("sub")
        if not sub:
            raise HTTPException(401, "Token inválido")
        return sub
    except JWTError:
        raise HTTPException(401, "Token inválido")

# ────────────────────────────────────
# DB helper
# ────────────────────────────────────
def get_db_connection() -> psycopg2.extensions.connection:
    conn            = engine.raw_connection()
    conn.autocommit = True               # evita “current transaction is aborted”
    return conn

# ────────────────────────────────────
# E-mail / WhatsApp helpers
# ────────────────────────────────────
def send_proposal_email(dest: str, subject: str, body: str, cv_url: str | None):
    try:
        smtp_host = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")

        msg            = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = dest
        msg.set_content(body + (f"\n\nCV: {cv_url}" if cv_url else ""))

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)

        logger.info(f"✉️  Mail enviado → {dest}")
    except Exception:
        logger.exception(f"No se pudo enviar mail a {dest}")

def send_whatsapp_message(phone: str, txt: str):
    # Stub – reemplázalo por tu integración real
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")

# ────────────────────────────────────
# Background task
# ────────────────────────────────────
def deliver_proposal(proposal_id: int, sleep_first: bool = True):
    """Envía la propuesta (auto o manual)."""
    if sleep_first:
        logger.info(f"⏳ background task {proposal_id} – dormimos {AUTO_DELAY}s")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Estado actual
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"Propuesta {proposal_id} no existe")
            return
        status, job_id, applicant_id = row
        if status != "waiting" and sleep_first:   # envío auto
            logger.info(f"Propuesta {proposal_id} ya no está en waiting (status={status})")
            return
        if status != "pending" and not sleep_first:
            raise HTTPException(400, "Solo se puede enviar si está en pending")

        # 2) Job + contacto
        cur.execute('SELECT * FROM "Job" WHERE id=%s', (job_id,))
        jrow  = cur.fetchone()
        jcols = [d[0] for d in cur.description]
        job   = dict(zip(jcols, jrow)) if jrow else {}
        title         = job.get("title")
        source        = job.get("source")
        contact_email = job.get("contactEmail")  or job.get("contact_email")
        contact_phone = job.get("contactPhone")  or job.get("contact_phone")
        owner_id      = job.get("userId")        or job.get("user_id")

        # 3) Datos postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()

        # 4) Destinatario final
        if source == "admin":
            dest_mail, dest_phone = contact_email, contact_phone
        else:
            cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner_id,))
            dest_mail, dest_phone = cur.fetchone()

        # 5) Mensaje
        subject = f"Nueva propuesta – {title}"
        body    = (
            f"Hola,\n\n"
            f"{applicant_name} se postuló a «{title}».\n"
            f"Mail del candidato: {applicant_email}\n"
        )
        if dest_mail:
            send_proposal_email(dest_mail, subject, body, cv_url)
        if dest_phone:
            send_whatsapp_message(dest_phone, f"Tienes una nueva propuesta para «{title}».")

        # 6) Marcar enviada
        cur.execute(
            "UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s",
            (proposal_id,)
        )
        logger.info(f"✅ propuesta {proposal_id} → sent")

    except Exception:
        logger.exception(f"❌ deliver_proposal({proposal_id})")
        if conn:
            conn.rollback()
    finally:
        if cur:  cur.close()
        if conn: conn.close()

# ────────────────────────────────────
# Endpoints
# ────────────────────────────────────
@router.post("/create")
def create_proposal(data: dict, bg: BackgroundTasks):
    job_id, applicant_id = data.get("job_id"), data.get("applicant_id")
    label                = data.get("label", "automatic")
    if not all([job_id, applicant_id, label]):
        raise HTTPException(400, "Faltan campos")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s,%s,%s,%s,NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s
            )
            RETURNING id
            """,
            (job_id, applicant_id, label,
             "waiting" if label == "automatic" else "pending",
             job_id, applicant_id)
        )
        row = cur.fetchone()
        if not row:
            return {"message": "Ya existe una propuesta para este usuario y oferta"}
        pid = row[0]
        logger.info(f"🆕 propuesta {pid} creada ({'waiting' if label=='automatic' else 'pending'})")
        if label == "automatic":
            bg.add_task(deliver_proposal, pid, sleep_first=True)
        return {"proposal_id": pid}
    except Exception:
        logger.exception("create_proposal error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.post("/cancel")
def cancel_proposal(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Propuesta no existe")
        if row[0] not in ("waiting", "pending"):
            raise HTTPException(400, "No puede cancelarse en este estado")

        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (pid,))
        logger.info(f"🚫 propuesta {pid} cancelada")
        return {"message": "cancelada"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("cancel_proposal error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    deliver_proposal(pid, sleep_first=False)
    return {"message": "Propuesta enviada"}

@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "No existe propuesta")
        if row[0] != "cancelled":
            raise HTTPException(400, "Solo se eliminan propuestas canceladas")

        cur.execute("DELETE FROM proposals WHERE id=%s", (pid,))
        logger.info(f"🗑️  propuesta {pid} borrada")
        return {"message": "eliminada"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("delete_cancelled error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT
              p.id, p.label, p.status,
              p.created_at  AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
              p.sent_at     AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
              j.title  AS job_title,
              j.source AS proposal_source,
              u.name   AS applicant_name,
              u.email  AS applicant_email
            FROM proposals p
            JOIN "Job"  j ON p.job_id      = j.id
            JOIN "User" u ON p.applicant_id = u.id
            ORDER BY p.created_at DESC
            """
        )
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    except Exception:
        logger.exception("list_proposals error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
