# Upgrade-Pfad A — E2B Fragments (echte React-Artifacts)  [EXPERIMENTELL]

Fragments ist ein Open-Source-Klon von Claude Artifacts (Next.js 14). Es bringt
**keine eigene Dockerfile** mit und nutzt **E2B** für die Code-Ausführung.

## Einrichten (Klon-Konflikt vermeiden!)
Nicht direkt in `fragments/` klonen — dort liegen diese Hilfsdateien, deshalb
verweigert `git clone .` ("Zielpfad existiert bereits und ist nicht leer").
Stattdessen ins **Unterverzeichnis `app/`** klonen:

```bash
cd fragments
git clone https://github.com/e2b-dev/fragments app   # -> fragments/app/  (leer, kein Konflikt)
cp Dockerfile app/Dockerfile                          # Repo hat keine Dockerfile
cp app/.env.template app/.env.local                   # dann Variablen eintragen (siehe unten)
cd ..
# bauen + starten:
docker compose -f docker-compose.yml -f docker-compose.upgrades.yml \
               --profile fragments up -d --build fragments
# -> http://localhost:3010
```

## `app/.env.local` ausfüllen
Relevante Variablen (Rest leer lassen):
```
E2B_API_KEY=...          # PFLICHT fuer Code-Ausfuehrung (siehe Air-Gap-Hinweis)
OPENAI_API_KEY=not-needed
```
Das **lokale vLLM** wählst du nicht per Env, sondern **in der Fragments-UI**:
oben Provider/Modell wählen und als Base-URL **`http://vllm-main:5568/v1`**
eintragen (der fragments-Container erreicht vLLM ueber `aistack-core`).
Optional: `NEXT_PUBLIC_NO_BASE_URL_INPUT` leer lassen, damit das Base-URL-Feld sichtbar ist.

## WICHTIG — Air-Gap-Konflikt
Die Code-Ausführung läuft in **E2B-Sandboxes**. Ohne **selbst gehostetes** E2B
(Firecracker-MicroVMs, aufwendig) nutzt Fragments die **E2B-Cloud** → Code/Daten
verlassen dein System, die Air-Gap ist gebrochen.

Empfehlung: Fürs lokale/DSGVO-Ziel bleibt **OWUI + die luftdichte `code-sandbox`**
der bessere Weg (echte Office-Files/Notebooks, kein Egress). Fragments ist v.a.
zum Ausprobieren der Artifact-UX gedacht — oder mit eigenem lokalem E2B.

[VERIFY] Variablennamen/Build-Schritte gegen das aktuelle Repo prüfen. Next 14
braucht Node ≥18 (Dockerfile nutzt Node 20).
