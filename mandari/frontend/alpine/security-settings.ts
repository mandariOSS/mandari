/**
 * Konto-Sicherheit (Alpine-Komponente `securitySettings`).
 *
 * Passwortstärke-Anzeige beim Passwortwechsel und der dreistufige 2FA-Einrichtungsdialog
 * (QR-Code, Bestätigung, Backup-Codes) sowie die Modals für Backup-Codes und Deaktivierung.
 * Die Passwortregeln kommen aus dem View (`password_requirements`) per
 * `{{ password_requirements|json_script:"password-requirements" }}`; die 2FA-Einrichtung
 * postet an die aktuelle Seite (`action=setup_2fa`).
 *
 * Markup: `templates/work/profile/security.html` und `work/profile/partials/_security_*.html`.
 */

import { defineComponent } from '../js/alpine/component'
import { showToast } from '../js/alpine/toast'
import { csrfToken } from '../js/csrf'
import { readJsonScript } from '../js/json-script'

const REQUIREMENTS_ID = 'password-requirements'
const STRENGTH_LABELS = ['Sehr schwach', 'Schwach', 'Mittel', 'Stark', 'Sehr stark']

export interface PasswordRequirements {
  min_length: number
  require_uppercase: boolean
  require_lowercase: boolean
  require_digit: boolean
  require_special: boolean
}

export interface PasswordStrength {
  score?: number
  issues?: string[]
  strength_label?: string
}

export interface TwoFactorSetupData {
  qr_code: string | null
  secret: string | null
  backup_codes: string[]
}

const DEFAULT_REQUIREMENTS: PasswordRequirements = {
  min_length: 8,
  require_uppercase: false,
  require_lowercase: false,
  require_digit: false,
  require_special: false,
}

/** Bewertet ein Passwort (0–4) und sammelt fehlende Anforderungen. */
export function evaluatePassword(password: string, requirements: PasswordRequirements): PasswordStrength {
  if (password.length < 1) return {}
  let score = 0
  const issues: string[] = []
  if (password.length < requirements.min_length) {
    issues.push(`Mindestens ${requirements.min_length} Zeichen erforderlich`)
  } else {
    score += 1
  }
  if (password.length >= 12) score += 1
  if (/[A-Z]/.test(password)) score += 0.5
  else if (requirements.require_uppercase) issues.push('Mindestens ein Großbuchstabe erforderlich')
  if (/[a-z]/.test(password)) score += 0.5
  else if (requirements.require_lowercase) issues.push('Mindestens ein Kleinbuchstabe erforderlich')
  if (/[0-9]/.test(password)) score += 0.5
  else if (requirements.require_digit) issues.push('Mindestens eine Zahl erforderlich')
  if (/[^A-Za-z0-9]/.test(password)) score += 0.5
  const finalScore = Math.min(Math.floor(score), 4)
  return { score: finalScore, issues, strength_label: STRENGTH_LABELS[finalScore] }
}

/** Alpine-Komponente: `x-data="securitySettings"` */
export const securitySettings = defineComponent(() => {
  const requirements = readJsonScript<PasswordRequirements>(REQUIREMENTS_ID) ?? DEFAULT_REQUIREMENTS

  return {
    newPassword: '',
    passwordStrength: {} as PasswordStrength,
    showSetupModal: false,
    showBackupCodesModal: false,
    showDisable2faModal: false,
    setupStep: 1,
    setupData: { qr_code: null, secret: null, backup_codes: [] } as TwoFactorSetupData,

    checkPasswordStrength(): void {
      this.passwordStrength = evaluatePassword(this.newPassword, requirements)
    },

    async setup2fa(): Promise<void> {
      this.setupStep = 1
      this.showSetupModal = true
      try {
        const res = await fetch('', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrfToken(),
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: 'action=setup_2fa',
        })
        if (res.ok) this.setupData = (await res.json()) as TwoFactorSetupData
      } catch (err) {
        console.error('2FA setup error:', err)
        showToast('Fehler beim Einrichten von 2FA', 'error')
      }
    },
  }
})
