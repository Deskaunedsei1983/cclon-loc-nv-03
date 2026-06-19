# Upgrade-Pfad C — Morphik (multimodales/visuelles RAG)  [EXPERIMENTELL]

Morphik nutzt visuelle Late-Interaction (ColPali-artig) und ist stark bei
**bild-/tabellenlastigen Dokumenten** (Scans, Diagramme, komplexe Layouts) —
genau dort, wo klassisches Text-RAG schwächelt. Ergänzt RAGFlow, ersetzt es nicht.

## Starten
```bash
docker compose --profile morphik up -d
```
Bringt `morphik` + `morphik-postgres` (pgvector) + `morphik-redis` hoch.

## Mit dem Agent verbinden
1. In der root-`.env` `MORPHIK_API_URL=http://morphik:8000` (und ggf. `MORPHIK_API_KEY`) setzen.
2. Agent neu starten:  `docker compose up -d agent`
3. Der Agent bekommt dann automatisch ein zusätzliches Tool **`retrieve_multimodal`**
   (PydanticAI ruft es bei Bedarf; der LangGraph-Critic zieht es im `gather`-Schritt mit).

## Lokale Modelle — keine Cloud nötig (`morphik.toml`)
Morphik nutzt **litellm** als Provider-Abstraktion und braucht drei Rollen. Alle
zeigen auf **deinen bestehenden Stack** (siehe `morphik/morphik.toml`, wird in den
Container gemountet):

| Rolle | Modell | Endpoint |
|---|---|---|
| Completion | `main` | `vllm-main:5568/v1` |
| Text-Embedding | `qwen3-embed` (Dim 4096) | `vllm-embed:8091/v1` |
| Visuell (ColPali) | lokales ColPali-Modell | von Morphik selbst geladen (GPU) |

Der häufige `litellm.NotFoundError: 404` kommt daher, dass Morphiks **Default**-Config
die Embeddings auf einen Endpunkt ohne `/embeddings` zeigt (z. B. das Chat-Modell).
Unsere `morphik.toml` behebt das, indem Embeddings explizit auf `vllm-embed` gehen.

## Auth
`agent` und `ingest-router` senden einen **signierten HS256-JWT**
(`JWT_SECRET_KEY == WEBUI_SECRET_KEY`). Ein roher Key → `401 "Not enough segments"`.

## Hinweise / VRAM
- **ColPali läuft lokal auf der GPU** (`enable_colpali = true`) → der `morphik`-Service
  hat jetzt eine GPU-Reservierung + HF-Cache. Behalte das VRAM-Budget im Blick.
- **Dimension:** `embedding.dimensions = 4096` muss zum Embedder passen. Hattest du
  vorher schon (mit anderer Dim) ingestiert, die Morphik-DB zurücksetzen:
  `docker compose down && docker volume rm <projekt>_morphik-pg-data`.

## [VERIFY]
- TOML-Schema ist versionsabhängig. Startet Morphik nicht, Default aus dem Image
  ziehen und angleichen:
  `docker run --rm ghcr.io/morphik-org/morphik-core:latest cat /app/morphik.toml`
- Mount-Pfade `/app/morphik.toml`, `/app/storage` und die Endpoints
  (`/ingest/file`, `/retrieve/chunks`) gegen deine Version prüfen.
  Siehe https://github.com/morphik-org/morphik-core
