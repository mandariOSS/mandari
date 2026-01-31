# Sicherheitsrichtlinie

## Unterstützte Versionen

| Version | Unterstützt |
|---------|-------------|
| latest  | ✅          |
| < 1.0   | ❌          |

## Sicherheitslücke melden

**Bitte melde Sicherheitslücken NICHT über öffentliche GitHub Issues.**

### Prozess

1. **E-Mail senden** an: **security@mandari.de**
2. **Betreff**: `[SECURITY] Kurze Beschreibung`
3. **Inhalt**:
   - Beschreibung der Schwachstelle
   - Schritte zur Reproduktion
   - Mögliche Auswirkungen
   - Vorschläge zur Behebung (optional)

### Was du erwarten kannst

- **Bestätigung** innerhalb von 48 Stunden
- **Erste Einschätzung** innerhalb von 7 Tagen
- **Regelmäßige Updates** über den Fortschritt
- **Kredit** in den Release Notes (wenn gewünscht)

### Was wir von dir erwarten

- Gib uns angemessene Zeit zur Behebung, bevor du öffentlich berichtest
- Vermeide Datenzerstörung oder Dienstunterbrechungen während deiner Recherche
- Greife nicht auf Daten anderer Nutzer zu

## Sicherheitsmaßnahmen

Mandari implementiert folgende Sicherheitsmaßnahmen:

- **Verschlüsselung**: AES-256-GCM für sensible Daten
- **Authentifizierung**: TOTP-basierte 2FA
- **Rate Limiting**: Schutz vor Brute-Force
- **RBAC**: 50+ feingranulare Berechtigungen
- **Audit Trail**: Vollständige Nachverfolgbarkeit
- **Tenant Isolation**: Strikte Datentrennung

## Hall of Fame

Wir danken folgenden Personen für verantwortungsvolle Meldungen:

*Noch keine Einträge*

---

Danke, dass du hilfst, Mandari sicher zu halten!
