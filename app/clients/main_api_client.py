import requests

# URL base de la API REST de Supabase
MAIN_API_BASE_URL = "https://apnfioxjddccokgkljvd.supabase.co/rest/v1"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFwbmZpb3hqZGRjY29rZ2tsanZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzkzMTkxODAsImV4cCI6MjA1NDg5NTE4MH0.BB8Zq2UQ4n9MuovSOiPnP0Aff6xLC2uUSfEcjYFvfGY"

def get_candidate_data(candidate_id: int):
    """
    Obtiene datos de un candidato específico desde la API principal de Supabase.
    Se asume que tienes una tabla llamada 'candidates' en tu base de datos.
    """
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}"
    }
    # Filtramos por el candidato con id igual a candidate_id
    url = f"{MAIN_API_BASE_URL}/candidates?id=eq.{candidate_id}"
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Levanta error si la respuesta no es exitosa
        return response.json()
    except requests.RequestException as e:
        raise Exception(f"Error al obtener datos del candidato: {e}")
