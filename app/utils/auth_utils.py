import os
import psycopg2
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Importa el modelo de usuario y la conexión a la BD
from app.schemas import UserInDB  # Asegúrate de que la ruta sea correcta
from app.routers.auth import get_db_connection # Reutilizamos la conexión del router de auth

# --- Configuración de Seguridad ---
SECRET_KEY = os.getenv("SECRET_KEY", "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

# Creamos un esquema de autenticación para los usuarios normales.
# Apunta al endpoint de login que ya tienes en auth.py
oauth2_scheme_user = OAuth2PasswordBearer(tokenUrl="/auth/login")

# --- Función Base para Obtener Usuario desde Token ---
# Esta función centraliza la lógica de decodificación y búsqueda en la BD.
def get_current_user_from_token(token: str) -> UserInDB:
    """
    Decodifica un token JWT, extrae el ID de usuario (sub), busca al usuario
    en la base de datos y devuelve un objeto UserInDB.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, email, role FROM "User" WHERE id = %s', (int(user_id),))
        user_data = cur.fetchone()
        if user_data is None:
            raise credentials_exception
        
        # Creamos una instancia del modelo Pydantic con los datos de la BD
        user = UserInDB(id=user_data[0], email=user_data[1], role=user_data[2])
        return user
    finally:
        if cur: cur.close()
        if conn: conn.close()


# === FUNCIÓN CORREGIDA Y MEJORADA para Administradores ===
def get_current_admin(token: str = Depends(OAuth2PasswordBearer(tokenUrl="/auth/admin-login"))) -> UserInDB:
    """
    Verifica que el token pertenezca a un usuario que es administrador.
    """
    user = get_current_user_from_token(token)
    if user.role != "admin": # Asumiendo que el rol se llama 'admin'
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El usuario no tiene permisos de administrador"
        )
    return user


# === FUNCIÓN NUEVA que resuelve tu error ===
def get_current_active_user(token: str = Depends(oauth2_scheme_user)) -> UserInDB:
    """
    Dependencia de FastAPI para obtener el usuario activo actual a partir de un token.
    Esta es la función que tu router de 'training' necesita.
    """
    user = get_current_user_from_token(token)
    # Aquí podrías añadir más validaciones, como si el usuario está activo o confirmado.
    # Por ahora, simplemente lo devolvemos.
    return user
