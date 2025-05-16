# app/routers/proposal.py
import os, time, logging, smtplib
from datetime import datetime
from email.message import EmailMessage

from dotenv import load_dotenv
from fastapi import (
    APIRouter, HTTPException, Depends,
    BackgroundTasks, Request
)
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.database import engine

# ──────────────────────  configuración ──────────────────────
load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = "HS256"
AUTO_DELAY = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # 5 min por defecto

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ──────────────────────  utilidades ─────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(401, "Token inválido o expirado")
        return sub
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")

def db():
    return engine.raw_connection()

# ──────────────   envío de email / whatsapp simulados ───────
def send_proposal_email(to_email: str, subject: str, body: str, cv_url: str | None):
    try:
        smtp_server   = os.getenv("SMTP_SERVER")
        smtp_port     = int(os.getenv("SMTP_PORT", "587"))
        smtp_user     = os.getenv("SMTP_USER")
        smtp_pass     = os.getenv("SMTP_PASS")

        msg            = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        if cv_url:
            body += f"\n\nCV: {cv_url}"
        msg.set_content(body)

        with smtplib.SMTP(smtp_server, smtp_port) as s:
            s.starttls(); s.login(smtp_user, smtp_pass); s.send_message(msg)
        logger.info(f"✔️ e-mail enviado a {to_email}")
    except Exception as e:
        logger.error(f"❌ error enviando email → {e}")

def send_whatsapp_message(phone: str, message: str):
    # integrar tu API real aquí
    logger.info(f"✔️ WhatsApp a {phone}: {message}")

# ──────────────  tarea en background para automáticas ───────
def process_auto_proposal(proposal_id: int):
    logger.info(f"⏳ background task para propuesta {proposal_id}")
    time.sleep(AUTO_DELAY)

    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"propuesta {proposal_id} no existe"); return
        status, job_id, applicant_id = row
        if status != "waiting":
            logger.info(f"propuesta {proposal_id} ya fue tratada (status={status})"); return

        # datos oferta
        try:
            cur.execute(
                'SELECT title, source, contact_email, contact_phone '
                'FROM "Job" WHERE id=%s', (job_id,)
            )
            job_title, source, contact_email, contact_phone = cur.fetchone()
        except Exception:   # columnas snake_case inexistentes → pruebo camello
            cur.execute(
                'SELECT title, source, "contactEmail", "contactPhone" '
                'FROM "Job" WHERE id=%s', (job_id,)
            )
            job_title, source, contact_email, contact_phone = cur.fetchone()

        # datos postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()

        # destinatario
        if source == "admin":
            employer_email, employer_phone = contact_email, contact_phone
        else:
            cur.execute(
                'SELECT email, phone FROM "User" '
                'WHERE id=(SELECT "userId" FROM "Job" WHERE id=%s)', (job_id,)
            )
            employer_email, employer_phone = cur.fetchone()

        if not employer_email and not employer_phone:
            logger.warning(f"⚠️ sin datos de contacto para propuesta {proposal_id}")
            return

        # mensaje
        subject = f"Nueva propuesta: {job_title}"
        body    = (
            f"Hola,\n\nEl postulante {applicant_name} se postuló a «{job_title}».\n"
            f"Contacto: {applicant_email}"
        )

        if employer_email:
            send_proposal_email(employer_email, subject, body, cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Tienes nueva propuesta en «{job_title}».")

        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit(); logger.info(f"✅ propuesta {proposal_id} → sent")

    except Exception as e:
        logger.error(f"❌ process_auto_proposal({proposal_id}) falló: {e}")
    finally:
        if cur: cur.close()
        conn.close()

# ────────────────────  creación propuestas ───────────────────
@router.post("/create")
def create_proposal(body: dict, background_tasks: BackgroundTasks):
    job_id, applicant_id, label = body.get("job_id"), body.get("applicant_id"), body.get("label")
    if not all([job_id, applicant_id, label]):
        raise HTTPException(400, "Faltan campos obligatorios")

    status  = "waiting" if label == "automatic" else "pending"
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s,%s,%s,%s,NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s
            ) RETURNING id
        """, (job_id, applicant_id, label, status, job_id, applicant_id))
        row = cur.fetchone(); conn.commit()
        if not row:
            return {"message": "Ya existe propuesta para este usuario y oferta"}
        proposal_id = row[0]; logger.info(f"🆕 propuesta {proposal_id} creada ({status})")
        if label == "automatic":
            background_tasks.add_task(process_auto_proposal, proposal_id)
        return {"proposal_id": proposal_id}
    except Exception as e:
        conn.rollback(); logger.error(e); raise HTTPException(500, "Error interno")
    finally:
        if cur: cur.close(); conn.close()

# ─────────────── cancelación  (DELETE y POST) ────────────────
@router.delete("/{proposal_id}/cancel")
def cancel_by_id(proposal_id: int):
    return _cancel_proposal(proposal_id=proposal_id)

@router.post("/cancel")
def cancel_by_job_applicant(body: dict):
    job_id, applicant_id = body.get("job_id"), body.get("applicant_id")
    if not job_id or not applicant_id:
        raise HTTPException(400, "job_id y applicant_id requeridos")
    return _cancel_proposal(job_id=job_id, applicant_id=applicant_id)

def _cancel_proposal(proposal_id: int | None = None,
                     job_id: int | None = None,
                     applicant_id: int | None = None):
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        if proposal_id:
            cur.execute("SELECT status FROM proposals WHERE id=%s", (proposal_id,))
        else:
            cur.execute("""
                SELECT status,id FROM proposals
                WHERE job_id=%s AND applicant_id=%s
            """, (job_id, applicant_id))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Propuesta no encontrada")
        status, pid = (row if proposal_id is None else (row[0], proposal_id))
        if status not in ("waiting", "pending"):
            raise HTTPException(400, f"No se puede cancelar status '{status}'")
        cur.execute("""
            UPDATE proposals
            SET status='cancelled', cancelled_at=NOW()
            WHERE id=%s
        """, (pid,))
        conn.commit(); logger.info(f"🚫 propuesta {pid} cancelada")
        return {"message": "cancelled", "proposal_id": pid}
    except HTTPException: raise
    except Exception as e:
        conn.rollback(); logger.error(e); raise HTTPException(500, "error al cancelar")
    finally:
        if cur: cur.close(); conn.close()

# ────────────────  envío manual por admin ────────────────────
@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual(proposal_id: int):
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Propuesta no encontrada")
        if row[0] != "pending":
            raise HTTPException(400, "Solo se puede enviar si está en 'pending'")
        process_auto_proposal(proposal_id)       # sin delay
        return {"message": "enviada"}
    finally:
        if cur: cur.close(); conn.close()

# ─────────── eliminar propuestas canceladas (admin) ──────────
@router.delete("/{proposal_id}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(proposal_id: int):
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Propuesta no encontrada")
        if row[0] != "cancelled":
            raise HTTPException(400, "Solo se eliminan propuestas canceladas")
        cur.execute("DELETE FROM proposals WHERE id=%s", (proposal_id,))
        conn.commit(); logger.info(f"🗑️ propuesta {proposal_id} eliminada")
        return {"message": "eliminada"}
    finally:
        if cur: cur.close(); conn.close()

# ───────────────────── lista para admin ──────────────────────
@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
              p.id, p.label, p.status,
              p.created_at AT TIME ZONE 'UTC'
                 AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at AT TIME ZONE 'UTC'
                 AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC'
                 AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              j.id   AS job_id, j.title, j.source AS job_source,
              u.name AS applicant_name, u.email AS applicant_email
            FROM proposals p
            JOIN "Job"  j ON j.id = p.job_id
            JOIN "User" u ON u.id = p.applicant_id
            ORDER BY created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    except Exception as e:
        logger.error(e); raise HTTPException(500, "error interno")
    finally:
        if cur: cur.close(); conn.close()
