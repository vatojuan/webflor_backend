# app/routers/admin_templates.py
import os, traceback
from datetime import datetime
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")
oauth2     = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────── helpers ──────────────────────────
def get_current_admin(token: str = Depends(oauth2)):
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if not sub:
            raise HTTPException(401, "Token inválido")
        return sub
    except JWTError:
        raise HTTPException(401, "Token inválido")

def get_db():
    return psycopg2.connect(
        dbname   = os.getenv("DBNAME"),
        user     = os.getenv("USER"),
        password = os.getenv("PASSWORD"),
        host     = os.getenv("HOST"),
        port     = int(os.getenv("DB_PORT", 5432)),
        sslmode  = "require",
    )

# router SIN prefix; el prefix se agrega al incluirlo en main.py
router = APIRouter(
    tags=["admin_templates"],
    dependencies=[Depends(get_current_admin)]
)

# ───────────────────────── endpoints ─────────────────────────
@router.get("")   # ← cadena vacía, no "/"
def list_templates():
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id,name,type,subject,body,is_default,created_at,updated_at
            FROM   proposal_templates
            ORDER  BY type, is_default DESC, updated_at DESC;
        """)
        return {"templates": cur.fetchall()}
    except Exception:
        traceback.print_exc(); raise HTTPException(500, "Error al listar plantillas")
    finally:
        cur.close(); conn.close()


@router.post("", status_code=201)
async def create_template(request: Request):
    data       = await request.json()
    name       = data.get("name","").strip()
    tpl_type   = data.get("type","").strip()
    subject    = data.get("subject","").strip()
    body       = data.get("body","").strip()

    if not name or tpl_type not in ("automatic","manual") or not subject or not body:
        raise HTTPException(400, "name, type (automatic|manual), subject y body son obligatorios")

    now = datetime.utcnow()
    conn=get_db(); cur=conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proposal_templates
              (name,type,subject,body,is_default,created_at,updated_at)
            VALUES (%s,%s,%s,%s,FALSE,%s,%s)
            RETURNING id,name,type,subject,body,is_default,created_at,updated_at;
        """,(name,tpl_type,subject,body,now,now))
        tpl = cur.fetchone(); conn.commit()
        return {"template": dict(zip(
            ("id","name","type","subject","body","is_default","created_at","updated_at"), tpl))}
    except Exception:
        traceback.print_exc(); raise HTTPException(500,"Error al crear plantilla")
    finally:
        cur.close(); conn.close()


@router.put("/{tpl_id}")
async def update_template(tpl_id:int, request:Request):
    data       = await request.json()
    name       = data.get("name","").strip()
    tpl_type   = data.get("type","").strip()
    subject    = data.get("subject","").strip()
    body       = data.get("body","").strip()

    if not name or tpl_type not in ("automatic","manual") or not subject or not body:
        raise HTTPException(400,"name, type, subject y body son obligatorios")

    now=datetime.utcnow()
    conn=get_db(); cur=conn.cursor()
    try:
        cur.execute("""
            UPDATE proposal_templates
            SET name=%s,type=%s,subject=%s,body=%s,updated_at=%s
            WHERE id=%s
            RETURNING id,name,type,subject,body,is_default,created_at,updated_at;
        """,(name,tpl_type,subject,body,now,tpl_id))
        tpl=cur.fetchone()
        if not tpl: raise HTTPException(404,"Plantilla no encontrada")
        conn.commit()
        return {"template": dict(zip(
            ("id","name","type","subject","body","is_default","created_at","updated_at"), tpl))}
    except HTTPException: raise
    except Exception:
        traceback.print_exc(); raise HTTPException(500,"Error al actualizar plantilla")
    finally:
        cur.close(); conn.close()


@router.delete("/{tpl_id}")
def delete_template(tpl_id:int):
    conn=get_db(); cur=conn.cursor()
    try:
        cur.execute("DELETE FROM proposal_templates WHERE id=%s RETURNING id;",(tpl_id,))
        if not cur.fetchone(): raise HTTPException(404,"Plantilla no encontrada")
        conn.commit(); return {"message":"Plantilla eliminada"}
    except HTTPException: raise
    except Exception:
        traceback.print_exc(); raise HTTPException(500,"Error al eliminar plantilla")
    finally:
        cur.close(); conn.close()


@router.post("/{template_id}/set-default")
def set_default_template(template_id:int):
    conn=get_db(); cur=conn.cursor()
    try:
        cur.execute("SELECT type FROM proposal_templates WHERE id=%s;",(template_id,))
        row=cur.fetchone()
        if not row: raise HTTPException(404,"Plantilla no encontrada")
        tpl_type=row[0]
        cur.execute("UPDATE proposal_templates SET is_default=FALSE WHERE type=%s;",(tpl_type,))
        cur.execute("UPDATE proposal_templates SET is_default=TRUE  WHERE id=%s;",(template_id,))
        conn.commit(); return {"message":f"Plantilla {template_id} ahora es default de '{tpl_type}'"}
    except HTTPException: conn.rollback(); raise
    except Exception:
        conn.rollback(); traceback.print_exc(); raise HTTPException(500,"Error interno")
    finally:
        cur.close(); conn.close()
