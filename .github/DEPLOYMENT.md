# GitHub Actions deployment

`CI` runs Ruff and tests on Python 3.12 and 3.14 for every push and pull request.

`Deploy production` is intentionally manual. It tests the release, creates a database backup and
a code backup, stops only `marsrut-bot.service`, installs the update, starts the service and checks
`http://127.0.0.1:8081/health`. If an installation or health check fails, the previous code is
restored and the service is started again.

Configure these repository Actions secrets before the first deployment:

- `DEPLOY_HOST`: production server hostname or IP;
- `DEPLOY_PORT`: SSH port, usually `22`;
- `DEPLOY_USER`: a dedicated deployment user;
- `DEPLOY_SSH_KEY`: its private Ed25519 key;
- `DEPLOY_KNOWN_HOSTS`: the verified `known_hosts` line for the server.

The deployment user connects without a password and may run only the root-owned
`/usr/local/sbin/marsrut-bot-deploy` helper through passwordless `sudo`. The helper has hard-coded
application, service and health-check targets; validates the release archive; performs application
file operations as `marsrut-bot`; and preserves `.env`, `.venv`, `data`, `logs`, and `backups`.

Install or update the helper once from a trusted root session on the production server:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/Studio313-krd/marsrut_bot/main/.github/scripts/marsrut-bot-deploy \
  -o /tmp/marsrut-bot-deploy

install -o root -g root -m 0755 \
  /tmp/marsrut-bot-deploy \
  /usr/local/sbin/marsrut-bot-deploy

printf '%s\n' \
  'marsrut-deploy ALL=(root) NOPASSWD: /usr/local/sbin/marsrut-bot-deploy' \
  > /etc/sudoers.d/marsrut-bot-deploy

chmod 0440 /etc/sudoers.d/marsrut-bot-deploy
visudo -cf /etc/sudoers.d/marsrut-bot-deploy
sudo -u marsrut-deploy sudo -n /usr/local/sbin/marsrut-bot-deploy --check
rm -f /tmp/marsrut-bot-deploy
```

Do not add `marsrut-deploy` to the `sudo` group and do not grant it direct access to generic
`tar`, `chown`, `systemctl`, shells, editors, or commands run as `marsrut-bot`. Do not store `.env`,
bot tokens or server passwords in GitHub.

Run a deployment from GitHub: **Actions → Deploy production → Run workflow**.
