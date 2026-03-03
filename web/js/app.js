// State
let pollInterval;
let activePaymentHash = null;

// Initialization
document.addEventListener("DOMContentLoaded", () => {
    fetchStatus();
    fetchServers();
    setInterval(fetchStatus, 10000);
});

// UI Routing
function switchTab(tabId) {
    document.querySelectorAll('main > section').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('nav > button').forEach(el => {
        el.classList.remove('tab-active', 'font-bold');
        el.classList.add('text-gray-400');
    });

    document.getElementById(`view-${tabId}`).classList.remove('hidden');
    const btn = document.getElementById(`tab-${tabId}`);
    btn.classList.add('tab-active');
    btn.classList.remove('text-gray-400');
}

// 1. Fetch Local Status
async function fetchStatus() {
    try {
        const res = await fetch('/api/local/status');
        const data = await res.json();

        // Update Header Badge
        const badge = document.getElementById('statusBadge');
        if (data.wg_status === 'Connected') {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-green-900/50 text-tsgreen border border-green-700";
            badge.innerText = "Tunnel Active";
            document.getElementById('txt-wg-status').className = "font-mono text-tsgreen font-bold";
        } else {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-red-900/50 text-red-500 border border-red-700";
            badge.innerText = "Tunnel Down";
            document.getElementById('txt-wg-status').className = "font-mono text-red-500 font-bold";
        }

        // Update Dashboard Text
        document.getElementById('txt-wg-status').innerText = data.wg_status;
        document.getElementById('txt-pubkey').innerText = data.wg_pubkey || "Not available";

        let confs = data.configs_found.length > 0 ? data.configs_found.join(", ") : "None Detected";
        document.getElementById('txt-configs').innerText = confs;

        document.getElementById('txt-lnd-ip').innerText = data.lnd_ip || "Not Detected";
        document.getElementById('txt-cln-ip').innerText = data.cln_ip || "Not Detected";
        document.getElementById('txt-dataplane-mode').innerText = data.dataplane_mode || "Unknown";
        document.getElementById('txt-target-container').innerText = data.target_container || "Not detected";
        document.getElementById('txt-target-ip').innerText = data.target_ip || "Not detected";
        document.getElementById('txt-forwarding-port').innerText = data.forwarding_port || "Not detected";
        document.getElementById('txt-rules-synced').innerText = data.rules_synced ? "Yes" : "No";

        const net = data.docker_network || {};
        const netName = net.name || "docker-tunnelsats";
        const netSubnet = net.subnet || "unknown";
        document.getElementById('txt-docker-network').innerText = `${netName} (${netSubnet})`;
        document.getElementById('txt-last-reconcile').innerText = data.last_reconcile_at || "Never";
        document.getElementById('txt-last-error').innerText = data.last_error || "None";

    } catch (e) {
        console.error("Failed to fetch status", e);
    }
}

// 2. Fetch Servers
async function fetchServers() {
    try {
        const res = await fetch('/api/servers');
        const servers = await res.json();
        const sel = document.getElementById('server-select');
        sel.innerHTML = "";
        servers.forEach(s => {
            let opt = document.createElement('option');
            opt.value = s.id;
            opt.innerText = `${s.country} - ${s.city} (Port: ${s.wireguardPort})`;
            sel.appendChild(opt);
        });
    } catch (e) { }
}

// 3. Purchase Flow
async function createSub() {
    const serverId = document.getElementById('server-select').value;
    const duration = parseInt(document.getElementById('duration-select').value);

    if (!serverId) return;

    document.getElementById('btn-create').innerText = "Loading...";
    document.getElementById('btn-create').disabled = true;

    try {
        const res = await fetch('/api/subscription/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ serverId, duration, referralCode: null })
        });
        const data = await res.json();

        if (data.paymentHash && data.invoice) {
            activePaymentHash = data.paymentHash;
            document.getElementById('invoice-bolt11').value = data.invoice;
            document.getElementById('pay-link').href = `lightning:${data.invoice}`;
            document.getElementById('invoice-box').classList.remove('hidden');

            // Start Polling
            pollInterval = setInterval(pollPayment, 3000);
        }
    } catch (e) {
        alert("Error creating subscription: " + e.message);
    } finally {
        document.getElementById('btn-create').innerText = "Generate Lightning Invoice";
        document.getElementById('btn-create').disabled = false;
    }
}

async function pollPayment() {
    if (!activePaymentHash) return;

    try {
        const res = await fetch(`/api/subscription/${activePaymentHash}`);
        const data = await res.json();

        if (data.status === 'PAID') {
            clearInterval(pollInterval);
            document.getElementById('invoice-box').innerHTML = `<h3 class="text-tsgreen font-bold text-center mb-2">Payment Received!</h3><p class="text-sm text-gray-300 text-center">Provisioning VPN config...</p>`;
            claimSubscription();
        }
    } catch (e) { }
}

async function claimSubscription() {
    try {
        const res = await fetch('/api/subscription/claim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paymentHash: activePaymentHash, referralCode: null })
        });

        if (res.ok) {
            const configMsg = await configureNode();
            document.getElementById('invoice-box').innerHTML = `<h3 class="text-tsgreen font-bold text-center mb-2">Installation Complete!</h3><p class="text-sm text-gray-300 text-center mb-2">Your VPN configuration has been securely stored.</p><p class="text-xs text-tsyellow text-center mb-4">${configMsg}</p><button onclick="restartTunnel(); switchTab('dashboard');" class="mt-4 w-full bg-tsyellow hover:bg-yellow-500 text-black font-bold py-2 px-6 rounded transition">Restart Apps & Tunnel</button>`;
        } else {
            document.getElementById('invoice-box').innerHTML = `<h3 class="text-red-500 font-bold text-center mb-2">Provisioning Error</h3><p class="text-sm text-gray-300 text-center">Payment was successful, but config provisioning failed.</p>`;
        }
    } catch (e) { }
}

async function configureNode() {
    try {
        const res = await fetch('/api/local/configure-node', { method: 'POST' });
        const data = await res.json();

        let msg = "";
        if (data.lnd && data.cln) msg = "LND and CLN were auto-configured!";
        else if (data.lnd) msg = "LND was auto-configured! Please restart LND via UI.";
        else if (data.cln) msg = "CLN was auto-configured! Please restart CLN via UI.";
        else msg = "Auto-config unavailable due to Umbrel permissions. Please follow the manual setup guide.";

        return msg;
    } catch (e) {
        return "Auto-config unavailable. Please configure manually.";
    }
}

// 4. Import Config
async function importConfig() {
    const txt = document.getElementById('config-text').value;
    const msg = document.getElementById('import-msg');

    msg.innerText = "Importing...";
    msg.className = "text-center mt-4 text-sm text-gray-400";

    try {
        const formData = new FormData();
        formData.append('config_text', txt);

        const res = await fetch('/api/local/upload-config', {
            method: 'POST',
            body: formData
        });

        const data = await res.json();
        if (res.ok) {
            const configMsg = await configureNode();
            msg.innerText = `Config imported successfully! ${configMsg}`;
            msg.className = "text-center mt-4 text-sm font-bold text-tsgreen";
            setTimeout(() => {
                restartTunnel();
                switchTab('dashboard');
            }, 3000);
        } else {
            msg.innerText = data.error || "Import failed.";
            msg.className = "text-center mt-4 text-sm font-bold text-red-500";
        }
    } catch (e) {
        msg.innerText = e.message;
        msg.className = "text-center mt-4 text-sm font-bold text-red-500";
    }
}

async function restartTunnel() {
    try {
        await fetch('/api/local/restart', { method: 'POST' });
        // The container entrypoint will catch the trigger file, and restart `wg-quick`
        setTimeout(fetchStatus, 3000);
    } catch (e) { }
}

function waitMs(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function pollReconcileResult(requestId, timeoutMs = 12000, intervalMs = 250) {
    const attempts = Math.ceil(timeoutMs / intervalMs);
    for (let i = 0; i < attempts; i += 1) {
        const res = await fetch(`/api/local/reconcile/${encodeURIComponent(requestId)}`);
        const data = await res.json();

        if (res.ok && data.success && data.complete) {
            return data;
        }

        if (!res.ok) {
            throw new Error(data.error || "Failed to fetch reconcile status.");
        }

        await waitMs(intervalMs);
    }

    throw new Error("Reconcile timed out.");
}

async function reconcileTunnel() {
    const msg = document.getElementById('txt-reconcile-msg');
    msg.innerText = "Reconciling dataplane...";
    msg.className = "text-xs text-gray-400 mt-2";
    try {
        const triggerRes = await fetch('/api/local/reconcile', { method: 'POST' });
        const triggerData = await triggerRes.json();
        if (!triggerRes.ok || !triggerData.success || !triggerData.request_id) {
            msg.innerText = triggerData.error || "Unable to trigger reconcile.";
            msg.className = "text-xs text-red-500 mt-2";
            return;
        }

        msg.innerText = "Reconcile requested. Waiting for dataplane sync...";
        const result = await pollReconcileResult(triggerData.request_id);

        msg.innerText = `Reconciled. Changes applied: ${result.changed ? "yes" : "no"}.`;
        msg.className = "text-xs text-tsgreen mt-2";
    } catch (e) {
        msg.innerText = e.message;
        msg.className = "text-xs text-red-500 mt-2";
    }
    fetchStatus();
}
