---
name: vod-manager-ssh-access
enabled: true
event: bash
pattern: (ssh|plink).*(192\.168\.1\.210|VOD-MANAGER-210|vod-manager)
---

vod-manager's Docker host is reachable **directly** over SSH -- do not route through
prox3 (192.168.1.244) + `pct exec 510`, and do not ask the user for access before
checking `~/.ssh/config` / `b:\Claude_Apps\.ssh.env` first.

**Confirmed working (2026-09-08):**
```
ssh VOD-MANAGER-210 "docker logs vod-manager --tail 50"
```
Alias is defined in `C:\Users\knmfl\.ssh\config`:
```
Host VOD-MANAGER-210
  HostName 192.168.1.210
  User root
  IdentityFile C:\Users\knmfl\.ssh\kid_rsa
```
Also recorded in `b:\Claude_Apps\.ssh.env` under "VOD-MANAGER SERVER". Key auth works,
no password needed.

Host is LXC 510 on prox3 -- but reachable at its own IP (192.168.1.210) directly, it does
NOT need `pct exec` from the prox3 host. The container name inside is `vod-manager`.

If this host is ever unreachable at .210 (IP changed), check prox3
(`ssh 244 "pct list"` or similar) for the current LXC 510 IP before assuming no access
exists -- the user has explicitly stated access exists and should not have to repeat that.
