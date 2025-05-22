# app/routers/proposal.py
import os
import time
import logging
import smtplib
import psycopg2
from email.message import EmailMessage
from datetime import timedelta

from dotenv           import load_dotenv
from fastapi          import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose             import jwt, JWTError

from app.database import engine   # ← tu engine SQL-Alchemy

load_dotenv()

# ────────────────────────────── Config ──────────────────────────────
SECRET_KEY  = os.getenv("SECRET_KEY")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY  = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))           # 5 min por defecto

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# -------------------------------------------------------------------
# Helpers de auth & DB
# -------------------------------------------------------------------
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")

def db() -> psycopg2.extensions.connection:
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# -------------------------------------------------------------------
# Envío de e-mail y WhatsApp
# -------------------------------------------------------------------
def _smtp_cfg() -> tuple[str, int, str, str]:
    """
    Devuelve host, puerto, usuario, contraseña.
    Si el puerto es 465 se entenderá como SMTP SSL; cualquier otro usa STARTTLS.
    """
    return (
        os.getenv("SMTP_SERVER", ""),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASS", ""),
    )

def send_mail(dest: str, subj: str, body: str, cv: str | None = None):
    """Envío robusto de correo: EHLO → TLS (o SSL) → LOGIN → SEND → QUIT."""
    host, port, user, pwd = _smtp_cfg()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, dest, subj
    msg.set_content(body + (f"\n\nCV: {cv}" if cv else ""))

    if not all([host, port, user, pwd]):
        raise RuntimeError("Config SMTP incompleta")

    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

        smtp.login(user, pwd)
        smtp.send_message(msg)
        smtp.quit()
        logger.info(f"✉️  Mail enviado → {dest}")
    except Exception as e:
        logger.exception(f"send_mail error: {e}")
        raise

def send_whatsapp(phone: str | None, txt: str):
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")

# -------------------------------------------------------------------
# Lógica de entrega (background o manual)
# -------------------------------------------------------------------
def deliver(pid: int, sleep_first: bool):
    """
    • Si *sleep_first* es True: espera AUTO_DELAY y envía (modo automático).
    • Si es False: envía inmediatamente (botón manual).
    """
    if sleep_first:
        logger.info(f"⏳ task {pid}: sleep {timedelta(seconds=AUTO_DELAY)}")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()

        # 1) Estado actual de la propuesta
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"Propuesta {pid} no existe"); return
        status, job_id, applicant_id = row
        if sleep_first and status != "waiting":
            logger.info(f"Propuesta {pid} dejó waiting ({status})"); return
        if (not sleep_first) and status != "pending":
            raise HTTPException(400, "Solo proposals en pending")

        # 2) Datos de la oferta (soportamos snake & camel)
        cur.execute('SELECT * FROM "Job" WHERE id=%s', (job_id,))
        jrow = cur.fetchone()
        if not jrow:
            logger.error(f"Job {job_id} no hallado"); return
        jcols = [d[0] for d in cur.description]
        job   = dict(zip(jcols, jrow))
        title         = job.get("title")
        source        = job.get("source")
        owner_id      = job.get("user_id") or job.get("userId")
        contact_email = job.get("contact_email") or job.get("contactEmail")
        contact_phone = job.get("contact_phone") or job.get("contactPhone")

        # 3) Datos del postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        a_name, a_mail, cv_url = cur.fetchone()

        # 4) Destino final
        if source == "admin":
            dest_mail, dest_phone = contact_email, contact_phone
        else:
            cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner_id,))
            dest_mail, dest_phone = cur.fetchone()

        # 5) Validación de e-mail
        if not dest_mail:
            logger.warning("❗ Sin e-mail destino: propuesta queda en error_email")
            cur.execute("""
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = 'Sin e-mail de contacto'
                 WHERE id=%s
            """, (pid,))
            conn.commit()
            return

        # 6) Envío
        subj = f"Nueva propuesta – {title}"
        body = (f"Hola,\n\n"
                f"{a_name} se postuló a «{title}».\n"
                f"Mail candidato: {a_mail}\n")

        try:
            send_mail(dest_mail, subj, body, cv_url)
        except Exception:
            # ya logueado en send_mail
            cur.execute("""
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = 'Fallo en el envío de e-mail'
                 WHERE id=%s
            """, (pid,))
            conn.commit()
            return

        send_whatsapp(dest_phone, f"Nueva propuesta para «{title}».")

        # 7) Marcar como enviada
        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"✅ propuesta {pid} → sent")

    except Exception:
        if conn: conn.rollback()
        logger.exception("deliver error")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

# -------------------------------------------------------------------
# End-points
# -------------------------------------------------------------------
@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    job_id, applicant_id = data.get("job_id"), data.get("applicant_id")
    label                = data.get("label", "automatic")
    if not (job_id and applicant_id):
        raise HTTPException(400, "Faltan campos")

    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s,%s,%s,%s,NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s
            )
            RETURNING id
        """, (
            job_id, applicant_id, label,
            "waiting" if label == "automatic" else "pending",
            job_id, applicant_id
        ))
        row = cur.fetchone()
        if not row:
            conn.commit(); return {"message": "Ya existe una propuesta"}
        pid = row[0]; conn.commit()
        logger.info(f"🆕 propuesta {pid} creada ({label})")

        if label == "automatic":
            bg.add_task(deliver, pid, True)
        return {"proposal_id": pid}
    except Exception:
        if conn: conn.rollback()
        logger.exception("create error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.post("/cancel")
def cancel(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "No existe propuesta")
        if row[0] not in ("waiting", "pending"):
            raise HTTPException(400, "Estado no cancelable")

        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"🚫 propuesta {pid} cancelada")
        return {"message": "cancelada"}
    except HTTPException: raise
    except Exception:
        if conn: conn.rollback()
        logger.exception("cancel error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    deliver(pid, False)
    return {"message": "enviada"}

@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "No existe")
        if row[0] != "cancelled":
            raise HTTPException(400, "Solo canceladas")

        cur.execute("DELETE FROM proposals WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"🗑️  propuesta {pid} eliminada")
        return {"message": "eliminada"}
    except HTTPException: raise
    except Exception:
        if conn: conn.rollback()
        logger.exception("delete error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()
        cur.execute("""
            SELECT
              p.id, p.label, p.status,
              p.created_at  AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at     AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              p.notes,
              j.title  AS job_title,
              j.source AS proposal_source,
              COALESCE(j.contact_email, j."contactEmail") AS job_contact_email,
              u.name   AS applicant_name,
              u.email  AS applicant_email
            FROM proposals p
            JOIN "Job"  j ON p.job_id      = j.id
            JOIN "User" u ON p.applicant_id = u.id
            ORDER BY p.created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    except Exception:
        logger.exception("list error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
