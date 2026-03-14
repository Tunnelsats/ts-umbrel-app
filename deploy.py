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
try:
    print("Connecting to umbrel.lan...")
    ssh.connect('umbrel.lan', username='umbrel', password=password, timeout=10)
    print("Starting SFTP transfer...")
    sftp = ssh.open_sftp()
    try:
        sftp.put('scripts/entrypoint.sh', '/home/umbrel/tunnelsats-entrypoint.sh')
        sftp.put('scripts/verify-dataplane-lean.sh', '/home/umbrel/verify-dataplane-lean.sh')
    finally:
        sftp.close()
    print("Executing Docker sync and restart...")
    stdin, stdout, stderr = ssh.exec_command(
        'docker cp /home/umbrel/tunnelsats-entrypoint.sh tunnelsats:/app/scripts/entrypoint.sh '
        '&& docker cp /home/umbrel/verify-dataplane-lean.sh tunnelsats:/app/scripts/verify-dataplane-lean.sh '
        '&& docker exec tunnelsats chmod +x /app/scripts/entrypoint.sh /app/scripts/verify-dataplane-lean.sh '
        '&& docker restart tunnelsats'
    )

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
    print("STDOUT:", stdout_text)
    print("STDERR:", stderr_text)
    if exit_status != 0:
        print("ERROR: Remote command returned non-zero exit status.")
        sys.exit(exit_status)
except paramiko.ssh_exception.SSHException as exc:
    print("SSH error:", exc)
    print("Host key verification failed or SSH session could not be established.")
    print("Ensure umbrel.lan exists in ~/.ssh/known_hosts and retry.")
    sys.exit(1)
except Exception as exc:
    print("Deployment error:", exc)
    sys.exit(1)

finally:
    ssh.close()
