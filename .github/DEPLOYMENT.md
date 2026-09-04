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

The deployment user must be able to connect without a password and use passwordless `sudo` only
for the commands required by the workflow (`tar`, `chown`, `systemctl`, and running commands as
`marsrut-bot`). Do not store `.env`, bot tokens or server passwords in GitHub.

Run a deployment from GitHub: **Actions → Deploy production → Run workflow**.
