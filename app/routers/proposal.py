# app/routers/proposal.py
import os, time, logging, smtplib, psycopg2
from email.message import EmailMessage
from dotenv          import load_dotenv
from fastapi          import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose             import jwt, JWTError
from app.database     import engine

load_dotenv()

SECRET_KEY  = os.getenv("SECRET_KEY")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY  = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # seg → 5 min por defecto

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ──────────────────────────  helpers  ──────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")


def db() -> psycopg2.extensions.connection:
    """Conexión sin autocommit; cada write deberá hacer commit explícito."""
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# ──────────────────────  e-mail / WhatsApp  ────────────────────
def send_mail(dest: str, subj: str, body: str, cv: str | None):
    try:
        host, port = os.getenv("SMTP_SERVER"), int(os.getenv("SMTP_PORT", 587))
        user, pwd  = os.getenv("SMTP_USER"),   os.getenv("SMTP_PASS")

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, dest, subj
        msg.set_content(body + (f"\n\nCV: {cv}" if cv else ""))
        with smtplib.SMTP(host, port) as s:
            s.starttls(); s.login(user, pwd); s.send_message(msg)
        logger.info(f"✉️  Mail → {dest}")
    except Exception:
        logger.exception("send_mail error")

def send_whatsapp(phone: str, txt: str):
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")

# ─────────────────────  background delivery  ───────────────────
def deliver(pid: int, sleep_first: bool):
    if sleep_first:
        logger.info(f"⏳ background task {pid} – sleep {AUTO_DELAY}s")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn, cur = db(), None
        cur = conn.cursor()

        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"Propuesta {pid} no existe"); return
        status, job_id, applicant_id = row
        if sleep_first and status != "waiting":
            logger.info(f"Propuesta {pid} ya no está waiting ({status})"); return
        if (not sleep_first) and status != "pending":
            raise HTTPException(400, "Solo pending")

        # ---- datos de la oferta ----
        cur.execute('SELECT title, source, "contactEmail", "contactPhone", "userId" FROM "Job" WHERE id=%s', (job_id,))
        title, source, c_mail, c_phone, owner = cur.fetchone()

        # ---- postulante ----
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        a_name, a_mail, cv_url = cur.fetchone()

        # ---- destino final ----
        if source == "admin":
            dest_mail, dest_phone = c_mail, c_phone
        else:
            cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner,))
            dest_mail, dest_phone = cur.fetchone()

        subj = f"Nueva propuesta – {title}"
        body = f"Hola,\n\n{a_name} se postuló a «{title}».\nMail: {a_mail}\n"

        send_mail(dest_mail, subj, body, cv_url)
        send_whatsapp(dest_phone, f"Nueva propuesta para «{title}».")

        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"✅ propuesta {pid} → sent")

    except Exception:
        if conn: conn.rollback()
        logger.exception("deliver error")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

# ───────────────────────────  API  ─────────────────────────────
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
            conn.commit()
            return {"message": "Ya existe una propuesta"}
        pid = row[0]
        conn.commit()                          # 👈 COMMIT EXPLÍCITO
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
        if not row:
            raise HTTPException(404, "No existe propuesta")
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
        cur.execute(
            """
            SELECT
              p.id, p.label, p.status,
              p.created_at  AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at     AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
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
        logger.exception("list error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
