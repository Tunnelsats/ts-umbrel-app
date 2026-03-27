import os
import sys
import threading

import paramiko

# Load credentials from .env.local
def load_env(filepath):
    env = {}
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split('=', 1)
                    env[key] = value.strip('"\'')
    return env

env = load_env('.env.local')
password = env.get('UMBREL_PASSWORD')

if not password:
    print("Error: UMBREL_PASSWORD not found in .env.local")
    exit(1)

ssh = paramiko.SSHClient()
ssh.load_system_host_keys()
known_hosts_path = os.path.expanduser("~/.ssh/known_hosts")
if os.path.exists(known_hosts_path):
    ssh.load_host_keys(known_hosts_path)
ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

def sftp_put_dir(sftp, local_dir, remote_dir):
    """Recursively syncs a local directory to a remote directory via SFTP, excluding env artifacts."""
    EXCLUDES = {'node_modules', '.venv', '__pycache__', '.git', '.pytest_cache', '.env', '.env.local'}
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass # Directory likely already exists

    for item in os.listdir(local_dir):
        if item in EXCLUDES:
            continue
        local_path = os.path.join(local_dir, item)
        remote_path = os.path.join(remote_dir, item)
        if os.path.isdir(local_path):
            sftp_put_dir(sftp, local_path, remote_path)
        else:
            sftp.put(local_path, remote_path)

try:
    print("Connecting to umbrel.local...")
    ssh.connect('umbrel.local', username='umbrel', password=password, timeout=10)
    print("Starting SFTP transfer...")
    sftp = ssh.open_sftp()
    try:
        # Transfer scripts
        sftp.put('scripts/entrypoint.sh', '/home/umbrel/tunnelsats-entrypoint.sh')
        sftp.put('scripts/verify-dataplane-lean.sh', '/home/umbrel/verify-dataplane-lean.sh')
        sftp.put('umbrel-app.yml', '/home/umbrel/umbrel-app.yml')
        
        # Transfer web and server directories (UI Modernization)
        print("Syncing web directory...")
        sftp_put_dir(sftp, 'web', '/home/umbrel/tunnelsats-web')
        print("Syncing server directory...")
        sftp_put_dir(sftp, 'server', '/home/umbrel/tunnelsats-server')
    finally:
        sftp.close()

    print("Executing Docker sync and restart...")

    UMBREL_APP_DATA = '/home/umbrel/umbrel/app-data/tunnelsats'
    UMBREL_COMPOSE  = f'{UMBREL_APP_DATA}/docker-compose.yml'

    # Phase 1: recreate the container from the Umbrel-managed compose file with APP_DATA_DIR set.
    #           This ensures /data is mounted to the correct app-data path (not the host /data root).
    # Phase 2: inject our local artifacts into the freshly-created container via docker cp.
    # Phase 3: restart so the new entrypoint takes effect.
    remote_commands = [
        # Recreate with correct volume mounts
        f'docker rm -f tunnelsats 2>/dev/null || true',
        f'APP_DATA_DIR={UMBREL_APP_DATA} docker compose -f {UMBREL_COMPOSE} up -d',
        # Inject our local builds (entrypoint fix, web UI, server, metadata)
        'docker cp /home/umbrel/tunnelsats-entrypoint.sh tunnelsats:/app/scripts/entrypoint.sh',
        'docker cp /home/umbrel/verify-dataplane-lean.sh tunnelsats:/app/scripts/verify-dataplane-lean.sh',
        'docker cp /home/umbrel/tunnelsats-web/. tunnelsats:/app/web/',
        'docker cp /home/umbrel/tunnelsats-server/. tunnelsats:/app/server/',
        'docker cp /home/umbrel/umbrel-app.yml tunnelsats:/app/umbrel-app.yml',
        'docker exec tunnelsats chmod +x /app/scripts/entrypoint.sh /app/scripts/verify-dataplane-lean.sh',
        # Restart to activate the patched entrypoint
        'docker restart tunnelsats',
    ]

    stdin, stdout, stderr = ssh.exec_command(' && '.join(remote_commands))

    stdout_chunks = []
    stderr_chunks = []

    def _drain_stream(stream, bucket):
        bucket.append(stream.read())

    stdout_thread = threading.Thread(target=_drain_stream, args=(stdout, stdout_chunks))
    stderr_thread = threading.Thread(target=_drain_stream, args=(stderr, stderr_chunks))
    stdout_thread.start()
    stderr_thread.start()

    exit_status = stdout.channel.recv_exit_status()
    stdout_thread.join()
    stderr_thread.join()

    stdout_text = b"".join(stdout_chunks).decode(errors="replace")
    stderr_text = b"".join(stderr_chunks).decode(errors="replace")

    print("Restart completed with exit status:", exit_status)
    if stdout_text: print("STDOUT:", stdout_text)
    if stderr_text: print("STDERR:", stderr_text)

    if exit_status != 0:
        print("ERROR: Remote command returned non-zero exit status.")
        sys.exit(exit_status)

    print("\nSUCCESS: Deployed to umbrel.lan (APP_DATA_DIR correctly mounted)")

except paramiko.ssh_exception.SSHException as exc:
    print("SSH error:", exc)
    sys.exit(1)
except Exception as exc:
    print("Deployment error:", exc)
    sys.exit(1)
finally:
    ssh.close()
