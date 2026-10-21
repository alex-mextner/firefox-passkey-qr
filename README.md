# firefox-passkey-qr

Make **Firefox on Ubuntu** offer **"use your phone (QR code)"** for passkeys
(WebAuthn hybrid / caBLE), which stock Firefox on Linux does not implement
([bug 1543674](https://bugzilla.mozilla.org/show_bug.cgi?id=1543674)).

`passkey-qr install` walks through the setup step by step and **asks before
every step that changes your system**. Each action has a check against the real
system state (files, packages, services, the Firefox profile), so a re-run skips
what is already done and `passkey-qr status` shows what is left. Missing build
dependencies (`meson` / `cargo` errors) are identified and installed with apt
automatically, after a confirmation:

| Step | What | Why |
|------|------|-----|
| 1 | apt build/runtime packages | credentialsd and the portal are built from source |
| 2 | Firefox from `packages.mozilla.org` (+ apt pin), copies your snap profile, optionally removes the snap | the Firefox snap sandbox blocks the native-messaging host the extension needs |
| 3 | **Only if the PC has no Bluetooth and Home Assistant is reachable:** installs [ha-ble-vhci](https://github.com/alex-mextner/ha-ble-vhci) | the QR flow requires the PC to *hear the phone's BLE advertisement*; ha-ble-vhci gets those from your ESPHome Bluetooth proxy through Home Assistant and presents them to BlueZ as a local adapter |
| 4 | builds [credentialsd](https://github.com/linux-credentials/credentialsd) + its patched `xdg-desktop-portal` into `/usr/local` | the credential manager that implements hybrid on Linux |
| 5 | native-messaging link + a Firefox policy (`/etc/firefox/policies/policies.json`, `normal_installed`) that installs the Mozilla-signed `credentialsd-helper` extension on next start (you can still disable it) | intercepts `navigator.credentials` in Firefox and shows the phone/USB chooser |

If the PC has a real Bluetooth adapter, step 3 does nothing. If it has neither
Bluetooth nor Home Assistant, the installer tells you to get a USB BLE dongle.

```sh
git clone https://github.com/alex-mextner/firefox-passkey-qr
cd firefox-passkey-qr
./passkey-qr --dry-run install   # prints every command, changes nothing
./passkey-qr install
./passkey-qr status
```

`credentialsd` and the patched portal are cloned into `~/dev` (override with
`PASSKEY_QR_SRC=/some/dir`); an existing checkout is reused and switched to the
pinned release tag.

For step 3 you need a **long-lived access token of an admin Home Assistant
user** (Profile -> Security); the installer asks for it and stores it in
`~/.config/ha-ble-vhci/token` (mode 600).

## Not supported

* **Touch ID on the Apple Magic Keyboard.** The keyboard is only a sensor:
  templates and matching live in a Mac's Secure Enclave and pairing is rooted in
  Apple's CA ([Apple Platform Security](https://support.apple.com/guide/security/magic-keyboard-with-touch-id-secf60513daa/web)).
  It cannot act as an authenticator on Linux.
* Connecting to BLE devices through the HA bridge (scan-only; enough for the
  passkey QR flow).

## Status / honesty

Verified in development: the bridge's advertisement path against a mock Home
Assistant and Bumble's controller (HCI events produced as expected), the
installer's detection and `--dry-run` plan. **Not** verified end to end on
real hardware: `/dev/vhci` + BlueZ with the bridge, the credentialsd/portal
build on Ubuntu 26.04, and an actual phone scan. Report breakage as issues.

## Troubleshooting

* `passkey-qr status` shows what is missing.
* Bridge: `journalctl --user -u ha-ble-vhci`; `bluetoothctl list`; after being
  added to `plugdev`, log out and in once.
* After step 4 log out and in once (D-Bus/systemd activation files).
* Extension: restart Firefox once; if it does not work, allow all sites in `about:addons` -> credentialsd-helper -> Permissions.
* Services: `journalctl --user -u xdg-desktop-portal -u xyz.iinuwa.credentialsd.Credentials`.
