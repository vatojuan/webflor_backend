# app/services/embedding.py
import os
import psycopg2
from dotenv import load_dotenv
from pgvector.psycopg2 import register_vector
from openai import OpenAI

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=5432,
            sslmode="require"
        )
        register_vector(conn)
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

def update_user_embedding(user_id: str):
    """
    Actualiza el embedding del usuario basado en su descripción actual.
    Se asume que la tabla "User" tiene la columna "embedding".
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT description FROM "User" WHERE id = %s', (user_id,))
        row = cur.fetchone()
        if not row:
            raise Exception("Usuario no encontrado")
        description = row[0]
        if not description:
            raise Exception("El usuario no tiene descripción para generar embedding")
        embedding_response = client.embeddings.create(
            model="text-embedding-ada-002",
            input=description
        )
        embedding_desc = embedding_response.data[0].embedding
        cur.execute('UPDATE "User" SET embedding = %s WHERE id = %s', (embedding_desc, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Embedding de usuario actualizado exitosamente"}
    except Exception as e:
        raise Exception(f"Error al actualizar el embedding del usuario: {e}")

def generate_file_embedding(text: str):
    """
    Genera un embedding para el contenido de un archivo.
    """
    try:
        embedding_response = client.embeddings.create(
            model="text-embedding-ada-002",
            input=text
        )
        embedding_file = embedding_response.data[0].embedding
        return embedding_file
    except Exception as e:
        raise Exception(f"Error al generar el embedding del archivo: {e}")
