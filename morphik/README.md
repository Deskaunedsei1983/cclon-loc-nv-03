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

## Hinweise
- Embeddings/Completions kann Morphik lokal beziehen — in Morphiks `morphik.toml`
  den OpenAI-kompatiblen Endpunkt auf dein vLLM zeigen (Env ist vorbereitet).
- Visuelle Modelle sind GPU-hungrig; behalte das VRAM-Budget im Blick.

[VERIFY] Exaktes Image/Tag, Endpoint-Pfade (`/retrieve/chunks` o.ä.) und die
`morphik.toml`-Konfig gegen das aktuelle Repo prüfen:
https://github.com/morphik-org/morphik-core
Die Tool-Funktion `t_retrieve_multimodal` in `agent/common.py` ist entsprechend markiert.
