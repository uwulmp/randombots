import os
from dotenv import load_dotenv

load_dotenv()  # charge ton .env

token = os.getenv("DISCORD_TOKEN")
print("Ton token est :", token if token else "⚠️ Rien trouvé")
