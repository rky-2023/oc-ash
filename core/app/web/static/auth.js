// Minimal WebAuthn client helpers for openclaw registration + login.
//
// Uses the new WebAuthn-Level-3 JSON shapes (PublicKeyCredential.toJSON()
// + parseCreationOptionsFromJSON / parseRequestOptionsFromJSON). Supported
// in Chrome 121+, Edge 121+, Safari 17.4+, Firefox 122+ (or with a small
// polyfill if older).

(function () {
  'use strict';

  const SESSION_KEY = 'oc.session';
  const logEl = () => document.getElementById('log');

  function log(msg) {
    const el = logEl();
    if (el) {
      el.textContent += (el.textContent ? '\n' : '') + msg;
      el.scrollTop = el.scrollHeight;
    } else {
      console.log(msg);
    }
  }

  function saveSession(payload) {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      access_token: payload.access_token,
      expires_at: Date.now() + (payload.expires_in * 1000),
      user_id: payload.user_id,
      username: payload.username,
    }));
  }

  function getSession() {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    try {
      const s = JSON.parse(raw);
      if (s.expires_at && s.expires_at < Date.now()) return null;
      return s;
    } catch { return null; }
  }

  async function postJSON(path, body) {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await resp.text();
    let json;
    try { json = JSON.parse(text); } catch { json = { detail: text }; }
    if (!resp.ok) {
      throw new Error(json.detail || `HTTP ${resp.status}`);
    }
    return json;
  }

  // --- Registration ---------------------------------------------------------

  async function register({ username, display_name, nickname }) {
    log(`POST /auth/register/begin (username=${username})`);
    const begin = await postJSON('/auth/register/begin', { username, display_name });

    log(`browser navigator.credentials.create(...)`);
    if (!window.PublicKeyCredential || !PublicKeyCredential.parseCreationOptionsFromJSON) {
      throw new Error(
        'This browser is missing WebAuthn L3 JSON support. ' +
        'Use Chrome 121+, Safari 17.4+, or Firefox 122+.'
      );
    }
    const options = PublicKeyCredential.parseCreationOptionsFromJSON(begin.options);
    const credential = await navigator.credentials.create({ publicKey: options });
    if (!credential) throw new Error('No credential returned by the authenticator');

    log(`POST /auth/register/complete`);
    const complete = await postJSON('/auth/register/complete', {
      challenge_id: begin.challenge_id,
      credential: credential.toJSON(),
      nickname,
    });

    saveSession({
      access_token: complete.access_token,
      expires_in: complete.expires_in,
      user_id: complete.user_id,
      username,
    });
    return complete;
  }

  // --- Login ---------------------------------------------------------------

  async function login({ username }) {
    log(`POST /auth/login/begin (username=${username || '<resident-key>'})`);
    const begin = await postJSON('/auth/login/begin', { username });

    log(`browser navigator.credentials.get(...)`);
    if (!window.PublicKeyCredential || !PublicKeyCredential.parseRequestOptionsFromJSON) {
      throw new Error(
        'This browser is missing WebAuthn L3 JSON support. ' +
        'Use Chrome 121+, Safari 17.4+, or Firefox 122+.'
      );
    }
    const options = PublicKeyCredential.parseRequestOptionsFromJSON(begin.options);
    const credential = await navigator.credentials.get({ publicKey: options });
    if (!credential) throw new Error('No credential returned by the authenticator');

    log(`POST /auth/login/complete`);
    const complete = await postJSON('/auth/login/complete', {
      challenge_id: begin.challenge_id,
      credential: credential.toJSON(),
    });

    saveSession(complete);
    return complete;
  }

  // --- Authenticated calls -------------------------------------------------

  async function authedGet(path) {
    const s = getSession();
    if (!s) throw new Error('No session. Log in first.');
    const resp = await fetch(path, {
      headers: { Authorization: `Bearer ${s.access_token}` },
    });
    const text = await resp.text();
    let json;
    try { json = JSON.parse(text); } catch { json = { detail: text }; }
    if (!resp.ok) throw new Error(json.detail || `HTTP ${resp.status}`);
    return json;
  }

  async function me() {
    return authedGet('/auth/me');
  }

  // Expose
  window.ocAuth = { register, login, me, log, getSession };
})();
