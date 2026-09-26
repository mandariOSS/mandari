# Kryptokonzept

Welche kryptografischen Verfahren mandari einsetzt, mit welchen Parametern, wer
Zugriff auf Schlüssel hat und was bei Verlust passiert.

Bezug: BSI IT-Grundschutz **CON.1 Kryptokonzept**, Parameterwahl abgeglichen mit
**BSI TR-02102-1** (kryptografische Verfahren) und **TR-02102-2** (TLS).

Stand: 16.09.2026, geprüft gegen Version 0.10.0 im Produktivbetrieb. Schlüsselwechsel
(Abschnitt 5) überarbeitet am 26.09.2026; am selben Tag Systemeinstellungen und
Editor-Zustand in die Verschlüsselung aufgenommen (Abschnitt 1).

---

## 1. Schutzbedarf je Datenart

mandari verarbeitet Daten mit deutlich unterschiedlichem Schutzbedarf. Die
Verschlüsselung folgt dieser Unterscheidung, statt pauschal alles zu verschlüsseln.

| Datenart | Schutz | Begründung |
|---|---|---|
| Nicht-öffentliche Sitzungsinhalte, Vermerke, Positionen, private Notizen | **Feldverschlüsselung** | Kern des Geschäftsgeheimnisses der Fraktionsarbeit und der nicht-öffentlichen Verwaltungsvorgänge |
| Personenbezogene Stammdaten im Verwaltungs-RIS (u. a. Bankverbindungen für Sitzungsgelder) | **Feldverschlüsselung** | Besondere Sensibilität, Zweckbindung |
| Support-Nachrichten | **Feldverschlüsselung** | Können Betriebsinterna der Kommune enthalten |
| Passwörter | **Hash, nicht umkehrbar** | Dürfen nie wiederherstellbar sein |
| Zweiter Faktor (TOTP-Geheimnis, Backup-Codes) | **Feldverschlüsselung** | Ein Klartext-Geheimnis entwertet den zweiten Faktor |
| Öffentliche Ratsinformationen (OParl-Bestand) | **keine** | Sind per Gesetz öffentlich; Verschlüsselung brächte keinen Schutz, aber Kosten bei Suche und Auslieferung |
| Protokolle und Audit-Log | **keine Inhaltsverschlüsselung** | Enthalten bewusst keine personenbezogenen Inhalte; Schutz über Zugriffsrechte |

Insgesamt sind **41 Felder** verschlüsselt:

- **34 mit dem Mandantenschlüssel** – Verwaltungs-RIS (14), Sitzungsvorbereitung und
  Fraktionsarbeit (11), Anträge (3: Inhalt, Versionen und der Zustand des gemeinsamen
  Editors), Support (2), Protokollassistenz (2) sowie SMTP-Passwort und KI-Schlüssel der
  Organisation (2)
- **5 mit dem Hauptschlüssel** – plattformweite Zugangsdaten: SMTP-Passwort und
  Nebius-Schlüssel der Systemeinstellungen, KI-Anbieter, GPU-Rechenknoten (2)
- **2 für den zweiten Faktor** – TOTP-Geheimnis und Backup-Codes

Dazu kommen die Mandantenschlüssel selbst, eingepackt mit dem Hauptschlüssel. Maßgeblich
ist das Verzeichnis `mandari/apps/common/crypto_registry.py`: Dort steht jedes
verschlüsselte Feld mit seiner Schlüsselart, und ein Test schlägt fehl, sobald ein Feld
fehlt. Ein weiterer Test schlägt an, sobald ein Feld, dessen Name nach Passwort oder
Schlüssel klingt, weder verschlüsselt eingetragen noch begründet ausgenommen ist
(etwa der Passwort-Hash).

Die Geheimnisse der Systemeinstellungen und der Editor-Zustand der Dokumente sind seit dem
26.09.2026 verschlüsselt. Die Migrationen `common/0006` und `work/0057` verschlüsseln den
Bestand und leeren die früheren Spalten; die Spalten selbst entfallen mit einer Folgeversion.

## 2. Eingesetzte Verfahren

### 2.1 Feldverschlüsselung

| Eigenschaft | Umsetzung |
|---|---|
| Verfahren | **AES-256-GCM** (`cryptography`, `AESGCM`) |
| Schlüssellänge | 256 Bit |
| Nonce | 96 Bit aus `os.urandom`, je Vorgang neu, dem Geheimtext vorangestellt |
| Authentizität | GCM-Authentifizierungs-Tag, 128 Bit |

**Abgleich TR-02102-1:** AES mit 256 Bit im GCM-Betrieb ist empfohlen. Die
Nonce-Länge von 96 Bit entspricht der Vorgabe, die Erzeugung aus einer
kryptografisch sicheren Quelle ebenfalls. Die Kombination aus Verschlüsselung
und Authentizität in einem Schritt (AEAD) ist die empfohlene Bauweise.
**Konform, kein Anpassungsbedarf.**

### 2.2 Transportverschlüsselung

Gemessen am Produktivsystem am 16.09.2026:

| TLS-Version | Verhalten |
|---|---|
| TLS 1.3 | akzeptiert |
| TLS 1.2 | akzeptiert |
| TLS 1.1 | abgewiesen |
| TLS 1.0 | abgewiesen |

Ausgehandelt wird `TLS_AES_128_GCM_SHA256` über TLS 1.3, Zertifikatsprüfung
fehlerfrei. HSTS ist mit einem Jahr und `includeSubDomains` gesetzt; die
Aufnahme in die Preload-Liste ist bewusst nicht erfolgt, weil sie schwer
rückgängig zu machen ist.

**Abgleich TR-02102-2:** TLS 1.2 als Mindestversion, TLS 1.3 bevorzugt, alte
Versionen abgewiesen — entspricht der Empfehlung. **Konform.**

### 2.3 Passwörter

| Eigenschaft | Umsetzung |
|---|---|
| Verfahren | **PBKDF2-HMAC-SHA256** |
| Iterationen | **1.200.000** |
| Salt | je Passwort, von Django erzeugt |
| Mindestlänge | 12 Zeichen |

**Entscheidung zu Argon2id:** Argon2id gilt heute als das modernere Verfahren,
weil es zusätzlich Arbeitsspeicher bindet und damit Angriffe mit
Spezialhardware verteuert. PBKDF2 mit SHA-256 bleibt gleichwohl ein zugelassenes
Verfahren, und 1,2 Millionen Iterationen liegen deutlich über den üblichen
Mindestempfehlungen.

Wir bleiben vorerst bei PBKDF2, aus zwei Gründen: Es ist ohne zusätzliche
Abhängigkeit verfügbar, und ein Wechsel würde bei Selbst-Hostern eine weitere
native Bibliothek erforderlich machen. `Argon2PasswordHasher` ist in der
Hasher-Liste bereits vorgesehen — ein späterer Wechsel greift beim nächsten
Login jedes Kontos automatisch und braucht keine Migration.

**Diese Entscheidung ist zu überprüfen**, sobald Argon2 in der Zielumgebung
ohnehin vorhanden ist oder eine Vergabestelle es ausdrücklich verlangt.

### 2.4 Zufallszahlen

Geheimnisse, Einladungs- und Prüf-Token stammen aus `secrets` (`token_urlsafe`,
`token_hex`, `token_bytes`), Schlüssel und Nonces aus `os.urandom`, Kennungen aus
`uuid4`. Eine Prüfung des gesamten Anwendungscodes ergab **keine einzige
Verwendung des `random`-Moduls** für sicherheitsrelevante Zwecke. **Konform.**

## 3. Schlüsselhierarchie

```
ENCRYPTION_MASTER_KEY          (32 Byte, aus der Umgebung, nie in der Datenbank)
        │
        ├── verschlüsselt ──► Mandantenschlüssel je Organisation und Session-Mandant
        │                     (in der Datenbank, mit vorangestellter Nonce)
        │                           └──► Feldinhalte, Zugangsdaten der Organisation
        │
        ├── verschlüsselt ──► plattformweite Zugangsdaten (Systemeinstellungen, KI-Anbieter,
        │                     GPU-Rechenknoten)
        │
        └── abgeleitet ────► 2FA-Schlüssel ──► TOTP-Geheimnisse, Backup-Codes
```

Der 2FA-Schlüssel wird nicht gespeichert, sondern zweckgebunden aus dem Hauptschlüssel
abgeleitet (SHA-256 mit festem Kontext) und mit Fernet genutzt (AES-128-CBC mit
HMAC-SHA256, ebenfalls authentifiziert). Er wechselt deshalb mit dem Hauptschlüssel.

Der Hauptschlüssel liegt ausschließlich in der Umgebung des Anwendungscontainers
(`.env` beziehungsweise Kubernetes-Secret) und wird **nie** persistiert. Die
Mandantenschlüssel liegen verschlüsselt in der Datenbank. Daraus folgt: Eine
Kopie der Datenbank allein — etwa ein entwendetes Backup — gibt keine
verschlüsselten Inhalte preis.

Die Trennung je Mandant bedeutet außerdem, dass ein kompromittierter
Mandantenschlüssel nicht die Daten anderer Organisationen betrifft.

## 4. Lebenszyklus der Schlüssel

| Phase | Umsetzung |
|---|---|
| Erzeugung Hauptschlüssel | Einmalig beim Aufsetzen, `secrets.token_bytes(32)`, Base64 |
| Erzeugung Mandantenschlüssel | Automatisch beim Anlegen einer Organisation |
| Aufbewahrung | Hauptschlüssel in der Umgebung, getrennt vom Server gesichert |
| Wechsel | `manage.py rotate_encryption` — Hauptschlüssel-Ebene und Mandantenschlüssel getrennt oder gemeinsam, ohne Ausfallzeit; Ablauf in Abschnitt 5 |
| Vernichtung | Mit dem Löschen der Organisation entfällt der Mandantenschlüssel; ein vorheriger Mandantenschlüssel wird mit dem Abschluss eines Wechsels gelöscht |

## 5. Schlüsselwechsel (Routine und Notfall)

### 5.1 Welche Schlüssel es gibt

| Schlüssel | Liegt in | Schützt | Wechsel |
|---|---|---|---|
| Hauptschlüssel `ENCRYPTION_MASTER_KEY` | Umgebung des Anwendungscontainers | Mandantenschlüssel, plattformweite Zugangsdaten, mittelbar den zweiten Faktor | neuer Wert in der Umgebung, danach `rotate_encryption --master-only` (5.3) |
| 2FA-Schlüssel | nirgends, wird aus dem Hauptschlüssel abgeleitet | TOTP-Geheimnisse, Backup-Codes | wechselt mit dem Hauptschlüssel |
| Mandantenschlüssel | Datenbank, mit dem Hauptschlüssel eingepackt | alle verschlüsselten Inhalte einer Organisation bzw. eines Session-Mandanten, SMTP-Passwort und KI-Schlüssel der Organisation | `rotate_encryption`, nach einem Neustart `rotate_encryption --finalize` (5.4) |

Welches Feld mit welchem Schlüssel verschlüsselt ist, steht an genau einer Stelle: im
Verzeichnis `mandari/apps/common/crypto_registry.py`. Der Befehl arbeitet ausschließlich
damit. Ein Test schlägt fehl, sobald ein Binärfeld oder ein verschlüsseltes Textfeld dort
weder eingetragen noch begründet als unverschlüsselt aufgeführt ist. Wer ein neues
verschlüsseltes Feld anlegt, trägt es mit seiner Schlüsselart ein.

`SECRET_KEY` ist kein Schlüssel für gespeicherte Inhalte. Er signiert Sitzungen und
Links; ein Wechsel meldet alle Konten ab und macht versandte Einladungs- und
Rückmeldelinks ungültig, gespeicherte Daten bleiben unberührt.

### 5.2 So funktioniert der Wechsel

- **Zwei Schlüssel im Übergang.** Während eines Wechsels gelten alter und neuer
  Schlüssel nebeneinander: gelesen wird mit beiden, geschrieben nur mit dem neuen. Für den
  Hauptschlüssel steht der alte in `ENCRYPTION_MASTER_KEY_PREVIOUS`, für einen
  Mandantenschlüssel in der Datenbank neben dem neuen. Die Anwendung läuft währenddessen
  ohne Unterbrechung weiter. Möglich ist das, weil AES-GCM und Fernet authentifiziert
  sind: Ein falscher Schlüssel schlägt sauber fehl, der passende wird ausprobiert.
- **Erst prüfen, dann ändern.** Jeder Lauf beginnt mit einer Bestandsaufnahme. Findet sie
  Werte, die sich mit keinem der Schlüssel lesen lassen, bricht er ab, bevor irgendetwas
  geändert ist, und nennt Feld und Datensatzkennung.
- **Transaktional.** Die Hauptschlüssel-Ebene (eingepackte Mandantenschlüssel,
  Zugangsdaten, zweiter Faktor) wird in einer einzigen Transaktion umgeschrieben. Ein
  neuer Mandantenschlüssel entsteht in einer Transaktion zusammen mit dem Umschlüsseln
  aller Inhalte dieses Mandanten. Scheitert etwas, bleibt die betroffene Einheit
  vollständig im alten Zustand.
- **Fortsetzbar.** Was schon mit dem aktuellen Schlüssel lesbar ist, wird übersprungen;
  Mandanten mit begonnenem Wechsel erhalten keinen weiteren neuen Schlüssel. Nach einem
  Abbruch wird derselbe Befehl einfach wiederholt.
- **Speicherschonend.** Die Datensätze werden seitenweise gelesen, nicht vollständig
  geladen; Dokumentinhalte und Editor-Zustände, die mit eingebetteten Bildern mehrere MB
  groß sein können, in kleineren Seiten.
- **Keine Werte in der Ausgabe.** Ausgegeben und protokolliert werden nur Anzahlen und
  Datensatzkennungen.
- **`--dry-run`** zeigt vorab je Feld, wie viele Werte mit welchem Schlüssel verschlüsselt
  sind und was ein Lauf tun würde. Er ändert nichts.

Die Befehle laufen im Anwendungscontainer (`python manage.py …`). Vor jedem Wechsel
gehört eine frische Datenbanksicherung dazu.

### 5.3 Routine: Hauptschlüssel wechseln

1. Bestandsaufnahme: `python manage.py rotate_encryption --dry-run`. Die Spalte
   „unlesbar“ muss überall 0 zeigen.
2. Neuen Schlüssel erzeugen und getrennt vom Server sicher hinterlegen:
   `python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"`
3. In der Umgebung aller Dienste, die `ENCRYPTION_MASTER_KEY` erhalten (Anwendung,
   Steuerung der Rechenknoten, Migrationsjob), den neuen Schlüssel eintragen und den
   bisherigen in `ENCRYPTION_MASTER_KEY_PREVIOUS`. Dienste neu starten. Ab jetzt wird mit
   dem neuen Schlüssel geschrieben, alles Bisherige bleibt lesbar.
   - Docker Compose: beide Variablen in der `.env`. Wer eine eigene Compose-Datei
     betreibt, muss `ENCRYPTION_MASTER_KEY_PREVIOUS` dort ebenfalls an die Dienste
     weiterreichen.
   - Helm: `secrets.encryptionKey` (neu) und `secrets.encryptionKeyPrevious` (bisher);
     bei einem eigenen Secret den Eintrag `encryption-key-previous` ergänzen.
4. `python manage.py rotate_encryption --master-only`. Das dauert Sekunden. Die Prüfung
   am Ende muss „Werte mit vorherigem Hauptschlüssel: 0“ melden.
5. `ENCRYPTION_MASTER_KEY_PREVIOUS` leeren und die Dienste erneut neu starten.
6. Den alten Schlüssel so lange getrennt aufbewahren, wie Datenbanksicherungen aus der
   Zeit davor existieren – nur mit ihm lassen sie sich wiederherstellen.

### 5.4 Routine: Mandantenschlüssel wechseln

1. `python manage.py rotate_encryption --dry-run`
2. `python manage.py rotate_encryption` – erzeugt je Mandant einen neuen Schlüssel und
   verschlüsselt alle Inhalte neu. Der bisherige Schlüssel bleibt vorerst zum Lesen
   erhalten.
3. Anwendung neu starten. Danach schreibt keine Instanz mehr mit einem Schlüssel, den sie
   vor dem Wechsel geladen hat.
4. `python manage.py rotate_encryption --finalize` – verschlüsselt Nachzügler neu und
   löscht die bisherigen Mandantenschlüssel. Die Prüfung am Ende muss für beide
   Schlüsselarten 0 melden.

Mit `--tenant-type organization` oder `--tenant-type session` lässt sich der Wechsel auf
Organisationen (Work) oder Session-Mandanten beschränken.

Beide Wechsel lassen sich verbinden: Umgebung wie in 5.3 umstellen und neu starten, dann
`rotate_encryption` (statt `--master-only`), neu starten, `rotate_encryption --finalize`,
`ENCRYPTION_MASTER_KEY_PREVIOUS` leeren, neu starten.

### 5.5 Notfall: Hauptschlüssel bekannt geworden

Ein bekannt gewordener Hauptschlüssel wird in zwei Stufen ersetzt.

1. **Sofort (Minuten):** Schritte 2 bis 5 aus 5.3. Danach öffnet der alte Hauptschlüssel
   die aktuelle Datenbank nicht mehr.
2. **Noch am selben Tag:** zusätzlich die Mandantenschlüssel wechseln (5.4). Jede
   Sicherung aus der Zeit vor dem Wechsel enthält die Mandantenschlüssel, eingepackt mit
   dem alten Hauptschlüssel. Erst neue Mandantenschlüssel schützen auch künftige Inhalte
   vollständig.

Außerdem:

- **Geheimnisse selbst erneuern.** Ein Schlüsselwechsel verschlüsselt dieselben Werte neu,
  er ändert sie nicht. Ist auch die Datenbank oder eine Sicherung in falsche Hände geraten,
  gelten SMTP-Passwörter, KI-Schlüssel und die Zugangsdaten der Rechenknoten als bekannt
  und werden beim jeweiligen Anbieter erneuert. Konten richten ihren zweiten Faktor neu
  ein (`python manage.py reset_two_factor <E-Mail> --reason "…"`).
- **`SECRET_KEY` wechseln**, wenn er am selben Ort lag wie der Hauptschlüssel, etwa in
  derselben `.env`.
- **Sicherungen einordnen.** Sicherungen aus der Zeit vor dem Wechsel bleiben mit dem
  alten Schlüssel lesbar. Ob sie bis zum Ende der Aufbewahrungsfrist bleiben oder
  vorzeitig gelöscht werden, ist eine Abwägung zwischen Wiederherstellbarkeit und Schutz.

### 5.6 Wenn etwas schiefgeht

- **Abbruch auf der Hauptschlüssel-Ebene:** Es wurde nichts gespeichert. Befehl
  wiederholen oder, falls nötig, die Umgebung auf den alten Schlüssel zurückstellen.
- **Abbruch bei den Mandantenschlüsseln:** Bereits umgestellte Mandanten bleiben
  umgestellt, der betroffene vollständig beim alten Schlüssel. Alles bleibt lesbar,
  solange beide Hauptschlüssel gesetzt sind. **Die Umgebung dann nicht zurückstellen**,
  sondern den Befehl wiederholen – er setzt fort.
- **Unlesbare Werte:** Der Lauf bricht vor jeder Änderung ab und nennt Feld und
  Datensatzkennung. Meist fehlt ein alter Schlüssel. Nur wenn Werte nachweislich verloren
  sind, lässt `--ignore-unreadable` sie unverändert und stellt den Rest um.
- **Feld ohne festgelegte Schlüsselart:** Ein vorgesehenes Feld enthält Werte, ist im
  Verzeichnis aber noch keiner Schlüsselart zugeordnet. Der Lauf bricht ab; zuerst den
  Eintrag ergänzen.

### 5.7 Datenbank von einem anderen Server übernehmen

Soll eine Kopie unter eigenem Hauptschlüssel weiterlaufen (etwa als Testsystem), wird der
eigene `ENCRYPTION_MASTER_KEY` gesetzt und der Hauptschlüssel des Quellsystems dem Befehl
über die Umgebungsvariable `OLD_MASTER_KEY` mitgegeben: erst `rotate_encryption`, dann
`rotate_encryption --finalize`, beide mit `OLD_MASTER_KEY`. Danach enthält die Kopie
weder Hauptschlüssel- noch Mandantenschlüssel-Material des Quellsystems. Die
Zugangsdaten und TOTP-Geheimnisse selbst sind dieselben wie im Quellsystem.

## 6. Rollen und Zugriff

| Rolle | Zugriff auf Schlüssel |
|---|---|
| Betreiber der Installation | Hauptschlüssel (Umgebung, Sicherung) |
| Administration der Anwendung | **kein** direkter Zugriff; Entschlüsselung nur über die Anwendung im Rahmen der Berechtigungen |
| Datenbankadministration | nur verschlüsselte Mandantenschlüssel, nicht nutzbar ohne Hauptschlüssel |
| Nutzer:innen | keiner |

Der zweite Faktor ist bewusst **nicht** im Django-Admin sichtbar. Ein Zurücksetzen
erfolgt über `manage.py reset_two_factor --reason`, wobei der Anlass protokolliert
wird; ins Protokoll gelangt dabei nur die Anzahl entfernter Einträge, nie ein
Datensatz.

## 7. Notfallvorsorge

**Der Verlust des Hauptschlüssels bedeutet den unwiederbringlichen Verlust aller
verschlüsselten Inhalte.** Kein Backup der Datenbank kann das ausgleichen — das
ist die Kehrseite davon, dass der Schlüssel nicht in der Datenbank liegt.

Daraus folgt für den Betrieb:

- Der Hauptschlüssel wird **getrennt vom Server** aufbewahrt, idealerweise in
  einem Passwortverwalter mit eigener Sicherung
- Vor jeder Migration oder jedem Umzug wird geprüft, dass der Schlüssel verfügbar
  ist — **nicht erst danach**
- Eine Wiederherstellung wird regelmäßig geübt, einschließlich der Frage, ob der
  Schlüssel zur gesicherten Datenbank passt
- Nach einem Schlüsselwechsel gehört zu jeder älteren Sicherung der damalige
  Hauptschlüssel. Er wird aufbewahrt, bis die letzte dieser Sicherungen abgelaufen ist

Die Verzahnung mit dem Notfall- und Wiederanlaufkonzept ist Gegenstand von
Issue #229.

## 8. Für Selbst-Hoster

Wer mandari selbst betreibt, übernimmt die Verantwortung für den Hauptschlüssel.
Das Wichtigste in drei Sätzen:

1. `ENCRYPTION_MASTER_KEY` einmal erzeugen, sicher hinterlegen und **nie einfach
   austauschen** – ein Wechsel läuft ausschließlich über das Verfahren in Abschnitt 5.
2. Getrennt von der Datenbanksicherung aufbewahren — sonst liegen Schloss und
   Schlüssel im selben Karton, und die Trennung war umsonst.
3. Vor dem ersten Produktivbetrieb eine Wiederherstellung üben.

Für TLS genügt die mitgelieferte Caddy-Konfiguration; sie handelt TLS 1.3 aus und
weist alte Versionen ab.

---

## Zusammenfassung des Abgleichs

| Bereich | Bewertung |
|---|---|
| Feldverschlüsselung AES-256-GCM | konform zu TR-02102-1 |
| Transportverschlüsselung TLS 1.2/1.3 | konform zu TR-02102-2 |
| Zufallszahlen | konform |
| Passwort-Hashing PBKDF2, 1.200.000 Iterationen | zulässig; Wechsel auf Argon2id offen und begründet zurückgestellt |
| Schlüsselhierarchie und -trennung | konform |
| Schlüsselwechsel | vollständig über ein geprüftes Verzeichnis aller verschlüsselten Felder; transaktional, fortsetzbar, ohne Ausfallzeit; Hauptschlüssel im Notfall in Minuten gewechselt (Abschnitt 5) |
| Notfallvorsorge | beschrieben, Übung über #229 zu verzahnen |
