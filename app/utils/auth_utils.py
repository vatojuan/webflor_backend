import os
import psycopg2
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel

# Reutilizamos la conexión a la BD desde el router de autenticación
from app.routers.auth import get_db_connection

# --- Definición del Modelo Pydantic ---
class UserInDB(BaseModel):
    id: int
    email: str
    role: str

    class Config:
        from_attributes = True # Reemplaza a orm_mode=True

# --- Configuración de Seguridad ---
SECRET_KEY = os.getenv("SECRET_KEY", "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

oauth2_scheme_user = OAuth2PasswordBearer(tokenUrl="/auth/login")

# --- Función Base para Obtener Usuario desde Token (CORREGIDA) ---
def get_current_user_from_token(token: str) -> UserInDB:
    """
    Decodifica un token JWT, extrae el identificador (sub), y busca al usuario
    en la base de datos, ya sea por ID numérico o por email.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_identifier: str = payload.get("sub")
        if user_identifier is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # --- LÓGICA CORREGIDA ---
        # Intentamos convertir el identificador a un entero.
        # Si funciona, buscamos por ID. Si falla, es un email y buscamos por email.
        try:
            user_id = int(user_identifier)
            query = 'SELECT id, email, role FROM "User" WHERE id = %s'
            params = (user_id,)
        except ValueError:
            query = 'SELECT id, email, role FROM "User" WHERE email = %s'
            params = (user_identifier,)
        
        cur.execute(query, params)
        user_data = cur.fetchone()
        
        if user_data is None:
            raise credentials_exception
        
        user = UserInDB(id=user_data[0], email=user_data[1], role=user_data[2])
        return user
    finally:
        if cur: cur.close()
        if conn: conn.close()


# === FUNCIÓN para Administradores (Ahora funciona correctamente) ===
def get_current_admin(token: str = Depends(OAuth2PasswordBearer(tokenUrl="/auth/admin-login"))) -> UserInDB:
    """
    Verifica que el token pertenezca a un usuario que es administrador.
    """
    user = get_current_user_from_token(token)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El usuario no tiene permisos de administrador"
        )
    return user


# === FUNCIÓN para Usuarios Activos (Ahora funciona correctamente) ===
def get_current_active_user(token: str = Depends(oauth2_scheme_user)) -> UserInDB:
    """
    Dependencia de FastAPI para obtener el usuario activo actual desde un token.
    """
    user = get_current_user_from_token(token)
    return user
