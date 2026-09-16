# Kryptokonzept

Welche kryptografischen Verfahren mandari einsetzt, mit welchen Parametern, wer
Zugriff auf Schlüssel hat und was bei Verlust passiert.

Bezug: BSI IT-Grundschutz **CON.1 Kryptokonzept**, Parameterwahl abgeglichen mit
**BSI TR-02102-1** (kryptografische Verfahren) und **TR-02102-2** (TLS).

Stand: 16.09.2026, geprüft gegen Version 0.10.0 im Produktivbetrieb.

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

Insgesamt sind **38 Felder in 9 Modulen** verschlüsselt, mit dem Schwerpunkt im
Verwaltungs-RIS (13) sowie bei Sitzungsvorbereitung und Fraktionsarbeit (je 8).

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
        ├── verschlüsselt ──► Mandantenschlüssel je Organisation
        │                     (in der Datenbank, mit vorangestellter Nonce)
        │
        └── Mandantenschlüssel ──► Feldinhalte
```

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
| Wechsel | `manage.py rotate_encryption` — wechselt Hauptschlüssel **und** alle Mandantenschlüssel |
| Vernichtung | Mit dem Löschen der Organisation entfällt der Mandantenschlüssel |

### Schlüsselwechsel

`apps/common/management/commands/rotate_encryption.py` wechselt den Hauptschlüssel
und alle Mandantenschlüssel. Der alte Hauptschlüssel kommt über `--old-master-key`
oder `OLD_MASTER_KEY`, der neue steht in `ENCRYPTION_MASTER_KEY`. Die zu
verschlüsselnden Felder werden **selbst ermittelt**, es gibt einen `--dry-run`,
und je Mandant läuft eine Transaktion.

Entstanden ist der Befehl für den Umzug einer Datenbank zwischen Servern. Für den
Ernstfall — ein bekannt gewordener Hauptschlüssel — hat er drei Schwächen:

1. **Er kann nur alles auf einmal.** Ein bekannt gewordener *Hauptschlüssel*
   erfordert eigentlich nur, die wenigen Mandantenschlüssel neu einzupacken —
   die Feldinhalte sind davon gar nicht betroffen. Heute werden trotzdem alle
   38 Felder aller Datensätze neu verschlüsselt. Das ist um Größenordnungen mehr
   Arbeit und Risiko als nötig.
2. **Er lädt die Datensätze vollständig in den Speicher** (`for obj in objects`
   ohne `.iterator()`). Bei den großen Tabellen ist das genau das Muster, das
   an anderer Stelle schon zu Speicherabbrüchen geführt hat.
3. **Er ist nicht fortsetzbar.** Bricht er beim dritten von vier Mandanten ab,
   sind zwei umgestellt und zwei nicht. Jeder Mandant ist für sich stimmig, der
   Gesamtstand aber uneinheitlich — und ein zweiter Lauf müsste wissen, wo er
   weitermacht.

Hilfreich ist dabei eine Eigenschaft des gewählten Verfahrens: **AES-GCM ist
authentifiziert.** Eine Entschlüsselung mit dem falschen Schlüssel schlägt
sauber fehl, statt Unsinn zu liefern. Damit lässt sich ein Übergang ohne
Ausfallzeit bauen, in dem beide Schlüssel nebeneinander gelten und der jeweils
passende einfach ausprobiert wird — ohne Versionsspalten an 38 Feldern.

Die Verbesserung dieser drei Punkte ist als Arbeitspaket beschrieben.

## 5. Rollen und Zugriff

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

## 6. Notfallvorsorge

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

Die Verzahnung mit dem Notfall- und Wiederanlaufkonzept ist Gegenstand von
Issue #229.

## 7. Für Selbst-Hoster

Wer mandari selbst betreibt, übernimmt die Verantwortung für den Hauptschlüssel.
Das Wichtigste in drei Sätzen:

1. `ENCRYPTION_MASTER_KEY` einmal erzeugen, sicher hinterlegen und **nie ändern**,
   solange es kein Wechselverfahren gibt.
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
| Schlüsselwechsel | vorhanden (`rotate_encryption`), für den Ernstfall aber zu grob, speicherhungrig und nicht fortsetzbar |
| Notfallvorsorge | beschrieben, Übung über #229 zu verzahnen |
