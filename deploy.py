import paramiko
import time
import os

# Load credentials from .env.local
def load_env(filepath):
    env = {}
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split('=', 1)
                    env[key] = value
    return env

env = load_env('.env.local')
password = env.get('UMBREL_PASSWORD')

if not password:
    print("Error: UMBREL_PASSWORD not found in .env.local")
    exit(1)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    print("Connecting to umbrel.lan...")
    ssh.connect('umbrel.lan', username='umbrel', password=password, timeout=10)
    print("Starting SFTP transfer...")
    sftp = ssh.open_sftp()
    sftp.put('scripts/entrypoint.sh', '/home/umbrel/tunnelsats-entrypoint.sh')
    sftp.put('scripts/verify-dataplane-lean.sh', '/home/umbrel/verify-dataplane-lean.sh')
    sftp.close()
    print("Executing Docker sync and restart...")
    stdin, stdout, stderr = ssh.exec_command('docker cp /home/umbrel/tunnelsats-entrypoint.sh tunnelsats:/app/scripts/entrypoint.sh && docker cp /home/umbrel/verify-dataplane-lean.sh tunnelsats:/app/scripts/verify-dataplane-lean.sh && docker exec tunnelsats chmod +x /app/scripts/entrypoint.sh /app/scripts/verify-dataplane-lean.sh && docker restart tunnelsats')
    
    # Wait for completion
    exit_status = stdout.channel.recv_exit_status()
    print("Restart completed with exit status:", exit_status)
    print("STDOUT:", stdout.read().decode())
    print("STDERR:", stderr.read().decode())

finally:
    ssh.close()
