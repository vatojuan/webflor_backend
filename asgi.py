import sys
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Asegurar que el directorio raíz está en sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Importar la aplicación desde app.main
from app.main import app

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))  # Toma el puerto de la variable de entorno
    uvicorn.run(app, host="0.0.0.0", port=port)
