FINEX - web publica + panel interno (agente analista de data centers)

LOCAL:   pip install -r requirements.txt ; define ANTHROPIC_API_KEY ; python server.py
         Web: http://localhost:5050   Panel: http://localhost:5050/interno (contrasena local: finex-demo)

HOSTING GRATUITO (Render):
  Build command:  pip install -r requirements.txt
  Start command:  gunicorn server:app --workers 1 --threads 4 --timeout 120
  Variables:      ANTHROPIC_API_KEY, FINEX_ADMIN_PASSWORD (elige una larga), FINEX_SECRET (cualquier texto largo)
  No subas nunca la clave de API al repositorio: solo en las variables del hosting.

Los anuncios publicados se guardan en data/anuncios.json, que el hosting gratuito borra al reiniciar.
Para conservarlos: en /interno pulsa "Descargar copia" y guarda el fichero como data/seed_anuncios.json en el repositorio.
Los rangos de rentabilidad (7-9 %, 9-12 %) y los umbrales de decision son supuestos de partida.
