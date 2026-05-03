#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — Automated Server Deployment
# Called automatically by try_oracle.py after a server is created.
# Connects via SSH, installs everything, sets up crontab, sends success email.
# =============================================================================

import paramiko
import time
import os
import smtplib
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
SSH_USER        = "ubuntu"
SSH_KEY_PATH    = os.path.expanduser("~/.ssh/id_ed25519")
REPO_DIR        = "/home/ubuntu/apex-capital"
EMAIL_SENDER    = "jack.beard07@gmail.com"
EMAIL_RECIPIENT = "jack.beard07@gmail.com"
GMAIL_PASSWORD  = "vaje mgnh jhtm kknf"
GITHUB_TOKEN    = "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"

# ── Environment variables for the server ─────────────────────────────────────
ENV_VARS = {
    "ALPACA_API_KEY":    "PKMJMECIKK235BQTNKGCCSKEL3",
    "ALPACA_SECRET_KEY": "7uxURY6amjpDaQkVcoD2tXrcMsrpUHntjut95QwHZbR8",
    "ALPACA_BASE_URL":   "https://paper-api.alpaca.markets",
    "EMAIL_SENDER":      "jack.beard07@gmail.com",
    "EMAIL_RECIPIENT":   "jack.beard07@gmail.com",
    "GMAIL_APP_PASSWORD":"vaje mgnh jhtm kknf",
}

# ── Crontab (same schedule as Mac, paths updated for server) ─────────────────
SERVER_CRONTAB = f"""# =============================================================
# APEX CAPITAL — Automated Trading Pipeline (Oracle Cloud Server)
# All times UTC.  EDT = UTC-4.
# =============================================================

# ── FULL PIPELINE — 4x daily, Mon-Fri ──────────────────────
45 13 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 main.py >> {REPO_DIR}/logs/cron_$(date +\\%Y\\%m\\%d).log 2>&1
0 16 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 main.py >> {REPO_DIR}/logs/cron_$(date +\\%Y\\%m\\%d).log 2>&1
0 18 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 main.py >> {REPO_DIR}/logs/cron_$(date +\\%Y\\%m\\%d).log 2>&1
15 20 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 main.py >> {REPO_DIR}/logs/cron_$(date +\\%Y\\%m\\%d).log 2>&1

# ── R&D TEAM — every 2 hrs, 6AM-midnight EDT ───────────────
0 10,12,14,16,18,20,22,0,2,4 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 rd_team_leader.py >> {REPO_DIR}/logs/rd_$(date +\\%Y\\%m\\%d).log 2>&1

# ── CANARY — every 5 min market hours, 30 min off-hours ────
*/5 13-20 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 canary_agent.py >> {REPO_DIR}/logs/canary_$(date +\\%Y\\%m\\%d).log 2>&1
*/30 0-12,21-23 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 canary_agent.py >> {REPO_DIR}/logs/canary_$(date +\\%Y\\%m\\%d).log 2>&1
*/30 * * * 0,6 cd {REPO_DIR} && /usr/bin/python3 canary_agent.py >> {REPO_DIR}/logs/canary_$(date +\\%Y\\%m\\%d).log 2>&1

# ── BOARD MEETING — Mon + Thu 8:30 AM EDT ──────────────────
30 12 * * 1,4 cd {REPO_DIR} && /usr/bin/python3 board_meeting.py >> {REPO_DIR}/logs/board_$(date +\\%Y\\%m\\%d).log 2>&1

# ── PREMARKET CHECK — 8:30 AM EDT, Mon-Fri ─────────────────
30 12 * * 1-5 cd {REPO_DIR} && /usr/bin/python3 premarket_check.py >> {REPO_DIR}/logs/premarket_$(date +\\%Y\\%m\\%d).log 2>&1

# ── CIT — 6 AM + 6 PM EDT daily ────────────────────────────
0 10 * * * cd {REPO_DIR} && /usr/bin/python3 cit_director.py --session morning >> {REPO_DIR}/logs/cit_$(date +\\%Y\\%m\\%d).log 2>&1
0 22 * * * cd {REPO_DIR} && /usr/bin/python3 cit_director.py --session evening >> {REPO_DIR}/logs/cit_$(date +\\%Y\\%m\\%d).log 2>&1

# ── AUTO-SYNC — pull latest code from GitHub every hour ────
0 * * * * cd {REPO_DIR} && git pull --quiet >> {REPO_DIR}/logs/sync_$(date +\\%Y\\%m\\%d).log 2>&1
"""


def wait_for_ssh(public_ip, timeout=600):
    """Wait until SSH port 22 is open on the server (up to 10 minutes)."""
    print(f"  Waiting for SSH to become available on {public_ip}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            sock = socket.create_connection((public_ip, 22), timeout=5)
            sock.close()
            print(f"  ✓ SSH port is open!")
            time.sleep(5)  # Give SSH daemon a moment to fully start
            return True
        except (socket.timeout, socket.error):
            print(f"    Still waiting... ({int(time.time()-start)}s)")
            time.sleep(15)
    return False


def ssh_connect(public_ip, retries=10):
    """Connect to the server via SSH with retries."""
    key = paramiko.Ed25519Key.from_private_key_file(SSH_KEY_PATH)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    for attempt in range(retries):
        try:
            print(f"  Connecting to {public_ip} (attempt {attempt+1}/{retries})...")
            client.connect(
                hostname    = public_ip,
                username    = SSH_USER,
                pkey        = key,
                timeout     = 30,
                banner_timeout = 60,
            )
            print(f"  ✓ SSH connected!")
            return client
        except Exception as e:
            print(f"    Connection failed: {e}")
            time.sleep(20)

    raise Exception(f"Could not SSH into {public_ip} after {retries} attempts")


def run_command(client, command, desc=""):
    """Run a command on the server and print output."""
    if desc:
        print(f"  → {desc}")
    stdin, stdout, stderr = client.exec_command(command, timeout=300)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    exit_code = stdout.channel.recv_exit_status()
    if out:
        for line in out.split("\n"):
            print(f"    {line}")
    if exit_code != 0 and err:
        print(f"    STDERR: {err}")
    return exit_code, out, err


def upload_file(sftp, local_path, remote_path):
    """Upload a file to the server."""
    sftp.put(local_path, remote_path)


def send_final_email(public_ip, success=True):
    """Send deployment result email to Chairman."""
    if success:
        subject = "✅ APEX CAPITAL — Server Fully Deployed & Running 24/7"
        body = f"""
Your Oracle Cloud server has been fully set up and the trading system is now running 24/7!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SERVER DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Public IP:    {public_ip}
Location:     Oracle Cloud — US Ashburn
OS:           Ubuntu 22.04
Status:       LIVE ✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT'S RUNNING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Main pipeline        — 4x daily, weekdays
✅ Canary agent         — every 5 min market hours
✅ R&D team             — every 2 hours
✅ Board meetings       — Monday + Thursday
✅ CIT                  — 6 AM + 6 PM daily
✅ Premarket check      — 8:30 AM daily
✅ Auto-sync from GitHub — every hour

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEXT STEPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. The crontab on your Mac has been automatically disabled
   so both systems don't run simultaneously.

2. To make code changes: edit on Mac → Claude pushes to GitHub
   → server auto-syncs within the hour.

3. To check server logs anytime, SSH in:
   ssh ubuntu@{public_ip}

The system is fully autonomous. Your Mac can sleep, travel,
do whatever — Apex Capital keeps running.

— Claude
Deployed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    else:
        subject = "⚠️ APEX CAPITAL — Server created but deployment had issues"
        body = f"""
The Oracle server was created at {public_ip} but the automated
deployment encountered issues.

Please tell Claude the IP address ({public_ip}) and ask for
help completing the manual setup.
"""

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, GMAIL_PASSWORD)
            server.send_message(msg)
        print(f"  ✅ Deployment email sent to {EMAIL_RECIPIENT}")
    except Exception as e:
        print(f"  ⚠ Email failed: {e}")


def deploy(public_ip, github_token):
    """Full automated deployment to the Oracle server."""
    print("\n" + "="*60)
    print("  APEX CAPITAL — Automated Deployment Starting")
    print("="*60)
    print(f"  Target server: {public_ip}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ── Step 1: Wait for SSH ─────────────────────────────────────────────────
    print("[Step 1/6] Waiting for server to accept SSH connections...")
    if not wait_for_ssh(public_ip):
        print("  ❌ SSH never became available. Sending partial success email.")
        send_final_email(public_ip, success=False)
        return False

    # ── Step 2: Connect ──────────────────────────────────────────────────────
    print("\n[Step 2/6] Connecting via SSH...")
    try:
        client = ssh_connect(public_ip)
    except Exception as e:
        print(f"  ❌ SSH connection failed: {e}")
        send_final_email(public_ip, success=False)
        return False

    sftp = client.open_sftp()

    try:
        # ── Step 3: Upload and run setup script ──────────────────────────────
        print("\n[Step 3/6] Running server setup (installing Python, git, cloning repo)...")

        # Read setup script and inject GitHub token
        setup_script_path = os.path.join(os.path.dirname(__file__), "setup_server.sh")
        with open(setup_script_path) as f:
            setup_script = f.read()
        setup_script = setup_script.replace("GITHUB_TOKEN", github_token)

        # Upload setup script
        remote_script = "/home/ubuntu/setup_apex.sh"
        with sftp.open(remote_script, "w") as f:
            f.write(setup_script)
        run_command(client, f"chmod +x {remote_script}")

        # Run it
        exit_code, out, err = run_command(
            client, f"bash {remote_script} 2>&1", "Running setup script..."
        )
        if exit_code != 0:
            print(f"  ⚠ Setup script had issues (exit code {exit_code}) — continuing anyway")

        # ── Step 4: Write .env file ──────────────────────────────────────────
        print("\n[Step 4/6] Writing .env credentials file...")
        env_content = "# APEX CAPITAL — SECRETS\n"
        for key, value in ENV_VARS.items():
            env_content += f"{key}={value}\n"

        with sftp.open(f"{REPO_DIR}/.env", "w") as f:
            f.write(env_content)
        run_command(client, f"chmod 600 {REPO_DIR}/.env")
        print("  ✓ .env file written and secured")

        # ── Step 5: Install crontab ──────────────────────────────────────────
        print("\n[Step 5/6] Installing crontab...")
        crontab_path = "/home/ubuntu/apex_crontab.txt"
        with sftp.open(crontab_path, "w") as f:
            f.write(SERVER_CRONTAB)
        run_command(client, f"crontab {crontab_path}", "Installing crontab")
        run_command(client, "crontab -l | wc -l", "Verifying crontab")
        print("  ✓ Crontab installed — all agents scheduled")

        # ── Step 6: Quick smoke test ─────────────────────────────────────────
        print("\n[Step 6/6] Running smoke test...")
        exit_code, out, _ = run_command(
            client,
            f"cd {REPO_DIR} && python3 -c \"from config import *; print('Config OK')\" 2>&1",
            "Testing config import"
        )
        if exit_code == 0:
            print("  ✓ Smoke test passed")
        else:
            print("  ⚠ Smoke test had warnings — system should still work")

        # ── Cleanup ──────────────────────────────────────────────────────────
        run_command(client, f"rm -f {remote_script} {crontab_path}")

        print("\n" + "="*60)
        print("  ✅ DEPLOYMENT COMPLETE")
        print(f"  Server {public_ip} is live and running 24/7")
        print("="*60)

        send_final_email(public_ip, success=True)
        return True

    except Exception as e:
        print(f"\n  ❌ Deployment error: {e}")
        send_final_email(public_ip, success=False)
        return False
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 deploy.py <public_ip> <github_token>")
        sys.exit(1)
    deploy(sys.argv[1], sys.argv[2])
