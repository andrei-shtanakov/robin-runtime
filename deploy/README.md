# Deploying Robin to a VPS

Target layout (always-on VPS, ROBIN-SPEC slot 3):

```
/srv/robin/
├── robin-runtime/     ← this repo (code + .venv)
├── mirrors/           ← read-only git clones: prograph-vault + ecosystem repos
│   ├── prograph-vault/
│   ├── arbiter/ maestro/ atp-platform/ …
├── var/               ← Robin's own store (interactions.jsonl, digests/, chats/)
└── robin.env          ← ALL secrets (slot 17), chmod 600
```

## Bring-up

```bash
# on the VPS, as root:
git clone <your-remote>/robin-runtime.git /srv/robin/robin-runtime
cd /srv/robin/robin-runtime
GIT_BASE=git@github.com:your-org sudo -E deploy/setup.sh   # packages, uv, mirrors,
                                                           # units, robin user
sudo vi /srv/robin/robin.env                               # fill every var (see env.example)
sudo timedatectl set-timezone Europe/…                     # MUST match ROBIN_TZ (digest cron)
sudo systemctl enable --now robin-telegram robin-web
```

Private repos: give the `robin` user a read-only deploy key (`sudo -u robin ssh-keygen`,
add the public key as a **read-only** deploy key on each repo).

## nginx (TLS in front of the web chat)

```nginx
server {
    listen 443 ssl;
    server_name robin.example.com;
    # ssl_certificate … (certbot --nginx robin.example.com)
    client_max_body_size 25m;          # voice uploads
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 120s;       # grounded answers can take a while
    }
}
```

## Telegram side (BotFather / clients)

1. `@BotFather` → `/newbot` → token into `robin.env`.
2. For **group** mentions: add the bot to the group; either disable privacy mode
   (`/setprivacy` → Disable) or rely on explicit @mentions (privacy mode delivers those).
3. For the **digest channel**: add the bot as a channel **admin** (post permission),
   put `@channelname` (or the `-100…` id) in `ROBIN_TELEGRAM_CHANNEL`.
4. User ids for `ROBIN_ALLOWED_DM`: each teammate DMs `@userinfobot` (or the maintainer
   reads `var/interactions.jsonl` after a refused attempt).
5. `ROBIN_MAINTAINER_CHAT`: the maintainer DMs the bot once, then
   `curl https://api.telegram.org/bot<token>/getUpdates` → `chat.id`.

## Manual smoke checklist (what offline tests can't cover)

- [ ] `sudo -u robin bash -lc 'cd /srv/robin/robin-runtime && set -a && . /srv/robin/robin.env && .venv/bin/python -m robin.agent "Which repo owns the agents-catalog SSOT?"'`
      → cited answer, cost printed (M0; proves the ANTHROPIC key + mirrors).
- [ ] DM the bot a text question → cited answer (M1). Ask a colleague, not the builder.
- [ ] DM «что изменилось за неделю?» → answer grounded in `repo@sha` cites.
- [ ] Send a voice note → `🎙 transcript` + text answer + voice reply.
- [ ] `systemctl start robin-digest-daily.service` → post appears in the channel,
      file appears in `/srv/robin/var/digests/` (M2).
- [ ] Stop the timers for a day → liveness alert lands in the maintainer DM (§7).
- [ ] Web: open `https://robin.example.com`, paste `ROBIN_WEB_TOKEN`, ask by text and mic.
- [ ] `/cost` in Telegram shows today's spend after the above.
- [ ] §6.7 spot-check: ask a question whose answer quotes `<` (e.g. generics in Rust code)
      and confirm the reply renders instead of erroring.

## Care and feeding

- Logs: `journalctl -u robin-telegram -u robin-web -u robin-digest-* --since today`.
- `var/` grows without bound (append-only by design). Archive `interactions.jsonl`
  yearly; never edit it in place. SQLite migration is the planned M3 upgrade.
- Updating code — **as `robin`, never as root**. The checkout, the `.venv` and `var/`
  are owned by the service user (`setup.sh` clones with `sudo -u robin`, the units run
  `User=robin`); a `sudo git pull` leaves root-owned objects in `.git` and a root-owned
  `.venv`, which the services then fail to write. `-H` matters: without it `HOME` stays
  the caller's and both uv's cache and robin's deploy key go missing.

  ```bash
  sudo -H -u robin git -C /srv/robin/robin-runtime pull --ff-only
  cd /srv/robin/robin-runtime && sudo -H -u robin uv sync
  sudo systemctl restart robin-telegram robin-web
  ```

  Only the long-running units need the restart; the digest and liveness timers are
  oneshot and pick up new code and a changed `robin.env` on their next fire.
- Changing settings: edit `/srv/robin/robin.env` (the `EnvironmentFile` of every unit),
  not anything inside the checkout. `deploy/env.example` only documents the knobs.
