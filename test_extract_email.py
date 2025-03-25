import re

# Lista de TLDs comunes (en minúsculas)
COMMON_TLDS = {"com", "org", "net", "edu", "gov", "io", "co", "us", "ar", "comar"}

def extract_email(text):
    """
    Extrae el primer email del texto y recorta cualquier texto extra pegado al TLD,
    usando una lista de TLDs comunes para determinar dónde cortar.
    
    Ejemplos:
      "jonathanguarnier2017@gmail.comExperiencia laboral..."  => "jonathanguarnier2017@gmail.com"
      "persona@example.orgExtra"                              => "persona@example.org"
      "prueba@empresa.comarDoc adicional"                     => "prueba@empresa.comar"
    """
    # 1. Limpieza básica: eliminar saltos y reducir espacios
    cleaned_text = re.sub(r'[\r\n\t]+', ' ', text)
    cleaned_text = re.sub(r'\s{2,}', ' ', cleaned_text)

    # 2. Buscar un candidato a email que pueda tener letras extra pegadas al TLD
    pattern = r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[A-Za-z]*'
    match = re.search(pattern, cleaned_text)
    if not match:
        return None
    candidate = match.group(0)

    # 3. Buscar el último punto para identificar el comienzo del TLD
    last_dot = candidate.rfind('.')
    if last_dot == -1:
        return candidate

    # 4. Extraer la secuencia de letras desde el último punto hasta el primer carácter no alfabético
    tld_contig = ""
    for ch in candidate[last_dot+1:]:
        if ch.isalpha():
            tld_contig += ch
        else:
            break

    # 5. Iterar desde el máximo posible (hasta 8 letras) hasta 2 para encontrar el TLD válido más largo
    max_length = min(9, len(tld_contig)+1)  # probamos hasta 8 letras
    valid_tld = None
    for i in range(max_length-1, 1, -1):  # de max_length-1 a 2 (inclusive)
        possible_tld = tld_contig[:i].lower()
        if possible_tld in COMMON_TLDS:
            valid_tld = possible_tld
            break

    if valid_tld:
        # Recortar el candidato justo hasta el final del TLD válido
        final_email = candidate[:last_dot+1+len(valid_tld)]
        return final_email
    else:
        return candidate

# -------------------------------
# Pruebas unitarias
# -------------------------------
if __name__ == "__main__":
    tests = [
        "Mi correo es jonathanguarnier2017@gmail.comExperiencia laboral...",
        "Correo: persona@example.orgExtra",
        "Email: hola.mundo123@miempresa.com",
        "Sin mail acá.",
        "Dirección: prueba@empresa.comarDoc adicional",
        "Otro: user@dominio"
    ]
    
    for i, t in enumerate(tests, start=1):
        result = extract_email(t)
        print(f"🧪 Prueba {i}: {result}")
