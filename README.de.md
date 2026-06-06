![Banner](assets/banner.png)

# Memorandum Message Collector

[![CI](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml/badge.svg)](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Sprache:** [English](README.md) · Deutsch · [Русский](README.ru.md) · [简体中文](README.zh-CN.md)

Schluss mit dem Durchsuchen von fünf Chat-Clients, nur um herauszufinden, was jemand letzten Dienstag gesagt hat. Memorandum aggregiert **Mattermost, Telegram, Pachca und IMAP-E-Mail** in einer lokalen, durchsuchbaren Datenbank und stellt sie als MCP-Tools bereit — damit **Claude, Gemini, Hermes** und andere MCP-Clients Fragen über all deine Arbeitsunterhaltungen beantworten können.

## Frag Claude zum Beispiel

> *„Fass zusammen, was das Platform-Team diese Woche zu `PL-15491` diskutiert hat."*
>
> *„Hat mich gestern jemand zur Migration @-erwähnt?"*
>
> *„Such die Tabelle, die Marina am Dienstag geschickt hat, und zieh die Q3-Zahlen raus."*
>
> *„Entwirf eine Antwort auf die letzte E-Mail des Kunden zum Launch-Datum."*

Memorandum läuft lokal — deine Nachrichten und Anhänge verlassen niemals deinen Rechner, und der Agent spricht mit ihnen über MCP.

## Funktionen

**Quellen & Synchronisation**
- Liest aus **Mattermost, Telegram, Pachca und IMAP** — mehrere Konten je Quelle, unabhängig benannt
- Inkrementelle Synchronisation pro Quelle; paralleles Abrufen (Ausfall einer Quelle bleibt isoliert)
- Erfasst **Dateianhänge beim Ingest** — kritisch für Pachca und Telegram, deren URLs ablaufen
- YAML-Filter pro Quelle: Bots, Kanäle und Regex-Muster ausschließen

**Suche & Abruf**
- **Zweischichtige Speicherung** — SQLite für strukturierte Abfragen (Absender / Kanal / Zeitraum), ChromaDB für semantische Suche
- **Live Gap Reads** — `get_new_messages` greift direkt auf die Quelle zu, damit der Agent die aktuellste Spitze eines Kanals sieht
- **Thread-Rekonstruktion** — `get_thread` liefert das Root plus alle Antworten, auch über IMAP-Ordner hinweg
- **YouTrack-Issue-Links** — Issue-IDs werden aus URLs und Kanalnamen geparst; `find_by_issue` liefert alles, was darauf verweist
- **Permalinks** an jedem Ergebnis — Klick zurück zur Originalnachricht

**Personen & Identität**
- **Quellenübergreifende Aliase** mit optional Rolle / Team / reports-to / responsible-for — der Agent weiß ab der ersten Sitzung, wer wer ist
- **Vom Agenten beschreibbare Aliase** — Claude kann Erkenntnisse über Personen (Rollenwechsel, neues Projekt) direkt in `config.yaml` persistieren (Round-Trip erhält deine Kommentare)
- **Intern vs. extern** — gestufte Klassifizierung (Quellen-Flag → E-Mail-Domain → Per-Alias-Override); externe Absender werden mit `[external]` markiert
- **Mention-Graph** — `who_mentioned` beantwortet „Wer hat mich / Alice diese Woche angepingt?" mit Alias-Auflösung

**Betrieb**
- **MCP-Server** mit Tools für Suche, Zusammenfassung, Digest, Threads, Issue-Lookup und Dateizugriff
- **Senden zurück** (opt-in, per Quelle freigeschaltet) — Telegram-Business-Chats unterstützt; E-Mail-Antworten landen zur Prüfung in deinem Drafts-Ordner
- **Aufbewahrung / Housekeeping** — automatisches Aufräumen alter Nachrichten + Vektoren; inhaltsadressierter Anhangssweep behält alles, was noch referenziert wird
- **CLI**: `./bin/memorandum {health, dashboard, aliases refresh, prune, reindex-chroma}` — Live-Terminal-TUI plus Housekeeping-Tools

Für Implementierungsdetails (Architektur, Schemas, Sync-Interna) siehe [AGENTS.md](AGENTS.md).

## Schnellstart

### 1. Repo klonen

```bash
git clone https://github.com/shiryavsky/memorandum.git
cd memorandum
```

### 2. Setup (macOS/Linux)

```bash
./setup.sh
```

`setup.sh` erstellt ein `.venv`, installiert die Python-Abhängigkeiten und legt beim ersten Lauf `config.yaml` aus `config.example.yaml` an. Unter Linux wird CPU-only-PyTorch (transitive Abhängigkeit von FlagEmbedding) aus dem [PyTorch CPU Index](https://download.pytorch.org/whl/cpu) vorinstalliert, sodass das ~1,3 GB große CUDA-Bündel übersprungen wird — auf macOS ist Torch bereits CPU.

Standardmäßig wird ein mehrsprachiges Embedding-Modell installiert — funktioniert direkt auch mit Deutsch.

### 3. Konfigurieren

Die Konfiguration liegt in zwei Dateien:

- **`config.yaml`** (im Projektverzeichnis, gitignored) — Struktur, Filter, Aliases, Retention.
- **`/etc/memorandum/secrets.yaml`** (`chmod 600`) — Tokens / Passwörter pro Quelle. Außerhalb des Projektbaums, damit ein Filesystem-fähiger Agent mit Sandbox in `~/` (Claude Desktop / Claude Code / beliebiger Filesystem-MCP) sie nicht lesen kann. Siehe [Warum eine separate secrets-Datei](#warum-eine-separate-secrets-datei) unten.

**Einmal-Setup der secrets-Datei:**

```bash
sudo mkdir -p /etc/memorandum
sudo install -m 600 -o "$USER" secrets.example.yaml /etc/memorandum/secrets.yaml
sudo "$EDITOR" /etc/memorandum/secrets.yaml
```

Dann `config.yaml` bearbeiten (in Schritt 2 aus `config.example.yaml` erstellt) und Quellen hinzufügen:

```yaml
sources:
  company_mattermost:
    type: mattermost
    enabled: true
    url: "https://mattermost.yourcompany.com"
    # token kommt aus /etc/memorandum/secrets.yaml
    internal: true                        # Absender hier gelten als interne Mitarbeiter (externe bekommen ein [external]-Tag)
    allow_send: false                     # Standard; auf true setzen, damit send_message hier posten darf
    filters:
      skip_senders: ["github-bot"]
      skip_channels: ["off-topic"]
      skip_patterns:
        - "^Reminder:"
        - "joined the channel"

  work_telegram:
    type: telegram
    enabled: true
    # token kommt aus secrets.yaml — Bot von @BotFather

  work_pachca:
    type: pachca
    enabled: true
    # token kommt aus secrets.yaml — Automations → API in den Pachca-Einstellungen
    filters:
      skip_channels: ["random"]

display_timezone: "America/New_York"   # Zeitstempel in der MCP-Ausgabe

# Optional: YouTrack-Issue-Links und Kanalnamen wie "PL-15491" klassifizieren.
# Diesen Block weglassen, um die Issue-ID-Erkennung zu deaktivieren (URLs werden weiterhin generisch extrahiert).
youtrack:
  base_url: "https://youtrack.yourcompany.com"
  project_prefixes: [PL, DEMO, MOBILE]

# Der aktuelle Benutzer (wird immer als intern behandelt). Bare Usernames verwenden, kein führendes "@".
my_aliases:
  - "you"
  - "you.lastname"

# Kanonische Identität anderer Personen. role / team / reports_to / responsible_for
# sind optional und tauchen über das MCP-Tool `get_user_aliases` auf.
user_aliases:
  - canonical_name: "Jane Smith"
    internal: true
    role: "Backend lead"
    team: "Platform"
    responsible_for: ["dev-pl", "PL-*"]
    aliases: ["jane", "jsmith"]
```

`/etc/memorandum/secrets.yaml` spiegelt die Quellennamen aus dem Config oben:

```yaml
sources:
  company_mattermost:
    token: "PAT-paste-your-mattermost-token-here"
  work_telegram:
    token: "123456:AABBcc..."
  work_pachca:
    token: "your-pachca-token"
```

Um den Default-Pfad zu überschreiben (Tests / Dev / ohne sudo): `secrets_path:` in `config.yaml` setzen oder `MEMORANDUM_SECRETS_PATH` exportieren. Eine fehlende Datei ist okay — Konnektoren, die ein Credential brauchen, scheitern dann mit klarer Fehlermeldung beim Connect.

#### Warum eine separate secrets-Datei

Der MCP-Server läuft als du und kann alles lesen, was dein User darf. Der **Agent** (Claude Desktop, Claude Code, jeder filesystem-fähige MCP-Client) ist normalerweise auf das Projekt- / Home-Verzeichnis sandboxed. Wenn die Credentials unter `/etc/` liegen, sind sie physisch außerhalb dieser Allowlist — ein fehl­geleitetes oder zukünftiges Filesystem-Tool greppt sie nicht, und ein Path-Traversal vom Agenten kommt nicht aus seiner Sandbox raus. Das ist keine UNIX-Permission-Grenze, sondern die Sandbox-Grenze — aber genau die respektiert der Agent tatsächlich.

Tipp: Nach ein paar Wochen Ingest `./bin/memorandum aliases refresh` ausführen — das druckt Stub-Einträge für jeden Absender, der noch nicht in `user_aliases` steht, sortiert nach Nachrichtenanzahl und mit der Quelle markiert, aus der er kommt. Die interessanten Einträge einfügen und `role`/`team`/`internal` von Hand ergänzen.

### 4. Erster Ingest

```bash
./run_ingest.sh --hours 720  # Letzte 30 Tage abrufen
```

### 5. Health-Check

Nach dem ersten Ingest prüfen, dass alles korrekt verdrahtet ist — Quellen verbunden, Nachrichten gespeichert, Embeddings befüllt:

```bash
./bin/memorandum health
```

Derselbe Bericht ist auch als MCP-Tool `get_health` verfügbar, sobald der Server registriert ist.

### 6. Scheduler starten (läuft alle 15 Minuten)

**Linux mit systemd (empfohlen für Produktion):**
```bash
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer
```

**macOS oder Nicht-systemd-Umgebungen:**
```bash
./bin/memorandum-sync
```
Mit `cron` / `launchd` kombinieren, wenn es zyklisch laufen soll. Das Skript nimmt `/tmp/memorandum-sync.lock`, also sind überlappende Läufe sicher.

### 7. MCP-Server registrieren

#### bei Claude:

Zu deiner Claude-MCP-Konfiguration hinzufügen (`~/.config/claude/mcp_servers.json`):

```json
{
  "memorandum": {
    "command": "/path/to/memorandum/.venv/bin/python",
    "args": ["/path/to/memorandum/mcp_server/server.py"],
    "cwd": "/path/to/memorandum",
    "timeout": 120
  }
}
```

#### bei Hermes:

Zur Hermes-Konfiguration hinzufügen (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  memorandum:
    command: /path/to/memorandum/.venv/bin/python
    args:
      - /path/to/memorandum/mcp_server/server.py
      - --config
      - /path/to/memorandum/config.yaml
    timeout: 120
```

Das `--config`-Argument stellt sicher, dass der Server `config.yaml` findet, selbst wenn das Arbeitsverzeichnis nicht greift.

### 8. (Optional) Live-Dashboard

Sobald Ingest planmäßig läuft, gibt das Terminal-TUI eine Ein-Bildschirm-Ansicht von Speicher, Ingest-Gesundheit, Mentions, Sendeaktivität und MCP-Tool-Nutzung — praktisch in einer tmux-Kachel:

```bash
./bin/memorandum dashboard
```

Aktualisiert sich alle 5 Sekunden; mit `q` verlassen.

![Dashboard Screenshort](assets/dashboard.png)

## Projektstruktur

```
memorandum/
├── config.yaml              # nicht-sensible Einstellungen (Quellen, Filter, Aliases) — gitignored
├── config.example.yaml      # Beispielkonfiguration
├── secrets.example.yaml     # Vorlage für /etc/memorandum/secrets.yaml (chmod 600; im Repo — enthält keine echten Credentials)
├── requirements.txt         # Python-Abhängigkeiten
├── requirements-dev.txt     # Dev-Abhängigkeiten (pytest, pytest-cov, responses)
│
├── connectors/                  # Quell-Konnektoren
│   ├── CONTRIBUTING.md          # ★ Wie man einen neuen Konnektor hinzufügt — erst lesen, bevor du diesen Ordner erweiterst
│   ├── _common.py               # Geteilte Konstanten (Preview-Größe, Default-Text-Extensions)
│   ├── factory.py               # build_connector — eine Baustelle für Ingest und MCP
│   ├── mattermost_connector.py  # Mattermost REST API (Per-Channel-Sync)
│   ├── telegram_connector.py    # Telegram Bot API (Gruppen, Channels, Business-Msgs; Bot-DMs werden übersprungen)
│   ├── pachca_connector.py      # Pachca REST API (Per-Chat-Cursor-Sync)
│   └── email_connector.py       # IMAP (Ordner-pro-Kanal; Message-ID-Threading; Senden = Draft)
│
├── pipeline/                # Ingest-Engine (läuft unter systemd)
│   ├── ingest.py            # Orchestriert fetch → filter → store, ein Konnektor pro Quelle
│   ├── format.py            # Kanonischer Nachrichten-Renderer (geteilt zwischen MCP-Server + Dashboard)
│   ├── health.py            # Health-Report-Builder + Formatter (von CLI und MCP geteilt)
│   ├── alias_resolver.py    # Kanonische Identitätsauflösung aus user_aliases-Config
│   └── filter_engine.py     # YAML-basiertes Filtern pro Quelle
│
├── cli/                     # Nutzerorientierte CLI-Tools (`python -m cli ...` / `bin/memorandum`)
│   ├── __main__.py          # argparse-Dispatcher
│   ├── health.py            # `memorandum health` — kapselt pipeline.health
│   ├── aliases.py           # `memorandum aliases refresh` — Append-Only-Stub-Generator
│   ├── alias_writer.py      # Gemeinsame YAML-Round-Trip-Schicht (genutzt von refresh + MCP-Write-Tools)
│   ├── prune.py             # `memorandum prune` — Dry-Run-Retention-Vorschau / --commit
│   ├── dashboard.py         # `memorandum dashboard` — Live-rich-TUI (Read-only-DB-Verbindung)
│   └── reindex.py           # `memorandum reindex-chroma` — Chroma löschen und aus SQLite neu aufbauen
│
├── storage/                 # Storage-Schicht
│   ├── db.py                # SQLite-Metadaten-Store
│   └── vector_store.py      # ChromaDB-Embeddings
│
├── mcp_server/              # MCP-Server
│   ├── server.py            # App + Dispatcher + Accessors + main
│   ├── schemas.py           # Tool()-Deklarationen, die Claude per Introspection sieht
│   ├── projectors.py        # Pro-Tool-Args-Redaktion fürs tool_calls-Audit-Log
│   └── tools/               # Ein Modul pro Domäne (search, digests, channels, threads,
│       │                    # identity, files, info); flaches TOOL_HANDLERS-Register
│       └── …
│
├── data/                    # Lokale Speicherung (gitignored)
│   ├── messages.db          # SQLite-Datenbank
│   ├── chroma/              # ChromaDB-Persistenz
│   └── attachments/         # Heruntergeladene Nachrichtenanhänge
│
├── systemd/                         # Linux-Deployment
│   ├── memorandum-collect.service   # Systemd-Oneshot-Service
│   └── memorandum-collect.timer     # Systemd-Timer (alle 15 Min.)
|
├── bin/                     # Skripte
│   ├── memorandum-sync      # Haupt-Sync-Skript mit Lock-Schutz
│   └── memorandum           # CLI-Wrapper — führt `python -m cli "$@"` im venv aus
│
├── tests/                   # Unit-Tests (pytest)
│   ├── conftest.py          # Gemeinsame Fixtures
│   ├── test_config.py
│   ├── test_filter_engine.py
│   ├── test_db.py
│   ├── test_server.py
│   ├── test_ingest.py
│   ├── test_mattermost_connector.py
│   ├── test_telegram_connector.py
│   ├── test_pachca_connector.py
│   ├── test_alias_resolver.py
│   ├── test_health.py
│   ├── test_youtrack_helpers.py
│   ├── test_cli_main.py
│   └── test_cli_aliases.py
│
├── setup.sh                 # Setup für macOS/Linux
├── run_ingest.sh            # Einmaliger Ingest-Test
└── README.md
```

## Verfügbare Tools (MCP)

| Tool                 | Beschreibung                                                |
| -------------------- | ----------------------------------------------------------- |
| `search_messages`    | Suche nach Stichwort oder semantischer Bedeutung            |
| `summarize_channel`  | Nachrichten aus einem bestimmten Kanal zum Zusammenfassen abrufen |
| `summarize_messages` | Digest von Nachrichten aus einem flexiblen Zeitraum (Stunden/Tage) |
| `list_channels`      | Bekannte Kanäle (id + Name + Beschreibung) aus der Datenbank auflisten |
| `get_new_messages`   | Nachrichten neuer als die DB für einen Kanal live aus der Quelle abrufen (alle Quellen) |
| `get_thread`         | Vollständigen Thread (Root + Antworten) per `thread_id` rekonstruieren |
| `get_stats`          | Nachrichtenstatistiken pro konfigurierter Quelle            |
| `get_attached_file`  | Dateiinhalt per file_id abrufen (Telegram, Mattermost, Pachca) |
| `get_user_aliases`   | Konfigurierte Identitätsaliase und aktuelle Benutzeraliase anzeigen |
| `get_health`         | Status des letzten Ingest-Laufs, Frische pro Quelle, Fehler |
| `send_message`       | Textnachricht an einen Kanal senden (opt-in via `allow_send`; alle Quellen) |
| `find_by_issue`      | Nachrichten finden, die auf eine YouTrack-Issue-ID verweisen (Links + Kanalname-Match) |
| `who_mentioned`      | Nachrichten finden, in denen jemand eine Person @-erwähnt hat (mit Alias-Auflösung; `target: "me"` funktioniert) |
| `upsert_user_alias`  | Erkenntnisse über eine Person (Rolle / Team / Aliase / `responsible_for`) in der dauerhaften Memory-Schicht persistieren |
| `remove_user_alias`  | Einen user_aliases-Eintrag löschen; my_aliases-Ziele werden abgelehnt |
| `update_user_alias_strings` | Einzelne Alias-Handles eines bestehenden Eintrags hinzufügen/entfernen; kanonkanonisches Klauen wird abgelehnt |

### send_message

Sendet eine Textantwort an einen Kanal — die Aktions-Hälfte der read→act-Schleife. Zwei Sicherheitsleitplanken:

- **Opt-in pro Quelle** (Default: verweigern): Das Tool verweigert, solange die Quelle nicht `allow_send: true` in `config.yaml` gesetzt hat. Sendungen sind für andere sichtbar, daher standardmäßig aus.
- **Read-before-send**: Der Agent muss `get_new_messages` für den Kanal direkt vor dem Senden aufrufen; sind neue Nachrichten erschienen, wird der Send abgebrochen und die Antwort mit dem neuen Kontext neu überdacht.

Argumente: `source`, `channel` (die Kanal-**id** aus `list_channels`), `text` und optional `reply_to` (Mattermost-Root-Post-ID / Telegram-Message-ID / Pachca-Parent-Message-ID), um die Antwort einzufädeln. Senden von Dateianhängen wird noch nicht unterstützt.

### Parameter von summarize_messages

| Parameter      | Typ    | Standard | Beschreibung                                                |
| -------------- | ------ | -------- | ----------------------------------------------------------- |
| `hours`        | int    | -        | N Stunden zurückblicken (z. B. 4, 24, 168). Überschreibt `days` |
| `days`         | int    | 1        | N Tage zurückblicken                                        |
| `source`       | string | -        | Nach Quellnamen filtern (z. B. `company_mattermost`)        |
| `channel`      | string | -        | Nach Kanalnamen filtern                                     |
| `max_messages` | int    | 100      | Maximale Nachrichten pro Kanal                              |

Verwende `get_stats`, um die in deiner Instanz konfigurierten Quellnamen zu sehen.

## Tests

```bash
# Dev-Abhängigkeiten installieren
pip install -r requirements-dev.txt

# Tests ausführen
pytest tests/ -v --tb=short

# Mit Coverage-Report ausführen
pytest tests/ --cov=. --cov-report=term-missing --ignore=storage/vector_store.py
```

Die Test-Suite (~640 Tests) deckt Config- + Secrets-Laden, Filtern, SQLite-Storage (RLock-Thread-Safety), MCP-URL-Generierung und Tool-Handler, alle vier Konnektoren (HTTP / IMAP gemockt) inkl. des `ConnectorProtocol`-Vertrags, den Ingest-Orchestrator (VectorStore gemockt — kein BGE-M3-Modell geladen), den CLI-Dispatcher und den `aliases refresh`-Round-Trip durch `ruamel.yaml` ab.

## Ingest-Optionen

```bash
# Normale Synchronisation (verwendet gespeicherten Kanal-Status)
./run_ingest.sh

# Vollständigen Scan ab vor 24 Stunden erzwingen
./run_ingest.sh --hours 24 --force

# Debug-Modus
./run_ingest.sh --debug
```

## CLI-Tools

Nutzerorientierte Tools liegen unter `cli/`. Der Wrapper `./bin/memorandum` löst das venv für dich auf; andernfalls `python -m cli <verb>` aus einem aktivierten venv aufrufen.

```bash
./bin/memorandum health                          # Ingest-Status + Frische pro Quelle
./bin/memorandum health --json                   # maschinenlesbar
./bin/memorandum aliases refresh                 # Stub-user_aliases-Einträge für neue Absender drucken
./bin/memorandum aliases refresh --in-place      # Diese Stubs in config.yaml anhängen
./bin/memorandum reindex-chroma                  # Vector Store löschen und aus SQLite neu aufbauen
```

`reindex-chroma` nimmt denselben `/tmp/memorandum-sync.lock` wie `bin/memorandum-sync` — ein laufender Sync (oder ein zweiter Reindex) blockiert ihn sauber, statt zu racen. Nutze ihn, um eine beschädigte Chroma-Directory wiederherzustellen, Metadaten nach einem Schema-Fix nachzuladen oder als Wiederaufbauschritt beim Wechsel des Embedding-Modells.

Exit-Codes für `health`: `0`=OK, `1`=teilweise/Fehler, `2`=nie gelaufen — nutzbar als Monitoring-Check (`./bin/memorandum health && echo healthy || echo check logs`). Dieselben Daten sind aus Claude via MCP-Tool `get_health` verfügbar.

`aliases refresh` ist **append-only**: Es diffeed die Absender in der DB gegen deine bestehenden `user_aliases`-Einträge und emittiert Stubs (sortiert nach Nachrichtenanzahl) für noch nicht abgedeckte. Bestehende Einträge werden nie bearbeitet oder umsortiert; `--in-place` nutzt `ruamel.yaml`-Round-Trip, sodass Kommentare in deiner `config.yaml` intakt bleiben.

> `python -m pipeline health` (die alte Form) druckt jetzt eine einzeilige Umleitung und beendet mit 2 — nutze `python -m cli health` (oder den Wrapper oben).

## Linux-Deployment (systemd)

Für Produktion auf Linux mit systemd:

```bash
# Service- und Timer-Dateien kopieren
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/

# logrotate-Config für Sync-Logs installieren
sudo cp systemd/memorandum-sync.logrotate /etc/logrotate.d/memorandum-sync

# Pfade in der Service-Datei bearbeiten
sudo vim /etc/systemd/system/memorandum-collect.service
# WorkingDirectory und ExecStart an deine Installation anpassen

# Timer aktivieren und starten
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer

# Status prüfen
sudo systemctl status memorandum-collect.timer
sudo systemctl list-timers

# Logs einsehen
journalctl -u memorandum-collect -f

# Sync-Log einsehen
tail -f /var/log/memorandum-sync.log

# Manueller Lauf (falls nötig)
sudo systemctl start memorandum-collect
```

## Logging

Das Sync-Skript (`bin/memorandum-sync`) loggt nach:
- `/var/log/memorandum-sync.log` auf Linux (falls /var/log beschreibbar)
- `data/memorandum-sync.log` im Projektverzeichnis (Fallback)

Logs werden täglich rotiert und 7 Tage lang aufbewahrt — gesteuert von der oben installierten logrotate-Config.

## Systemvoraussetzungen

- Python 3.11+
- Virtuelle Umgebung (`.venv`)
- Ein Mattermost Personal Access Token, Telegram Bot Token und/oder Pachca Personal Access Token
- ~4,5 GB Speicher für Modell + Daten (Standard BGE-M3; weniger mit einem kleineren Modell — siehe [Embedding-Modell austauschen](#embedding-modell-austauschen))
- ~2–2,5 GB RAM für BGE-M3-Embeddings (Standard; ein kleines englisches Modell passt in ~300 MB)

## Embedding-Modell austauschen

Modell und Tuning des Vector Stores leben in `config.yaml` unter `embedding:`. Den Block weglassen, um den BGE-M3-Standard zu behalten; eine beliebige Untermenge dieser Schlüssel überschreiben:

```yaml
embedding:
  model: "BAAI/bge-m3"       # jede FlagEmbedding-kompatible Modell-ID
  device: "cpu"              # "cpu", "cuda" oder "mps"
  use_fp16: true
  max_length: 512
  batch_size: 1
  collection_name: "messages"
```

Vorgeschlagene Alternativen:
- `BAAI/bge-m3` — mehrsprachig, ~4 GB auf der Platte, **1024-dim** (Standard)
- `BAAI/bge-small-en-v1.5` — nur Englisch, ~130 MB, **512-dim** (schnell, wenig RAM)

**Wichtig — Dimensionalität:** Chroma speichert Vektoren mit fester Dimension pro Sammlung. Wenn du `model:` auf ein anderes Modell (oder eine andere Ausgabegröße) zeigst, bricht die Ähnlichkeitssuche lautlos, solange nicht alle Dokumente neu eingebettet werden. Memorandum gibt beim ersten Insert einen klaren Fehler aus, falls die Dimension der bestehenden Sammlung nicht zum konfigurierten Modell passt — wähle aber trotzdem vor dem Wechsel einen Migrationspfad:

1. **Alte Vektoren behalten.** `collection_name:` auf einen neuen Wert setzen (z. B. `messages_bge_small`). Die alte Sammlung bleibt auf der Platte; das neue Modell befüllt die neue.
2. **Sauberer Neuanfang.** `./bin/memorandum reindex-chroma` ausführen — der Befehl holt den Sync-Lock, löscht das Chroma-Verzeichnis und bettet jede Nachricht aus SQLite mit dem konfigurierten Modell neu ein.

## Memorandum erweitern

### Einen neuen Quell-Konnektor hinzufügen (Slack, Discord, Matrix, …)

Die vier eingebauten Konnektoren sind eine kleine Oberfläche, und das übrige System lässt sich natürlich auf einen fünften erweitern. Die Anleitung — Interface-Contract, Message-Dict-Form, Inkremental-Sync-Pattern, Dateianhänge, die vier Dispatch-Stellen, die du verdrahten musst, zu schreibende Tests und die Stolpersteine, die die bestehenden Konnektoren beim Bau getroffen haben — liegt unter **[connectors/CONTRIBUTING.md](connectors/CONTRIBUTING.md)**. Vor dem Code-Schreiben einmal ganz durchlesen; der Vertrag ist klein, aber die *Reihenfolge* und die *Invarianten* zählen.

## Was ausgeliefert wurde, was als Nächstes kommt

[**CHANGELOG.md**](CHANGELOG.md) ist das Entscheidungs-Log — jedes gelandete Feature mit kurzer Begründung und den berührten Dateipfaden. Nutzbar sowohl als „Was steckt in diesem Build" als auch als „Warum wurde X so gebaut"-Referenz für Mitwirkende.

Für geplante Arbeiten und Bug-Reports [GitHub Issues](../../issues) nutzen (Templates vorhanden); für Design-Fragen [Discussions](../../discussions).
