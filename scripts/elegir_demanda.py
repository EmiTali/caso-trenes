# scripts/elegir_demanda.py
import secrets

EXPEDIENTES = ["C1", "C2", "C3", "C4", "C5", "C6"]

if __name__ == "__main__":
    apartada = secrets.choice(EXPEDIENTES)
    print(f"Demanda apartada (no abrir hasta semana 6): {apartada}")