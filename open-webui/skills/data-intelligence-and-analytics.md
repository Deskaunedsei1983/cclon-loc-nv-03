---
name: data-intelligence-and-analytics
description: Regelwerk für fortgeschrittene Datenanalyse, SQL und automatisierte Report-Erstellung.
---

## 1. Zero-Hardcoding & Datenakquisition
- Schreibe niemals Arrays oder Dictionaries mit mehr als 10 Einträgen manuell in den Code. Nutze Bibliotheken (z. B. 'yfinance' für Finanzdaten, 'requests' für REST-APIs), um Daten programmatisch zu holen.
- Falls echte Daten temporär blockiert sind, generiere repräsentative synthetische Daten mittels 'Faker' oder 'random' über Schleifen.

## 2. SQL & Datenbank-Analyse
- Verbinde dich sicher über Umgebungsvariablen (DB_HOST, etc.) mit PostgreSQL, MySQL oder SQLite. Schließe Verbindungen konsequent.
- Erkunde immer zuerst das Schema (Tabellenstrukturen, Zeilenanzahl, Datentypen, Foreign Keys), bevor du Analyse-Queries schreibst.
- Schreibe performantes SQL (Nutze explizite JOINs statt Subqueries, LIMIT-Klauseln bei Massendaten und Aggregationen). Lade Ergebnisse zur Weiterverarbeitung direkt in Polars-DataFrames.

## 3. Mute STDOUT & Visualisierung
- CRITICAL: Drucke niemals riesige DataFrames oder Dictionaries per 'print()' im Terminal ab! Große Textmengen sprengen das JSON-Chunk-Limit des Tool-Egress (128KB) und bringen das System zum Absturz. Nutze 'print()' nur für kurze Erfolgsmeldungen.
- Erstelle Daten-Visualisierungen mit 'matplotlib' oder 'seaborn'. Speichere die Grafiken als PNG im Arbeitsverzeichnis.
- Konsolidiere thematische Analysen immer in eine *einzelne* Excel-Datei mittels 'pandas.ExcelWriter' oder entsprechenden Polars-Engines über sauber getrennte Tabellenblätter.
