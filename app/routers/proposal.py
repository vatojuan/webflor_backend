# app/routers/proposal.py
import os, time, logging, smtplib
from email.message import EmailMessage
from dotenv       import load_dotenv
from fastapi      import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose         import jwt, JWTError
from app.database import engine
import psycopg2

load_dotenv()

# ╭─────────────────────────╮
# │  Config & logger        │
# ╰─────────────────────────╯
SECRET_KEY  = os.getenv("SECRET_KEY")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY  = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))   # 5 min por defecto

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("proposal")

# ╭─────────────────────────╮
# │  FastAPI setup          │
# ╰─────────────────────────╯
router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


# ╭─────────────────────────╮
# │  Helpers                │
# ╰─────────────────────────╯
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token requerido")
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if not sub:
            raise HTTPException(401, "Token inválido")
        return sub
    except JWTError:
        raise HTTPException(401, "Token inválido")


def get_conn() -> psycopg2.extensions.connection:
    c = engine.raw_connection()
    c.autocommit = True                    # evita “current transaction is aborted…”
    return c


def send_email(to_: str, subject: str, body: str):
    host, port = os.getenv("SMTP_SERVER"), int(os.getenv("SMTP_PORT", 587))
    user, pwd  = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    msg        = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to_, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls(); s.login(user, pwd); s.send_message(msg)
        logger.info(f"✉️  e-mail enviado → {to_}")
    except Exception:
        logger.exception(f"Email falló → {to_}")


def send_whatsapp(phone: str, txt: str):
    # stub
    logger.info(f"📲 WhatsApp a {phone}: {txt}")


# ╭─────────────────────────╮
# │  Background task        │
# ╰─────────────────────────╯
def process_auto_proposal(pid: int):
    logger.info(f"⏳ bg-task propuesta {pid}")
    time.sleep(AUTO_DELAY)

    conn, cur = None, None
    try:
        conn, cur = get_conn(), None
        cur       = conn.cursor()

        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row or row[0] != "waiting":
            logger.info(f"Propuesta {pid} ya procesada / inexistente")
            return
        _, job_id, applicant_id = row

        # ─ Job
        cur.execute('SELECT * FROM "Job" WHERE id=%s', (job_id,))
        jcols = [d[0] for d in cur.description]
        j     = dict(zip(jcols, cur.fetchone()))
        title         = j["title"]
        src           = j.get("source")
        contact_email = j.get("contact_email") or j.get("contactEmail")
        contact_phone = j.get("contact_phone") or j.get("contactPhone")
        owner_id      = j.get("userId") or j.get("user_id")

        # ─ Applicant
        cur.execute('SELECT name,email,"cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        a_name, a_email, cv_url = cur.fetchone()

        # ─ Destinatario
        if src == "admin":
            dest_email, dest_phone = contact_email, contact_phone
        else:
            cur.execute('SELECT email,phone FROM "User" WHERE id=%s', (owner_id,))
            dest_email, dest_phone = cur.fetchone()

        subject = f"Nueva propuesta → {title}"
        body    = f"Hola,\n\n{a_name} se postuló a «{title}».\nMail candidato: {a_email}\nCV: {cv_url or '—'}"

        if dest_email:  send_email(dest_email, subject, body)
        if dest_phone:  send_whatsapp(dest_phone, f"Tienes una nueva propuesta para «{title}».")

        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (pid,))
        logger.info(f"✅ propuesta {pid} → sent")

    except Exception:
        logger.exception(f"process_auto_proposal({pid}) falló")
        if conn: conn.rollback()
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ╭─────────────────────────╮
# │  Endpoints              │
# ╰─────────────────────────╯
@router.post("/create")
def create_proposal(body: dict, bg: BackgroundTasks):
    job_id, user_id = body.get("job_id"), body.get("applicant_id")
    label           = (body.get("label") or "automatic").lower()
    if not all([job_id, user_id]):
        raise HTTPException(400, "job_id y applicant_id requeridos")

    conn, cur = get_conn(), None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s,%s,%s,%s,NOW()
            WHERE NOT EXISTS (SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s)
            RETURNING id
            """,
            (job_id, user_id, label, "waiting" if label=="automatic" else "pending", job_id, user_id),
        )
        rec = cur.fetchone()
        if not rec:
            return {"message": "ya_existe"}
        pid = rec[0]
        logger.info(f"🆕 propuesta {pid} creada ({label})")
        if label == "automatic":
            bg.add_task(process_auto_proposal, pid)
        return {"id": pid}
    except Exception:
        logger.exception("create_proposal falló")
        raise HTTPException(500, "Error interno")
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.post("/cancel")
def cancel_proposal(body: dict):
    pid = body.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn, cur = get_conn(), None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s AND status IN ('waiting','pending') RETURNING id", (pid,))
        if not cur.fetchone():
            raise HTTPException(400, "No cancelable / inexistente")
        logger.info(f"🚫 propuesta {pid} cancelada")
        return {"message": "cancelada"}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def admin_send(pid: int):
    """Envío manual de propuestas `pending`."""
    conn, cur = get_conn(), None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE proposals SET status='waiting' WHERE id=%s AND status='pending' RETURNING id", (pid,))
        if not cur.fetchone():
            raise HTTPException(400, "Debe estar en pending")
        # ahora se procesa sin delay
        process_auto_proposal(pid)
        return {"message": "enviada"}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn, cur = get_conn(), None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.label, p.status,
                   p.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
                   p.sent_at    AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
                   p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires',
                   j.title AS job_title, ua.name AS applicant_name, ua.email AS applicant_email
            FROM proposals p
            JOIN "Job"  j  ON j.id = p.job_id
            JOIN "User" ua ON ua.id = p.applicant_id
            ORDER BY p.id DESC
            """
        )
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()
