import os, io, re, json, mimetypes, psycopg2, smtplib
from datetime import datetime
from email.message import EmailMessage
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from fastapi.background import BackgroundTasks
from fastapi.encoders import jsonable_encoder
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from docx import Document
from jose import JWTError, jwt
from typing import List, Optional

load_dotenv()
router = APIRouter(tags=["email_db"])

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

SMTP_HOST  = os.getenv("SMTP_HOST")
SMTP_PORT  = int(os.getenv("SMTP_PORT", 587))
SMTP_USER  = os.getenv("SMTP_USER")
SMTP_PASS  = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

oauth2 = Depends(
    __import__("fastapi.security").security.OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
)

def get_current_admin(token: str = oauth2):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "Token inválido o expirado")

def db():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME", "postgres"),
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=5432,
        sslmode="require",
    )

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s\-]{8,}")

# ---------- Utilidades extracción ----------
def pdf_to_text(b: bytes) -> str:
    return " ".join([p.extract_text() or "" for p in PdfReader(io.BytesIO(b)).pages])

def docx_to_text(b: bytes) -> str:
    tmp = io.BytesIO(b); doc = Document(tmp)
    return "\n".join(p.text for p in doc.paragraphs)

def txt_to_text(b: bytes) -> str:
    return b.decode(errors="ignore")

def extract_contact(text: str):
    email = next(iter(EMAIL_RE.findall(text)), None)
    phone = next(iter(PHONE_RE.findall(text)), None)
    if not email: return None, None, None
    name  = (
        email.split("@")[0]
        .replace(".", " ")
        .replace("_", " ")
        .title()
    )
    return email.lower(), name, phone

# ---------- Endpoints CRUD ----------
@router.post("/admin_emails_upload", dependencies=[Depends(get_current_admin)])
async def upload_files(files: List[UploadFile] = File(...)):
    results = []
    for f in files:
        logs = [f"Procesando {f.filename}"]
        try:
            raw  = await f.read()
            mime,_ = mimetypes.guess_type(f.filename)
            if mime == "application/pdf" or f.filename.lower().endswith(".pdf"):
                text = pdf_to_text(raw);        logs.append("PDF → texto")
            elif mime and "word" in mime or f.filename.lower().endswith(".docx"):
                text = docx_to_text(raw);        logs.append("DOCX → texto")
            else:
                text = txt_to_text(raw);         logs.append("Texto plano")
            email, name, phone = extract_contact(text)
            if not email:
                raise Exception("E-mail no encontrado")
            conn = db(); cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO email_contacts (email, name, phone, source, source_file)
                VALUES (%s,%s,%s,'file',%s)
                ON CONFLICT (email) DO NOTHING
                """,
                (email, name, phone, f.filename)
            )
            conn.commit(); cur.close(); conn.close()
            logs.append("Guardado en BD")
            results.append({"file":f.filename,"email":email,"status":"success","logs":logs})
        except Exception as e:
            logs.append(f"Error: {e}")
            results.append({"file":f.filename,"status":"error","logs":logs})
    return {"results":results}

@router.post("/admin_emails_manual", dependencies=[Depends(get_current_admin)])
async def add_manual(payload: dict):
    email = payload.get("email","").lower().strip()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400,"E-mail inválido")
    name  = payload.get("name")
    phone = payload.get("phone")
    notes = payload.get("notes")
    conn=db(); cur=conn.cursor()
    cur.execute(
        """
        INSERT INTO email_contacts (email,name,phone,source,notes)
        VALUES (%s,%s,%s,'manual',%s)
        ON CONFLICT (email) DO UPDATE SET
          name  = COALESCE(EXCLUDED.name ,email_contacts.name ),
          phone = COALESCE(EXCLUDED.phone,email_contacts.phone),
          notes = COALESCE(EXCLUDED.notes,email_contacts.notes),
          imported_at = %s
        """,
        (email,name,phone,notes,datetime.utcnow())
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok":True,"email":email}

@router.get("/admin_emails", dependencies=[Depends(get_current_admin)])
def list_emails(
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    offset = (page-1)*page_size
    conn=db(); cur=conn.cursor()
    if search:
        q = f"%{search.lower()}%"
        cur.execute(
            """
            SELECT id,email,name,phone,source,imported_at,valid,notes
            FROM email_contacts
            WHERE LOWER(email) LIKE %s OR LOWER(name) LIKE %s
            ORDER BY imported_at DESC
            LIMIT %s OFFSET %s
            """,
            (q,q,page_size,offset)
        )
    else:
        cur.execute(
            """
            SELECT id,email,name,phone,source,imported_at,valid,notes
            FROM email_contacts
            ORDER BY imported_at DESC
            LIMIT %s OFFSET %s
            """,
            (page_size,offset)
        )
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM email_contacts")
    total = cur.fetchone()[0]
    cur.close(); conn.close()
    return {"total":total,"items":jsonable_encoder(rows)}

@router.put("/admin_emails/{contact_id}", dependencies=[Depends(get_current_admin)])
def update_contact(contact_id:int, payload:dict):
    fields = ["name","phone","notes","valid"]
    sets   = []
    values = []
    for f in fields:
        if f in payload:
            sets.append(f"{f}=%s")
            values.append(payload[f])
    if not sets:
        return {"ok":True}
    values.append(contact_id)
    conn=db(); cur=conn.cursor()
    cur.execute(f"UPDATE email_contacts SET {', '.join(sets)} WHERE id=%s",values)
    conn.commit(); cur.close(); conn.close()
    return {"ok":True}

@router.delete("/admin_emails/{contact_id}", dependencies=[Depends(get_current_admin)])
def delete_contact(contact_id:int):
    conn=db(); cur=conn.cursor()
    cur.execute("DELETE FROM email_contacts WHERE id=%s",(contact_id,))
    conn.commit(); cur.close(); conn.close()
    return {"ok":True}

# ---------- Envío masivo ----------
def _send_email(subject:str, body:str, to_:str):
    msg = EmailMessage()
    msg["Subject"]=subject
    msg["From"]=FROM_EMAIL
    msg["To"]=to_
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST,SMTP_PORT) as s:
        s.starttls(); s.login(SMTP_USER,SMTP_PASS); s.send_message(msg)

@router.post("/admin_emails/send_bulk", dependencies=[Depends(get_current_admin)])
def send_bulk(data:dict, bg: BackgroundTasks):
    subject = data.get("subject")
    body    = data.get("body")
    ids     = data.get("ids")      # opcional lista de IDs
    if not subject or not body:
        raise HTTPException(400,"subject y body requeridos")
    conn=db(); cur=conn.cursor()
    if ids:
        cur.execute("SELECT email FROM email_contacts WHERE id = ANY(%s)",(ids,))
    else:
        cur.execute("SELECT email FROM email_contacts")
    recipients = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    for e in recipients:
        bg.add_task(_send_email,subject,body,e)
    return {"queued":len(recipients)}
