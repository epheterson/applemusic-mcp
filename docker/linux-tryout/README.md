# Linux try-out container

A real Debian + Google Chrome desktop, reachable in your browser via noVNC, for
exercising the **off-mac path** (Windows/Linux: data via the Apple Music API,
playback via the Chrome web player). No Music.app, no Safari — exactly what a
Windows/Linux user gets.

This is a **dev/test tool**, not part of the shipped package.

## Run it

```bash
./docker/linux-tryout/run.sh
```

First build downloads Google Chrome (a few minutes). When you see `noVNC ready`,
open **http://localhost:6080/vnc.html** in any browser → **Connect** (no password).
You'll get a Linux desktop with a terminal already open and instructions.

Running it on the iMac/mini but connecting from your MacBook? Use the host's LAN
address instead of localhost: `http://<that-mac>.local:6080/vnc.html`.

## Sign in

In the container's terminal:

```bash
applemusic-mcp login        # opens Chrome → sign in with Apple ID + 2FA code
applemusic-mcp status       # User token present, API ok
python ~/smoke.py           # search + play + queue, end to end
```

Your sign-in persists in the docker volume `applemusic-linux-data` across restarts.

## Auth: passkeys / YubiKeys (read this)

**You almost certainly don't need them.** Standard Apple ID two-factor uses a
6-digit code pushed to your trusted iPhone/Mac — that works fine in the Linux
Chrome. Enter Apple ID + password, then the code.

**USB security keys cannot pass through Docker on a Mac** (Docker Desktop's Linux
VM doesn't expose host USB). So a YubiKey plugged into the Mac is invisible here.
If your Apple ID is configured to *require* a hardware security key, you have two
options:

1. **Inject a token harvested on your Mac (recommended).** Sign in on macOS where
   your passkey/YubiKey works (`applemusic-mcp login --safari`), then hand that
   token to the container — the Chrome engine injects it and is signed in without
   an interactive login. (The server already does this via `_ensure_session_cookie`;
   wiring a token env into this container is a small follow-up if you need it.)
2. **Run a full Linux VM with USB passthrough** (e.g. UTM/Parallels) instead of
   Docker, and pass the YubiKey through to it.

## Notes

- **Audio is not routed out of the container, on purpose.** We verify playback
  *starts* and the queue works — not that you can hear it. (Keeps the host quiet.)
- **Intel/amd64 only** for real DRM: Google Chrome's Widevine CDM is amd64-only.
  The iMac is Intel, so it runs natively. On Apple Silicon, Chrome runs under
  emulation (slow) or falls back to preview-only.
- The container sets `APPLEMUSIC_BROWSER_NO_SANDBOX=1` — Chrome can't use its
  sandbox as root in a container. This flag is for containers/CI only; the normal
  desktop install keeps the sandbox on.
- Logs inside the container: `/tmp/xvfb.log`, `/tmp/x11vnc.log`, `/tmp/novnc.log`.
