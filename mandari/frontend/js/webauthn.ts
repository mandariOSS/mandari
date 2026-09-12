/**
 * Sicherheitsschlüssel und Passkeys (WebAuthn): Registrierung im Konto und Anmeldung im zweiten Schritt.
 *
 * Markup: `accounts/security_keys.html` (`data-webauthn-register`) und `accounts/login_2fa.html`
 * (`data-webauthn-login`) mit `data-options-url`, `data-verify-url` und `data-csrf`. Der Server liefert die
 * Optionen als JSON mit base64url-kodierten Binärfeldern (py_webauthn `options_to_json`) und prüft die
 * signierte Antwort (apps/accounts/webauthn_service.py).
 */

import { csrfToken } from './csrf'

type JsonObject = Record<string, unknown>

interface DescriptorJson {
  id: string
  transports?: string[]
}

export function base64urlToBuffer(value: string): ArrayBuffer {
  const base64 = value
    .replace(/-/g, '+')
    .replace(/_/g, '/')
    .padEnd(Math.ceil(value.length / 4) * 4, '=')
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

export function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function toDescriptors(list: unknown): PublicKeyCredentialDescriptor[] {
  if (!Array.isArray(list)) return []
  return (list as DescriptorJson[]).map((item) => ({
    id: base64urlToBuffer(item.id),
    type: 'public-key',
    transports: item.transports as AuthenticatorTransport[] | undefined,
  }))
}

function creationOptions(json: JsonObject): PublicKeyCredentialCreationOptions {
  const user = json.user as { id: string; name: string; displayName: string }
  return {
    ...(json as unknown as PublicKeyCredentialCreationOptions),
    challenge: base64urlToBuffer(json.challenge as string),
    user: { ...user, id: base64urlToBuffer(user.id) },
    excludeCredentials: toDescriptors(json.excludeCredentials),
  }
}

function requestOptions(json: JsonObject): PublicKeyCredentialRequestOptions {
  return {
    ...(json as unknown as PublicKeyCredentialRequestOptions),
    challenge: base64urlToBuffer(json.challenge as string),
    allowCredentials: toDescriptors(json.allowCredentials),
  }
}

function serializeCredential(credential: PublicKeyCredential): JsonObject {
  const attachment = (credential as PublicKeyCredential & { authenticatorAttachment?: string | null })
    .authenticatorAttachment
  const result: JsonObject = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: attachment ?? undefined,
  }
  const response = credential.response
  if ('attestationObject' in response) {
    const attestation = response as AuthenticatorAttestationResponse & { getTransports?: () => string[] }
    result.response = {
      clientDataJSON: bufferToBase64url(attestation.clientDataJSON),
      attestationObject: bufferToBase64url(attestation.attestationObject),
      transports: typeof attestation.getTransports === 'function' ? attestation.getTransports() : [],
    }
  } else {
    const assertion = response as AuthenticatorAssertionResponse
    result.response = {
      clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
      authenticatorData: bufferToBase64url(assertion.authenticatorData),
      signature: bufferToBase64url(assertion.signature),
      userHandle: assertion.userHandle ? bufferToBase64url(assertion.userHandle) : undefined,
    }
  }
  return result
}

async function postJson(url: string, csrf: string, body: JsonObject = {}): Promise<JsonObject> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrf,
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: JSON.stringify(body),
  })
  const data = (await response.json().catch(() => ({}))) as JsonObject
  if (!response.ok) {
    throw new Error(typeof data.error === 'string' ? data.error : 'Die Anfrage ist fehlgeschlagen.')
  }
  return data
}

export function describeError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') return 'Vorgang abgebrochen oder Zeitlimit überschritten.'
    if (error.name === 'InvalidStateError') return 'Dieser Schlüssel ist bereits registriert.'
    return 'Der Sicherheitsschlüssel hat die Anfrage abgelehnt.'
  }
  return error instanceof Error ? error.message : 'Unbekannter Fehler.'
}

async function register(root: HTMLElement): Promise<void> {
  const csrf = root.dataset.csrf || csrfToken()
  const options = await postJson(root.dataset.optionsUrl ?? '', csrf)
  const credential = (await navigator.credentials.create({
    publicKey: creationOptions(options),
  })) as PublicKeyCredential | null
  if (!credential) throw new Error('Es wurde kein Schlüssel erstellt.')
  const name = root.querySelector<HTMLInputElement>('[data-webauthn-name]')?.value ?? ''
  await postJson(root.dataset.verifyUrl ?? '', csrf, { credential: serializeCredential(credential), name })
  window.location.reload()
}

async function login(root: HTMLElement): Promise<void> {
  const csrf = root.dataset.csrf || csrfToken()
  const options = await postJson(root.dataset.optionsUrl ?? '', csrf)
  const credential = (await navigator.credentials.get({
    publicKey: requestOptions(options),
  })) as PublicKeyCredential | null
  if (!credential) throw new Error('Es wurde keine Signatur erstellt.')
  const result = await postJson(root.dataset.verifyUrl ?? '', csrf, { credential: serializeCredential(credential) })
  window.location.assign(typeof result.redirect === 'string' ? result.redirect : '/')
}

function setup(root: HTMLElement, run: (root: HTMLElement) => Promise<void>): void {
  const button = root.querySelector<HTMLButtonElement>('[data-webauthn-start]')
  const status = root.querySelector<HTMLElement>('[data-webauthn-status]')
  if (!button) return
  if (!window.PublicKeyCredential) {
    button.disabled = true
    if (status) status.textContent = 'Dieser Browser unterstützt keine Sicherheitsschlüssel.'
    return
  }
  button.addEventListener('click', async () => {
    button.disabled = true
    if (status) status.textContent = 'Bitte den Sicherheitsschlüssel berühren oder das Gerät entsperren …'
    try {
      await run(root)
    } catch (error) {
      if (status) status.textContent = describeError(error)
      button.disabled = false
    }
  })
}

document.querySelectorAll<HTMLElement>('[data-webauthn-register]').forEach((root) => setup(root, register))
document.querySelectorAll<HTMLElement>('[data-webauthn-login]').forEach((root) => setup(root, login))
