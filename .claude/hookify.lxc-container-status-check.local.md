---
name: lxc-container-status-check
enabled: true
event: bash
pattern: plink.*-batch.*(root@|pw\s)
---

**CORRECTED 2026-09-08: vod-manager (LXC 510) is reachable DIRECTLY at 192.168.1.210 —
prefer `ssh VOD-MANAGER-210 "<command>"` over the prox3+pct-exec route below. See
[[vod-manager-ssh-access]] hookify rule for the confirmed-working direct path. Only fall
back to plink+pct-exec (below) if .210 becomes unreachable.**

Reaching into vod-manager's host/LXC over SSH — reuse the verified pattern instead of re-discovering it:

**Fallback via prox3 + pct exec (only if direct .210 access is down):**
```
"/c/Program Files/PuTTY/plink" -ssh -batch -i "$HOME/.ssh/kid_rsa.ppk" root@192.168.1.244 "pct exec 510 -- <command>"
```
vod-manager runs in LXC 510 on prox3 (192.168.1.244). Check `b:\Claude_Apps\.ssh.env` for `keyauth: confirmed` before falling back to password auth — never print the password into the transcript; pull it into a shell variable server-side in the same command if password auth is genuinely required.

**`-hostkey "*"` does not work** with plink — it errors "not a valid format." If a host-key prompt blocks `-batch` mode, that host hasn't been connected from this session/machine before; either accept the key once interactively outside this tool, or ask the user.

**Live container CPU/status check** (e.g. after an image pull/restart, to distinguish a real regression from a normal startup burst): sample `docker stats --no-stream <container>` 2-3 times a few seconds apart rather than trusting one snapshot — a brief post-restart spike from import/enrichment work is expected and self-resolves; a sustained non-decaying peg is the actual signal worth escalating (this is exactly how the CPU-pegging fix in beads-bzg.1/beads-bzg.2 was verified post-deploy on 2026-09-08).

Example:
```
"/c/Program Files/PuTTY/plink" -ssh -batch -i "$HOME/.ssh/kid_rsa.ppk" root@192.168.1.244 "pct exec 510 -- docker stats --no-stream vod-manager"
```
