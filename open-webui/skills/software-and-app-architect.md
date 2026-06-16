---
name: software-and-app-architect
description: Richtlinien für Software-Scaffolding und Code-Reviews in der Sandbox.
---

## 1. Full-Stack Entwicklung (FastAPI + Vue.js)
Wenn du Software-Architekturen baust:
- API-Backend: Nutze Python mit FastAPI.
- Datenhaltung: Nutze DuckDB für relationale/analytische Daten, Polars für High-Performance Processing.
- Frontend: Initialisiere Web-UIs mit Vite, Vue.js und Tailwind CSS. Erstelle direkt ein demo-ready, modernes Dashboard mit Metriken und responsiven Tabellen (inkl. Such-/Filterfunktion).
- Struktur: Generiere sinnvolle Seed-Daten. Führe am Ende 'git init' aus, erstelle eine saubere '.gitignore' und schreibe eine unmissverständliche 'README.md' mit den exakten Startbefehlen.

## 2. Code-Review-Modus
Wenn du Code analysieren oder verändern sollst:
- Nutze 'git diff' oder lies die Dateien vollständig, um den logischen Kontext und Imports zu verstehen.
- Analysiere Code rigoros nach folgenden Kategorien: Sicherheit (Injections, harte Credentials), Korrektheit (Edge-Cases, Race-Conditions), Performance (N+1 Abfragen, Speicherblockaden) und Wartbarkeit.
- Priorisiere Funde klar nach Relevanz (Kritisch, Warnung, Vorschlag) unter Angabe der exakten Zeilennummern und liefere die korrigierten Code-Snippets direkt mit.
