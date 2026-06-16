---
name: content-and-document-intelligence
description: Protokoll zur tiefen Textanalyse, Dokumenten-Extraktion und strukturierten Recherche.
---

## 1. Dokumenten-Vergleich & -Analyse
- Beim Vergleichen von Dokumentenversionen extrahiere den Volltext und nutze Pythons 'difflib' auf Satz- oder Absatsebene.
- Kategorisiere Änderungen strikt nach: Formatierung, geringfügige Umformulierung und substantielle Änderungen (Fristen, finanzielle Verpflichtungen, Haftungsausschlüsse). Generiere einen strukturierten Änderungsreport als Markdown.
- Beim Einlesen von '.eml'-Dateien extrahiere Kopfzeilen (Von, An, Datum, Betreff) und bereinige den Body von HTML. Filter gezielt nach Deadlines, Beschlüssen und offenen Aufgaben.

## 2. Strukturierte Recherche & Mitbewerber-Scraping
- Zerlege komplexe Fragestellungen vorab autonom in 3-5 Untersuchungsachsen.
- Nutze innerhalb der Sandbox bei Bedarf gezielte 'curl'-Abfragen mit simulierten User-Agent-Headern, um öffentlich zugängliche Seiten zu erfassen, und extrahiere Kerninformationen strukturiert via 'beautifulsoup4'.
- Erstelle aus Mitbewerberdaten (Preise, Features) normalisierte Vergleichstabellen und überführe sie in übersichtliche Markdown-Zusammenfassungen inkl. Quellennachweis.
