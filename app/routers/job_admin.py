import os
import traceback
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# — JWT admin —
SECRET_KEY    = os.getenv("SECRET_KEY")
ALGORITHM     = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
        return sub
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

# — DB helper —
def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            sslmode="require"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la BD: {e}")

def get_admin_config() -> dict[str, bool]:
    """Lee admin_config y devuelve dict de flags booleanos."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT key, value FROM admin_config;")
        rows = cur.fetchall()
        return { k: (v.lower() == "true") for k, v in rows }
    except Exception as e:
        print("❌ Error leyendo configuración:", e)
        return {}
    finally:
        cur.close()
        conn.close()

def get_admin_id(admin_sub: str) -> int | None:
    """Resuelve el ID de usuario (columna id) dado su sub (email)."""
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        cur.execute('SELECT id FROM "User" WHERE email = %s LIMIT 1;', (admin_sub,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()

# — Router protegido —
router = APIRouter(
    prefix="/api/job",
    tags=["job_admin"],
    dependencies=[Depends(get_current_admin)]
)

@router.get("/admin_offers")
async def get_admin_offers(admin_sub: str = Depends(get_current_admin)):
    """
    Devuelve todas las ofertas de Job, filtradas según flags en admin_config:
      • show_expired_admin_offers
      • show_expired_employer_offers
    """
    # 1) Leer flags
    cfg               = get_admin_config()
    show_admin_exp    = cfg.get("show_expired_admin_offers", False)
    show_employer_exp = cfg.get("show_expired_employer_offers", False)

    # 2) Resolver ID del admin actual
    admin_id = get_admin_id(admin_sub)

    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        # 3) Traer todas las ofertas
        cur.execute("""
            SELECT
              j.id,
              j.title,
              j.description,
              j.requirements,
              j."expirationDate",
              j."userId",
              j.source,
              j.label
            FROM public."Job" j
            ORDER BY j.id DESC;
        """)
        cols = [d[0] for d in cur.description]

        now    = datetime.now(timezone.utc)
        offers = []
        for row in cur.fetchall():
            offer = dict(zip(cols, row))
            exp   = offer["expirationDate"]
            is_expired = bool(exp and exp < now)
            if exp:
                offer["expirationDate"] = exp.isoformat()

            is_admin_offer = (admin_id is not None and offer["userId"] == admin_id)

            # 4) Filtrar expiradas según flags
            if is_expired:
                if is_admin_offer and not show_admin_exp:
                    continue
                if not is_admin_offer and not show_employer_exp:
                    continue

            offers.append(offer)

        return {"offers": offers}

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al obtener ofertas: {e}")
    finally:
        cur.close()
        conn.close()

@router.put("/update-admin")
async def update_admin_offer(request: Request):
    """
    Actualiza una oferta de Job y recalcula su embedding.
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        data        = await request.json()
        job_id      = int(data.get("id") or 0)
        title       = data.get("title")
        description = data.get("description")
        requirements= data.get("requirements", "")
        expiration  = data.get("expirationDate")
        user_id     = int(data.get("userId") or 0)

        if not (job_id and title and description and user_id):
            raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

        exp_date = None
        if expiration:
            try:
                exp_date = datetime.fromisoformat(expiration)
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha inválido")

        # Recalcular embedding via OpenAI
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        text_to_embed = f"{title} {description} {requirements}"
        resp = client.embeddings.create(input=text_to_embed, model="text-embedding-ada-002")
        embedding = resp.data[0].embedding

        cur.execute("""
            UPDATE public."Job"
            SET
              title            = %s,
              description      = %s,
              requirements     = %s,
              "expirationDate" = %s,
              "userId"         = %s,
              embedding        = %s
            WHERE id = %s
            RETURNING
              id, title, description, requirements,
              "expirationDate", "userId", source, label;
        """, (title, description, requirements, exp_date, user_id, embedding, job_id))

        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")

        conn.commit()

        cols = ["id","title","description","requirements","expirationDate","userId","source","label"]
        offer = dict(zip(cols, updated))
        if offer["expirationDate"]:
            offer["expirationDate"] = offer["expirationDate"].isoformat()
        return offer

    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error interno al actualizar")
    finally:
        cur.close()
        conn.close()

@router.delete("/delete-admin")
async def delete_admin_offer(request: Request):
    """
    Elimina una oferta de Job por su jobId.
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    try:
        data   = await request.json()
        job_id = int(data.get("jobId") or 0)
        if not job_id:
            raise HTTPException(status_code=400, detail="jobId es requerido")

        cur.execute('DELETE FROM public."Job" WHERE id = %s RETURNING id;', (job_id,))
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")

        conn.commit()
        return {"message": "Oferta eliminada", "jobId": job_id}

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al eliminar oferta: {e}")
    finally:
        cur.close()
        conn.close()
