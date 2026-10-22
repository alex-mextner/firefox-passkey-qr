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

The deb Firefox is told, before its first start, to open the profile copied from the snap (its own
`[Install4F96D1932A9F858E]` section: that hash is Firefox's name for `/usr/lib/firefox`, the same on every
machine); if it already started and made an empty profile, that profile is kept and the copied one becomes the
default. If you ran an older version of this installer first, point Firefox at your profile once in
`about:profiles` ("Set as default profile").

### Unattended use

```sh
./passkey-qr --dry-run --yes install   # the steps an unattended run performs, assuming the token is provided (exit 0, changes nothing)
./passkey-qr --yes install             # answers every step; never prompts
./passkey-qr check                     # read-only: exit 0 = everything required is in place, 3 = not
```

`--yes` answers yes to every step and **never prompts, never closes or starts your browser and never opens a
page**. What a person has to do is not guessed: that step is skipped, the other steps still run, and the run
ends listing it:

| Exit code of `install` | Meaning |
|---|---|
| 0 | everything required is in place |
| 3 | finished what it could; the list at the end says what waits for you (re-run afterwards, finished actions are skipped) |
| 1 | a step failed (the message and the log name the command) |

What waits for you under `--yes`: the Home Assistant token (it must already be in `$HA_TOKEN` or
`~/.config/ha-ble-vhci/token`; a stored token that Home Assistant rejected is only replaced by a fresh
`$HA_TOKEN`; the token is never printed), a Firefox that is still running (it blocks the profile copy and
the profile switch), and no Bluetooth adapter and no Home Assistant at all (plug in a USB BLE dongle).
The Firefox snap is **kept** unless you also pass `--remove-snap`. Firefox is not restarted: the policy is
in place, so it installs the extension at its next start (`check` does not wait for that).
`HA_URL` selects a Home Assistant by hand instead of the LAN/Tailscale lookup.

`check` also runs on a machine that was never touched (everything unmet, exit 3), changes nothing on disk or
in any service (the app-id probe only registers a short-lived D-Bus connection with the portal, and it never
starts the portal) and never writes a log; only `install` writes `~/.local/state/passkey-qr/<timestamp>-install-<pid>.log`.
[ubuntu-tweaks](https://github.com/alex-mextner/ubuntu-tweaks) drives exactly this interface
(`--dry-run --yes install` as the preview, `--yes [--remove-snap] install` to apply, `check` for drift)
from `ubuntu-tweak init`.
`PASSKEY_QR_BLUETOOTH_DIR` overrides `/sys/class/bluetooth` (a test seam: `python3 -m unittest discover -s tests`).

`credentialsd` and the patched portal are cloned into `~/dev` (override with
`PASSKEY_QR_SRC=/some/dir`); an existing checkout is reused and switched to the
pinned release tag.

For step 3 you need a **long-lived access token of an admin Home Assistant
user** (Profile -> Security); the installer asks for it and stores it in
`~/.config/ha-ble-vhci/token` (mode 600).

After the last step the installer reloads the user D-Bus/systemd services, restarts
the portal, restarts Firefox (session restore brings the tabs back), waits until
the extension is active with access to all sites, and offers to open
https://webauthn.io (none of the Firefox restart or the test page happens under `--yes`). The policy-installed extension gets `<all_urls>` granted
automatically (checked in an isolated Firefox 158 profile).

Everything shown on screen during `install`, including all command output, is also written to
`~/.local/state/passkey-qr/<timestamp>-install.log` (`latest.log` links to the
last run) together with a diagnostic snapshot at the end or on failure.
Home Assistant tokens are never logged.

## Not supported

* **Touch ID on the Apple Magic Keyboard.** The keyboard is only a sensor:
  templates and matching live in a Mac's Secure Enclave and pairing is rooted in
  Apple's CA ([Apple Platform Security](https://support.apple.com/guide/security/magic-keyboard-with-touch-id-secf60513daa/web)).
  It cannot act as an authenticator on Linux.
* Connecting to BLE devices through the HA bridge (scan-only; enough for the
  passkey QR flow).

## Status / honesty

Last verified on 2026-10-22 (Ubuntu 26.04, Firefox 158.0.1):

- hardware: a PC without Bluetooth, an ESPHome Bluetooth proxy on an ESP32, Home Assistant over Tailscale;
- software: `ha-ble-vhci` as a virtual `hci0`, `credentialsd` with the patched portal;
- end to end: a real passkey login from an iPhone through the QR code;
- unattended (`--yes`): the package, build and install steps on a pristine Ubuntu 26.04.1 root in an
  unprivileged `bwrap`, up to the first step that needs a systemd user session;
- in unit tests: the advertisement path against a mock Home Assistant and Bumble's controller, the `--dry-run`
  plan, the profile and portal edits (`python3 -m unittest discover -s tests`).

Not verified yet:

- a run through [ubuntu-tweaks](https://github.com/alex-mextner/ubuntu-tweaks) (`ubuntu-tweak init`) on a
  brand-new machine, [#11](https://github.com/alex-mextner/firefox-passkey-qr/issues/11);
- an Android phone and a PC with a physical Bluetooth adapter (there the `ha-ble-vhci` step is skipped because a
  local adapter is found), [#12](https://github.com/alex-mextner/firefox-passkey-qr/issues/12).

Report breakage as issues.

## Troubleshooting

* `passkey-qr status` shows what is missing; the log is `~/.local/state/passkey-qr/latest.log`.
* Bridge: `journalctl --user -u ha-ble-vhci`; `bluetoothctl list`. A logout and login is needed only when the
  installer reports that your user was just added to `plugdev` (the udev rule itself uses the `uaccess` tag).
* QR dialog hangs / "QR stopped working" with the bridge running: the ESP32
  Bluetooth proxy is probably powered off or off Wi-Fi. The bridge stays up and
  Home Assistant is fine, but the phone's advert never arrives. `passkey-qr status`
  shows a live `[ ] a Bluetooth proxy is online in Home Assistant` line (not a
  `check` failure: an offline proxy is transient); power the ESP32 back on.
  `ha-ble-vhci --scanners` and `journalctl --user -u ha-ble-vhci` say the same.
* The installer reloads user systemd and D-Bus itself, so no logout is needed after installing.
* Extension: restart Firefox once. The installer's policy grants the extension access to all sites, and
  `passkey-qr status` checks that in the profile.
* Services: `journalctl --user -u xdg-desktop-portal -u xyz.iinuwa.credentialsd.Credentials`.
* `passkey-qr` registers the helper's app id with the portal; the portal only accepts ids that have a desktop
  file, so `org.mozilla.firefox.desktop` must exist. The installer creates it unless an
  `org.mozilla.firefox.desktop` entry already exists (for example the Flatpak Firefox).
* The portal only exports the Credential interface when a `[preferred]` entry names `credentialsd` for it. The
  installer writes this to `~/.config/xdg-desktop-portal/portals.conf`:

  ```ini
  [preferred]
  org.freedesktop.impl.portal.experimental.Credential=credentialsd
  ```
