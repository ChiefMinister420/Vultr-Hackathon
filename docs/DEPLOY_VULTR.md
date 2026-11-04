# Deploy on Vultr

Runbook for the public demo on one Vultr Cloud Compute VM (Ubuntu 24.04). The control server runs directly on the host under systemd as the `slipstream` user. Sandboxes run in Docker under gVisor (`runsc`), and Caddy terminates HTTPS in front of it. **The Docker socket is never mounted into any container.** Check current pricing in the Vultr console before creating resources.

**Submission rules:** `/etc/slipstream.env` must contain `REQUIRE_VULTR=true`. **Never copy a Groq key (`GROQ_API_KEY`) to the VM**; Groq is for local development only.

1. **Create the VM and SSH key.** In the Vultr console, add your SSH public key (Account → SSH Keys), then deploy a Cloud Compute instance with Ubuntu 24.04 x64 and that key. 2 GB RAM or more is recommended. Note the public IPv4 address (called `<VM-IP>` below), then `ssh root@<VM-IP>`.

2. **Create the Serverless Inference subscription and key.** In the console, open Serverless Inference, add a subscription and copy its API key. Keep the key only in your password manager and in `/etc/slipstream.env` (step 4).

3. **Run the setup script.**
   ```sh
   apt-get update && apt-get install -y git
   git clone <repository-url> /opt/slipstream
   cd /opt/slipstream
   sudo bash deploy/setup.sh
   ```
   This installs Docker, python3-venv and gVisor (`runsc` from gVisor's apt repository, registered with `runsc install`, followed by a Docker restart). It also creates the `slipstream` user in the `docker` group, creates `/opt/slipstream/.venv`, installs the project, builds `slipstream-runner:local` and `slipstream-proxy:local`, and checks gVisor with `docker run --rm --runtime=runsc alpine dmesg`. It is safe to re-run, and it never touches `/etc/slipstream.env`.

4. **Create `/etc/slipstream.env`.**
   ```sh
   sudo install -m 600 -o root -g root deploy/slipstream.env.example /etc/slipstream.env
   sudo nano /etc/slipstream.env   # set VULTR_INFERENCE_API_KEY and VULTR_INFERENCE_MODEL
   ```
   Keep `REQUIRE_VULTR=true`, `LLM_PROVIDER=vultr`, `SANDBOX_RUNTIME=runsc`, `HOST=127.0.0.1` and `TRUST_PROXY=true`. Leave out any `GROQ_API_KEY` line. systemd reads this file as root, so mode 600 is enough.

5. **Enable the service.**
   ```sh
   sudo cp deploy/slipstream.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now slipstream
   curl -s http://127.0.0.1:8080/api/health
   ```

6. **Set up Caddy.**
   ```sh
   sudo apt-get install -y caddy
   sed "s/<VM-IP>/$(curl -4s https://ifconfig.me)/" deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
   Check that the address in `/etc/caddy/Caddyfile` is your VM's IP. The reverse-proxy port must match `PORT`.

7. **Firewall: allow OpenSSH, 80 and 443 only.**
   ```sh
   sudo ufw default deny incoming
   sudo ufw allow OpenSSH
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw enable
   ```
   Port 8080 stays closed; the server listens on loopback anyway. No container publishes a port.

8. **Verify.**
   ```sh
   curl -s https://<VM-IP>.sslip.io/api/health | python3 -m json.tool
   ```
   Expect `"docker_ready": true`, `"runtime": "runsc"`, `"require_vultr": true`, `"provider": "vultr"` and your model. Then open `https://<VM-IP>.sslip.io` and run one red team self-test. Click **Escape attempts** and approve it. The scorecard should show exfil, direct socket and outside writes BLOCKED, the secrets count 0, and example.com OK. The network section should list example.org as blocked with "host not in allowlist".

9. **When something fails.**
   - Service logs: `journalctl -u slipstream -e` (live: `-f`). `LLM configuration error: REQUIRE_VULTR=true but VULTR_INFERENCE_API_KEY is not set` means the env file is incomplete.
   - Caddy and certificates: `journalctl -u caddy -e`. Certificate issuance needs ports 80 and 443 open and the right IP in the Caddyfile.
   - Runner problems: `docker info | grep -i runtimes` should list `runsc`. `docker images | grep slipstream` should show both images; if not, re-run `deploy/setup.sh`.
   - Leftover sandboxes: `docker ps -a` and `docker network ls`. The server removes stale `slipstream=1` resources at startup (`sudo systemctl restart slipstream`).
   - 429 responses are the abuse limits: `MAX_CONCURRENT_RUNS`, `RATE_LIMIT_PER_MIN`. Plans labelled "daily model call cap reached" mean `MAX_LLM_CALLS_PER_DAY` was hit; the count resets at 00:00 UTC.

After the event, rotate the Vultr inference key and destroy the VM.

## docker-compose.yml

`docker-compose.yml` is for local experiments only and is **not** the deployment path. It mounts the Docker socket into the control container, which gives that container root on the host. Under Compose, the runner's temporary input and output folders are created inside the control container but bind-mounted by the host's Docker daemon. The file therefore shares `/tmp/slipstream-compose` at the same path on the host and in the container (via `TMPDIR`). Without that, every run would see empty folders. Compose does not build `slipstream-proxy:local`, so build it yourself before trying runs with network domains.
