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

Standard Apple ID two-factor (a 6-digit code to your trusted iPhone/Mac) works
fine in the Linux Chrome — enter Apple ID + password, then the code.

**But USB security keys cannot pass through Docker on a Mac** (Docker Desktop's
Linux VM doesn't expose host USB). So if your Apple ID *requires* a hardware key
(YubiKey / passkey), interactive sign-in here is impossible — the key is invisible
to the container. Use **token injection** instead:

### Token injection (recommended when a hardware key is required)

Sign in where the key works (macOS: `applemusic-mcp login --safari`), grab the
`media-user-token`, and pass it to the container via `APPLEMUSIC_USER_TOKEN`:

```bash
# On the Mac that's signed in, print the token (keep it private):
TOK=$(python -c "from applemusic_mcp.auth import get_user_token; print(get_user_token())")

# Run the container with it injected — no interactive login needed:
docker run --rm -it -p 6080:6080 --shm-size=1g \
  -e APPLEMUSIC_USER_TOKEN="$TOK" applemusic-mcp-linux-tryout
```

The API works immediately and the Chrome web player injects the token as its
session cookie, so playback/queue work with no in-container sign-in. (This is the
same env var any headless/CI deployment can use.) Alternatively, run a full Linux
VM with USB passthrough (UTM/Parallels) and pass the key through to it.

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
