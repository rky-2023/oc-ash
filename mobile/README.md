# mobile/

Android-first React Native notification client.

**Stack:** React Native (latest stable), TypeScript, `react-native-firebase`, `react-native-keychain` (hardware-backed Android Keystore).

**Pairing flow:**
1. Open app → scan QR shown by openclaw web UI (you've logged in via YubiKey on a laptop).
2. App performs Play Integrity attestation.
3. Server enrolls device, exchanges Curve25519 public keys.
4. App stores private key in Android Keystore — it never leaves the secure element.

**Screens (v1):**
- Inbox (notifications)
- Detail (decrypted payload, deep links into source)
- Settings (DND, channel toggles, per-source mute)
- Pair-new-device

**Status:** scaffold (Phase 9 will populate). iOS deferred until v2.
