# Cast Manager

Keeps the office TVs (Chromecast / Google TV) showing their dashboard display links. A watchdog
checks every TV every `check_interval_s` seconds and re-casts when a TV has fallen back to its
idle screen. A local web page at http://127.0.0.1:8765 shows status and lets you manage the TVs.

## Setup

```
cd C:\Apps\castmanager
python -m pip install -r requirements.txt
copy config.example.json config.json
```

Edit `config.json` (or use the web page) and paste each TV's display link into `url`.
`config.json` is git-ignored because display links are secrets.

## Run

```
python main.py            # real TVs
python main.py --fake     # fake cast layer: no TV is contacted (for trying the page)
python main.py --fake --config C:\path\to\other.json --port 8799
```

The web page only listens on 127.0.0.1. Logs go to `logs\castmanager.log` (1 MB per file, 5 files).

## Config

| Key | Meaning |
|---|---|
| `port` | Web page port (default 8765) |
| `check_interval_s` | Seconds between watchdog passes (default 45, minimum 5) |
| `refresh_interval_s` | Re-cast a TV that is showing its page every this many seconds, so a frozen page gets a fresh start (default 10800 = 3 hours, minimum 600, `0` = off) |
| `takeover` | `idle_only`: re-cast only when the TV is idle or already on DashCast. `always`: also take over other apps |
| `tvs[]` | `name`, `ip`, `uuid`, `url`, `enabled`. An `id` is added automatically on first start |

Watchdog rules, per enabled TV:

| TV state | Action | Status |
|---|---|---|
| Showing DashCast (app 84912283) | nothing | showing |
| Idle (Backdrop or no app) | re-cast | idle, then casting |
| Another app | `idle_only`: nothing; `always`: re-cast | in use by (app) |
| Unreachable | reconnect with backoff 45 s doubling to 5 min | offline |
| UUID does not match config | never cast | wrong device |

A TV that has been showing its page for `refresh_interval_s` since its last cast is re-cast (the
app is quit and relaunched), because a Chromecast page left running for hours can stop repainting
while it still looks like it is showing. At most one watchdog re-cast per TV every 2 minutes. Each TV is also cast once at startup and
whenever its url changes. A TV with no url, no UUID, or `enabled: false` is never cast to.
**Stop** quits the app on the TV and pauses that TV's watchdog until you press **Resume** or **Cast now**.

## Start with Windows

From an elevated PowerShell:

```
powershell -ExecutionPolicy Bypass -File .\install_task.ps1     # register "Cast Manager"
Start-ScheduledTask -TaskName "Cast Manager"                    # start it now
powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1   # remove it
```

The task runs `pythonw main.py` at system startup as SYSTEM (no one needs to be logged on and no
password is stored) and restarts every minute on failure. It is not run as the current user because
an S4U task for an Azure AD account is accepted but never launches. Check it with
`Get-ScheduledTask -TaskName 'Cast Manager'` and the page at http://127.0.0.1:8765.

## Tests

```
python -m pytest -q
```

Tests use the fake cast layer; no TV is contacted.
