# The lab host

A dedicated, always-on machine on the local network runs the coordination services and
the sandboxed workers; development happens on a separate laptop. This folder records how
the lab is configured, so it can be rebuilt from nothing and never becomes a
hand-configured machine whose state nobody can explain.

Current host: an Apple-silicon Mac mini (M4, 16 GB) running macOS 26, reached over SSH.

## Rebuild from scratch

1. **Remote access.** Enable Remote Login (SSH) for the lab user only. Install the
   laptop's public key in `~/.ssh/authorized_keys` (mode 600, directory 700), then
   disable password logins with [`macos/050-keys-only.conf`](macos/050-keys-only.conf)
   in `/etc/ssh/sshd_config.d/` (it sorts before the system's `100-macos.conf`, and the
   first value read wins). Validate with `sudo sshd -t`; confirm with `sudo sshd -T`;
   test a new key login, and a refused key-less login, before closing the existing session.
   Screen Sharing stays enabled as a second recovery path.
2. **Power.** No sleep, restart after power failure: `sudo pmset -a sleep 0 autorestart 1`.
3. **Python.** Install [uv](https://github.com/astral-sh/uv), then `uv python install 3.12`.
   The system Python is left untouched.
4. **Code.** `git clone` this repository (never copy folders between machines), then at its
   root: `uv venv --python 3.12 .venv && uv pip install -r 01-llm-runtime/requirements.txt`.
5. **Secrets.** Generate the worker registry with
   `07-distributed-workers/run_workers.py new-registry`, copy it to
   `~/rdas/registry.json` over `scp`, mode 600, directory 700. Never in Git; never in
   `/tmp` (emptied at boot).
6. **The coordinator service.** Install
   [`macos/com.lucazanolini.rdas-coordinator.plist`](macos/com.lucazanolini.rdas-coordinator.plist)
   in `/Library/LaunchDaemons/` (a daemon, not an agent: it must start at boot with
   nobody logged in), then `sudo launchctl bootstrap system <plist>`. It runs as the lab
   user, restarts on exit (throttled to once per 5 s), and logs to `~/rdas/coordinator.log`.
   Inspect with `sudo launchctl print system/com.lucazanolini.rdas-coordinator`;
   restart with `launchctl kickstart -k`.
7. **Containers.** Install Apple's [`container`](https://github.com/apple/container) from
   the **signed** installer package of a release, after checking it with
   `pkgutil --check-signature` (Developer ID Installer: Apple Inc., notarized). Then
   `container system start` (installs a default Linux kernel on first run). Each container
   runs in its own lightweight virtual machine.

## Decisions

- **Network exposure.** Services listen on the LAN; nothing is forwarded from the router,
  so nothing is reachable from the Internet. Remote access from outside, if ever needed,
  goes through a VPN, never a forwarded port.
- **Firewall.** The macOS application firewall is off, deliberately: the set of listening
  services is small and known (SSH, Screen Sharing and its Kerberos service, the
  coordinator), every one authenticates its clients, and the host is LAN-only. Revisit if
  the host leaves the home network, stores sensitive data, or runs agent code that can
  open listeners of its own.
- **Least privilege.** The coordinator runs as the ordinary lab user, not as root;
  workers receive only their own secret.

## State: persistent and disposable

| State | Survives a crash | Survives a reboot | If lost |
|---|---|---|---|
| coordinator's in-memory board | no | no | lost (a durable ledger is Stage 8) |
| `~/rdas/` (registry, log) | yes | yes | regenerate the registry; logs are diagnostics |
| `/tmp` | yes | no | by design |
| container filesystems | n/a | n/a | disposable: recreated from the image |
| `.venv` | yes | yes | rebuilt in seconds from `requirements.txt` |
| service definition, SSH configuration | yes | yes | recreated from this folder |

## Verified drills

- A service killed with SIGKILL is restarted by the supervisor with a new process.
- After a reboot, with no one logged in, the service is running within a minute.
- A service with a missing input enters a throttled crash loop; the supervisor reports the
  exit code, the service log names the missing file; fixing it and `kickstart -k` restores
  service.
- The project environment, deleted entirely, is rebuilt and passes its tests.
- A laptop–lab network partition of about 13 s is survived by the coordinator and workers
  (see [Stage 7](../07-distributed-workers/README.md#across-two-machines-a-real-partition)).
