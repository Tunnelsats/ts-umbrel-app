// State
let pollInterval;
let activePaymentHash = null;
let purchaseMode = "buy"; // "buy" or "renew"
// Initialization
document.addEventListener("DOMContentLoaded", () => {
    fetchStatus();
    fetchServers();
    setInterval(fetchStatus, 10000);
});

// UI Routing
function switchTab(tabId) {
    document.querySelectorAll('main section').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('nav > button').forEach(el => {
        el.classList.remove('nav-active', 'bg-gray-800', 'text-white', 'border-tsgreen');
        el.classList.add('text-gray-400', 'border-transparent');
    });

    document.getElementById(`view-${tabId}`).classList.remove('hidden');
    const btn = document.getElementById(`nav-${tabId}`);
    btn.classList.add('nav-active', 'bg-gray-800', 'text-white', 'border-tsgreen');
    btn.classList.remove('text-gray-400', 'border-transparent');
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
        const pk = data.wg_pubkey || "Not available";
        document.getElementById('txt-pubkey').innerText = pk;

        // Setup pubkey for renewal
        document.getElementById('renew-pubkey').value = pk;

        let confs = data.configs_found.length > 0 ? data.configs_found.join(", ") : "None Detected";
        document.getElementById('txt-configs').innerText = confs;

<<<<<<< HEAD
        // NOTE: LND/CLN IP detection moved to PR #3 (dataplane layer)

        if (data.version) {
            document.getElementById('app-version').innerText = data.version;
        }

        // Update Dashboard Banner
        const bannerTitle = document.getElementById('dashboard-banner-title');
        const bannerText = document.getElementById('dashboard-banner-text');
        const bannerDots = document.getElementById('dashboard-banner-dots');

        if (data.wg_status === 'Connected') {
            bannerTitle.innerText = "Network Layer Active";
            bannerText.innerText = "Secure WireGuard tunneling provided by Tunnelsats. Your Lightning P2P traffic is now encrypted and routed through our private global exit nodes.";
            bannerDots.classList.remove('hidden');
        } else {
            bannerTitle.innerText = "Hybrid Lightning Connectivity";
            bannerText.innerText = "TunnelSats enables privacy-preserving clearnet connectivity for your node. Keep your home IP hidden while benefiting from faster, more reliable Lightning routing.";
            bannerDots.classList.add('hidden');
        }
=======
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
>>>>>>> 46623c700b007fd79a9a4ddd8f5d8b304af3899a

    } catch (e) {
        console.error("Failed to fetch status", e);
    }
}

// 2. Fetch Servers
async function fetchServers() {
    try {
        const res = await fetch('/api/servers');
        const servers = await res.json();

        const selBuyList = document.getElementById('buy-server-list');
        selBuyList.innerHTML = "";

        servers.forEach(s => {
            let btn = document.createElement('button');
            btn.type = 'button';
            const label = `${s.country} - ${s.city} (Port: ${s.wireguardPort})`;
            btn.setAttribute('onclick', `selectOption('buy-server', '${s.id}', '${label}')`);
            btn.className = 'w-full text-left px-4 py-3 text-white hover:bg-gray-700 transition-colors border-b border-gray-700/50 hover:pl-6 block';
            btn.innerText = label;
            selBuyList.appendChild(btn);
        });

        if (servers.length > 0) {
            const firstLabel = `${servers[0].country} - ${servers[0].city} (Port: ${servers[0].wireguardPort})`;
            selectOption('buy-server', servers[0].id, firstLabel);
        } else {
            document.getElementById('buy-server-label').innerText = "No servers available";
        }
    } catch (e) { }
}

// Purchase / Renew Mode Switch (Removed, handled by tabs now)

// Initialize QRCodes
let qrBuy = null;
let qrRenew = null;

function renderQR(mode, text) {
    const boxId = `qr-placeholder-${mode}`;
    const box = document.getElementById(boxId);
    box.innerHTML = ""; // Clear placeholder

    if (mode === 'buy') {
        if (!qrBuy) qrBuy = new QRCode(box, { width: 192, height: 192 });
        qrBuy.makeCode(text);
    } else {
        if (!qrRenew) qrRenew = new QRCode(box, { width: 192, height: 192 });
        qrRenew.makeCode(text);
    }
}

// 3. Purchase Flow
async function createSub(mode) {
    const duration = parseInt(document.getElementById(`${mode}-duration-select`).value);
    let serverId = null;
    if (mode === 'buy') {
        serverId = document.getElementById('buy-server-select').value;
        if (!serverId) return;
    }

    // Save purchase mode globally for polling
    purchaseMode = mode;

    // Helper for ui errors
    function displayPurchaseError(msg) {
        let errEl = document.getElementById(`purchase-error-${mode}`);
        if (!errEl) {
            errEl = document.createElement('p');
            errEl.id = `purchase-error-${mode}`;
            errEl.className = 'text-red-500 font-bold text-center mt-2';
            const container = document.getElementById(`btn-create-${mode}`).parentNode;
            container.appendChild(errEl);
        }
        errEl.innerText = msg;
    }

    const oldErr = document.getElementById(`purchase-error-${mode}`);
    if (oldErr) oldErr.remove();

    document.getElementById(`btn-create-${mode}`).innerText = "Loading...";
    document.getElementById(`btn-create-${mode}`).disabled = true;

    try {
        let endpoint = '/api/subscription/create';
        let payload = { serverId, duration, referralCode: null };

        if (mode === 'renew') {
            endpoint = '/api/subscription/renew';
            const wgPublicKey = document.getElementById('renew-pubkey').value;
            payload = { duration, wgPublicKey };
            if (!wgPublicKey || wgPublicKey === "Not available") {
                displayPurchaseError("Cannot renew without an active public key from a connected VPN.");
                document.getElementById(`btn-create-${mode}`).innerText = "Generate Renewal Invoice";
                document.getElementById(`btn-create-${mode}`).disabled = false;
                return;
            }
        }

        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();

        if (data.paymentHash && data.invoice) {
            activePaymentHash = data.paymentHash;
            document.getElementById(`invoice-bolt11-${mode}`).value = data.invoice;
            document.getElementById(`pay-link-${mode}`).href = `lightning:${data.invoice}`;

            renderQR(mode, data.invoice);
            document.getElementById(`invoice-box-${mode}`).classList.remove('hidden');

            // Start Polling (clear any existing interval first)
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(pollPayment, 3000);
        } else if (data.message) {
            displayPurchaseError(data.message);
        }
    } catch (e) {
        displayPurchaseError("Error creating subscription: " + e.message);
    } finally {
        document.getElementById(`btn-create-${mode}`).innerText = mode === 'renew' ? "Generate Renewal Invoice" : "Generate Lightning Invoice";
        document.getElementById(`btn-create-${mode}`).disabled = false;
    }
}

async function pollPayment() {
    if (!activePaymentHash) return;

    try {
        const res = await fetch(`/api/subscription/${activePaymentHash}`);
        const data = await res.json();

        if (data.status === 'PAID') {
            clearInterval(pollInterval);
            const invoiceBox = document.getElementById(`invoice-box-${purchaseMode}`);
            invoiceBox.innerHTML = ''; // Clear content

            if (purchaseMode === 'buy') {
                const h3 = document.createElement('h3');
                h3.className = 'text-tsgreen font-bold text-center mb-2';
                h3.textContent = 'Payment Received!';

                const p = document.createElement('p');
                p.className = 'text-sm text-gray-300 text-center mb-4';
                p.textContent = 'Proceed to the Install tab to finalize your setup.';

                const button = document.createElement('button');
                button.className = 'mt-4 w-full bg-tsgreen hover:bg-cyan-500 text-gray-900 font-bold py-2 px-6 rounded transition shadow-lg';
                button.textContent = 'Proceed to Installation';
                button.onclick = () => {
                    document.getElementById('pending-install-section').classList.remove('hidden');
                    switchTab('import');
                };

                invoiceBox.append(h3, p, button);
            } else {
                const h3 = document.createElement('h3');
                h3.className = 'text-tsgreen font-bold text-center mb-2';
                h3.textContent = 'Renewal Successful!';

                const p = document.createElement('p');
                p.className = 'text-sm text-gray-300 text-center mb-4';
                p.textContent = 'Your VPN subscription has been extended successfully. No restarts required.';

                const button = document.createElement('button');
                button.className = 'mt-4 w-full bg-tsyellow hover:bg-yellow-500 text-black font-bold py-2 px-6 rounded transition shadow-lg';
                button.textContent = 'Return to Dashboard';
                button.onclick = () => switchTab('dashboard');

                invoiceBox.append(h3, p, button);
            }
        }
    } catch (e) { }
}

async function claimSubscription(mode) {
    let btnInstall = null;
    if (mode === 'import') {
        btnInstall = document.getElementById('btn-claim-install');
        btnInstall.disabled = true;
        btnInstall.innerText = "Installing...";
    }

    try {
        const res = await fetch('/api/subscription/claim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paymentHash: activePaymentHash, referralCode: null })
        });

        const invoiceBox = document.getElementById(`invoice-box-${mode}`);
        invoiceBox.innerHTML = '';

        if (res.ok) {
            const configMsg = "Node configuration will be available after dataplane setup.";

            const h3 = document.createElement('h3');
            h3.className = 'text-tsgreen font-bold text-center mb-2';
            h3.textContent = 'Installation Complete!';

            const p1 = document.createElement('p');
            p1.className = 'text-sm text-gray-300 text-center mb-2';
            p1.textContent = 'Your VPN configuration has been securely stored.';

            const p2 = document.createElement('p');
            p2.className = 'text-xs text-tsyellow text-center mb-4';
            p2.textContent = configMsg;

            const button = document.createElement('button');
            button.className = 'mt-4 w-full bg-tsyellow hover:bg-yellow-500 text-black font-bold py-2 px-6 rounded transition shadow-lg';
            button.textContent = 'Restart Apps & Tunnel';
            button.onclick = () => {
                restartTunnel();
                document.getElementById('pending-install-section').classList.add('hidden');
                activePaymentHash = null;
                switchTab('dashboard');
            };

            if (btnInstall) btnInstall.classList.add('hidden'); // Hide the install button now
            invoiceBox.append(h3, p1, p2, button);
        } else {
            const h3 = document.createElement('h3');
            h3.className = 'text-red-500 font-bold text-center mb-2';
            h3.textContent = 'Provisioning Error';

            const p = document.createElement('p');
            p.className = 'text-sm text-gray-300 text-center';
            p.textContent = 'Payment was successful, but config provisioning failed.';

            invoiceBox.append(h3, p);
            if (btnInstall) {
                btnInstall.disabled = false;
                btnInstall.innerText = "Retry Installation";
            }
        }
    } catch (e) {
        if (btnInstall) {
            btnInstall.disabled = false;
            btnInstall.innerText = "Retry Installation";
        }
    }
}

// NOTE: configureNode() and restoreNode() moved to PR #3/PR #4 (dataplane + API integration).

// 4. Import Config
async function importConfig() {
    const txt = document.getElementById('config-text').value;
    const msg = document.getElementById('import-msg');
    const existingConfigs = document.getElementById('txt-configs').innerText;

    if (existingConfigs !== "None Detected" && existingConfigs !== "Loading..." && existingConfigs !== "") {
        if (!confirm("Warning: You already have a Tunnelsats configuration active. Importing a new config will overwrite it. Do you wish to proceed?")) {
            return;
        }
    }

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
            const configMsg = "Node configuration will be available after dataplane setup.";
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

// NOTE: restoreNode() moved to PR #3/PR #4.

// Custom Dropdown Logic
let openDropdown = null;

function toggleDropdown(id) {
    const list = document.getElementById(`${id}-list`);
    const caret = document.getElementById(`${id}-caret`);

    if (openDropdown && openDropdown !== id) {
        closeDropdown(openDropdown);
    }

    if (list.classList.contains('hidden')) {
        list.classList.remove('hidden');
        setTimeout(() => {
            list.classList.remove('scale-95', 'opacity-0');
            caret.classList.add('rotate-180');
        }, 10);
        openDropdown = id;
    } else {
        closeDropdown(id);
    }
}

function closeDropdown(id) {
    const list = document.getElementById(`${id}-list`);
    const caret = document.getElementById(`${id}-caret`);
    if (!list || !caret) return;

    list.classList.add('scale-95', 'opacity-0');
    caret.classList.remove('rotate-180');
    setTimeout(() => {
        list.classList.add('hidden');
    }, 200);
    if (openDropdown === id) openDropdown = null;
}

function selectOption(dropdownId, value, label) {
    const selectEl = document.getElementById(`${dropdownId}-select`);
    const labelEl = document.getElementById(`${dropdownId}-label`);
    if (selectEl) selectEl.value = value;
    if (labelEl) {
        labelEl.innerText = label;
        labelEl.classList.replace('text-gray-400', 'text-white');
    }
    closeDropdown(dropdownId);
}

document.addEventListener('click', (e) => {
    if (openDropdown) {
        const container = document.getElementById(`${openDropdown}-dropdown-container`);
        if (container && !container.contains(e.target)) {
            closeDropdown(openDropdown);
        }
    }
});
