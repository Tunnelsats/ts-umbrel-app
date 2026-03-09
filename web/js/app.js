// State (var for window-scope testability)
var pollInterval;
var activePaymentHash = null;
var purchaseMode = "buy"; // "buy" or "renew"

// Pricing Configuration
const BASE_PRICE_USD = 3;
const DISCOUNTS = { 1: 0, 3: 0.05, 6: 0.10, 12: 0.20 };
let currentSatsPerDollar = null;
const POLL_INTERVAL_MS = 3000;

async function fetchPricing() {
    try {
        const res = await fetch('https://mempool.space/api/v1/prices');
        const data = await res.json();
        if (data && data.USD) {
            currentSatsPerDollar = 100000000 / data.USD;
        }
    } catch(e) {
        console.warn("Could not fetch BTC price", e);
    }
    renderDurations();
}

function calculatePrice(months) {
    const discount = DISCOUNTS[months] || 0;
    const grossUsd = BASE_PRICE_USD * months;
    const amountUsd = grossUsd * (1 - discount);
    
    let satsStr = "";
    if (currentSatsPerDollar) {
        const amountSats = Math.floor(amountUsd * currentSatsPerDollar);
        satsStr = ` (${amountSats.toLocaleString()} sats)`;
    }
    
    return { amountUsd, satsStr, discount };
}

function renderDurations() {
    const durations = [1, 3, 6, 12];
    const lists = ['buy-duration', 'renew-duration'];
    
    lists.forEach(mode => {
        const listEl = document.getElementById(`${mode}-list`);
        if (!listEl) return;
        listEl.innerHTML = "";
        
        const currentSelect = document.getElementById(`${mode}-select`);
        const currentValue = currentSelect ? currentSelect.value : "1";
        const labelEl = document.getElementById(`${mode}-label`);
        
        durations.forEach((months) => {
            const { amountUsd, satsStr, discount } = calculatePrice(months);
            const discountPercent = discount * 100;
            const discountStr = discount > 0 ? ` (${discountPercent}% off)` : "";
            const monthStr = months === 1 ? "1 Month" : `${months} Months`;
            const label = `${monthStr}${discountStr} - $${amountUsd.toFixed(2).replace(/\.00$/, '')}${satsStr}`;
            
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'w-full text-left px-4 py-3 text-white hover:bg-gray-700 transition-colors border-b border-gray-700/50 hover:pl-6 block';
            btn.innerText = label;
            btn.addEventListener('click', () => selectOption(mode, String(months), label));
            listEl.appendChild(btn);
            
            if (String(months) === currentValue && labelEl) {
                labelEl.innerText = label;
            }
        });
    });
}

// Initialization
document.addEventListener("DOMContentLoaded", () => {
    fetchStatus();
    fetchServers();
    fetchPricing();

    // Attach programmatic event listeners
    const btnRecon = document.getElementById('btn-reconcile');
    if (btnRecon) {
        btnRecon.addEventListener('click', reconcileTunnel);
    }
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

    // Reset polling and invoice UI when navigating away
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
    ['buy', 'renew'].forEach(mode => {
        const box = document.getElementById(`invoice-box-${mode}`);
        const btnCreate = document.getElementById(`btn-create-${mode}`);
        
        if (activePaymentHash && tabId === mode && purchaseMode === mode) {
            // Restore active invoice UI and resume polling
            if (box) box.classList.remove('hidden');
            if (btnCreate) {
                btnCreate.innerText = "Invoice Active...";
                btnCreate.disabled = true;
            }
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
        } else {
            // Hide and reset inactive or completed flows
            if (box) box.classList.add('hidden');
            if (btnCreate) {
                btnCreate.innerText = mode === 'renew' ? "Generate Renewal Invoice" : "Generate Lightning Invoice";
                btnCreate.disabled = false;
            }
        }
    });

    if (tabId === 'renew') {
        fetch('/api/local/meta').then(r => r.json()).then(data => {
            document.getElementById('renew-server').value = data.serverId || 'Not found';
            document.getElementById('renew-pubkey').value = data.wgPublicKey || 'Not found';
        }).catch(e => {
            console.error("Could not load metadata for renew:", e);
            document.getElementById('renew-server').value = 'Error loading';
            document.getElementById('renew-pubkey').value = 'Error loading';
        });
    }
}

// 1. Fetch Local Status
async function fetchStatus() {
    try {
        const res = await fetch('/api/local/status');
        const data = await res.json();

        // Update Header Badge
        const badge = document.getElementById('statusBadge');
        if (data.wg_status === 'Connected' && data.target_container && data.rules_synced) {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-green-900/50 text-tsgreen border border-green-700";
            badge.innerText = "Protected";
            document.getElementById('txt-wg-status').className = "font-mono text-tsgreen font-bold";
        } else if (data.wg_status === 'Connected') {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-yellow-900/50 text-tsyellow border border-yellow-700";
            badge.innerText = "Connected";
            document.getElementById('txt-wg-status').className = "font-mono text-tsyellow font-bold";
        } else {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-red-900/50 text-red-500 border border-red-700";
            badge.innerText = "Tunnel Down";
            document.getElementById('txt-wg-status').className = "font-mono text-red-500 font-bold";
        }

        // Update Dashboard Text
        document.getElementById('txt-wg-status').innerText = data.wg_status;
        const pk = data.wg_pubkey || "Not available";
        document.getElementById('txt-pubkey').innerText = pk;

        // Note: renew-pubkey is populated via /api/local/meta on tab switch instead.

        let confs = data.configs_found.length > 0 ? data.configs_found.join(", ") : "None Detected";
        document.getElementById('txt-configs').innerText = confs;

        // --- PR A: Update Dataplane UI ---
        const targetContainer = data.target_container;
        const targetIp = data.target_ip;
        document.getElementById('txt-target').innerText = targetContainer ? `${targetContainer} (${targetIp})` : "Not Configured";

        const fwdPort = data.forwarding_port;
        const fwdEl = document.getElementById('txt-forwarding');
        const fwdSpan = fwdEl ? fwdEl.querySelector('span') : null;
        if (fwdSpan) {
            fwdSpan.innerText = fwdPort || '--';
            fwdSpan.className = fwdPort ? "text-white font-mono" : "text-gray-400 font-mono";
        }

        const badgeRules = document.getElementById('badge-rules');
        if (targetContainer) {
            if (data.rules_synced) {
                badgeRules.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-green-700 bg-green-900/50 text-tsgreen";
                badgeRules.innerText = "Synced";
            } else {
                badgeRules.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-yellow-700 bg-yellow-900/50 text-tsyellow";
                badgeRules.innerText = "Out of Sync";
            }
        } else {
             badgeRules.className = "hidden";
        }

        const btnRecon = document.getElementById('btn-reconcile');
        if (targetContainer) {
            btnRecon.classList.remove('hidden');
            btnRecon.classList.add('flex');
        } else {
             btnRecon.classList.add('hidden');
             btnRecon.classList.remove('flex');
        }

        const lastRec = data.last_reconcile_at;
        document.getElementById('txt-reconcile').innerText = lastRec ? new Date(lastRec).toLocaleString() : "Never";

        const errEl = document.getElementById('txt-error');
        if (data.last_error) {
            errEl.classList.remove('hidden');
            errEl.querySelector('span').innerText = data.last_error;
        } else {
            errEl.classList.add('hidden');
        }

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

    } catch (e) {
        console.error("Failed to fetch status", e);
    }
}

// 2. Fetch Servers
async function fetchServers() {
    try {
        const res = await fetch('/api/servers');
        const data = await res.json();
        // Handle both {servers: [...]} (upstream API) and flat array formats
        const servers = Array.isArray(data) ? data : (data.servers || []);

        const selBuyList = document.getElementById('buy-server-list');
        selBuyList.innerHTML = "";
        servers.forEach(s => {
            let btn = document.createElement('button');
            btn.type = 'button';
            const label = `${s.flag} ${s.country} — ${s.city}`;
            btn.addEventListener('click', () => selectOption('buy-server', s.id, label));
            btn.className = 'w-full text-left px-4 py-3 text-white hover:bg-gray-700 transition-colors border-b border-gray-700/50 hover:pl-6 block';
            btn.innerText = label;
            selBuyList.appendChild(btn);
        });

        if (servers.length > 0) {
            const firstLabel = `${servers[0].flag} ${servers[0].country} — ${servers[0].city}`;
            selectOption('buy-server', servers[0].id, firstLabel);
        } else {
            document.getElementById('buy-server-label').innerText = "No servers available";
        }
    } catch (e) { }
}

// Purchase / Renew Mode Switch (Removed, handled by tabs now)

// Initialize QRCodes
var qrBuy = null;
var qrRenew = null;

function renderQR(mode, text) {
    const boxId = `qr-placeholder-${mode}`;
    const box = document.getElementById(boxId);

    if (mode === 'buy') {
        if (qrBuy) {
            qrBuy.clear();
        } else {
            box.innerHTML = ""; // Clear loading text placeholder only on first run
            qrBuy = new QRCode(box, { width: 192, height: 192 });
        }
        qrBuy.makeCode(text);
    } else {
        if (qrRenew) {
            qrRenew.clear();
        } else {
            box.innerHTML = ""; // Clear loading text placeholder only on first run
            qrRenew = new QRCode(box, { width: 192, height: 192 });
        }
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
            const renewServerId = document.getElementById('renew-server').value;
            
            payload = { duration };
            // Avoid sending placeholders so backend autofill can kick in if needed
            if (wgPublicKey && wgPublicKey !== 'Not found' && wgPublicKey !== 'Error loading') {
                payload.wgPublicKey = wgPublicKey;
            }
            if (renewServerId && renewServerId !== 'Not found' && renewServerId !== 'Error loading') {
                payload.serverId = renewServerId;
            }

            if (!payload.wgPublicKey) {
                displayPurchaseError("No target public key found. Please purchase a new subscription or import an existing configuration.");
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
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
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

    // Don't poll while the browser tab is completely hidden to save resources.
    // However, DO keep polling if they just switched to the Dashboard UI tab 
    // so they still get the success UI when they click back.
    if (document.hidden) return;

    try {
        const res = await fetch(`/api/subscription/${activePaymentHash}`);
        const data = await res.json();

        if (data.status === 'paid') {
            clearInterval(pollInterval);
            const invoiceBox = document.getElementById(`invoice-box-${purchaseMode}`);
            invoiceBox.innerHTML = ''; // Clear content

            if (purchaseMode === 'buy') {
                // Celebration SVG (safely parsed to avoid innerHTML AST warnings)
                const svgString = `
                <svg viewBox="0 0 120 120" width="80" height="80" class="mx-auto mb-4">
                    <circle cx="60" cy="60" r="50" fill="none" stroke="#22c55e" stroke-width="4" opacity="0.3">
                        <animate attributeName="r" from="20" to="55" dur="1s" repeatCount="indefinite"/>
                        <animate attributeName="opacity" from="0.6" to="0" dur="1s" repeatCount="indefinite"/>
                    </circle>
                    <circle cx="60" cy="60" r="30" fill="#22c55e" opacity="0.15"/>
                    <path d="M45 60 L55 72 L78 48" fill="none" stroke="#22c55e" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">
                        <animate attributeName="stroke-dasharray" from="0 100" to="60 100" dur="0.6s" fill="freeze"/>
                    </path>
                    <circle cx="30" cy="30" r="3" fill="#facc15"><animate attributeName="cy" from="30" to="10" dur="0.8s" repeatCount="indefinite"/><animate attributeName="opacity" from="1" to="0" dur="0.8s" repeatCount="indefinite"/></circle>
                    <circle cx="90" cy="35" r="2" fill="#22c55e"><animate attributeName="cy" from="35" to="15" dur="1s" repeatCount="indefinite"/><animate attributeName="opacity" from="1" to="0" dur="1s" repeatCount="indefinite"/></circle>
                    <circle cx="75" cy="25" r="2" fill="#facc15"><animate attributeName="cy" from="25" to="5" dur="0.7s" repeatCount="indefinite"/><animate attributeName="opacity" from="1" to="0" dur="0.7s" repeatCount="indefinite"/></circle>
                </svg>`;

                const parser = new DOMParser();
                const celebrationSvg = parser.parseFromString(svgString, 'image/svg+xml').documentElement;

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

                invoiceBox.append(celebrationSvg, h3, p, button);
            } else {
                // Renewals don't need claim/provisioning, so clear active hash after payment confirmation.
                activePaymentHash = null;
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

// 5. Dataplane Reconcile Logic
let reconcilePollCount = 0;
const MAX_RECONCILE_POLLS = 30; // Max 1 minute polling (30 * 2000ms)

async function reconcileTunnel() {
    const btn = document.getElementById('btn-reconcile');
    const spinner = document.getElementById('reconcile-spinner');
    const text = document.getElementById('reconcile-text');

    btn.disabled = true;
    spinner.classList.remove('hidden');
    text.innerText = "Triggering...";

    try {
        const res = await fetch('/api/local/reconcile', { method: 'POST' });
        const data = await res.json();

        if (res.status === 202 && data.request_id) {
            text.innerText = "Reconciling...";
            reconcilePollCount = 0;
            pollReconcileStatus(data.status_url);
        } else {
            text.innerText = "Error Triggering";
            setTimeout(resetReconcileBtn, 3000);
        }
    } catch (e) {
        text.innerText = "Network Error";
        setTimeout(resetReconcileBtn, 3000);
    }
}

async function pollReconcileStatus(url) {
    if (reconcilePollCount >= MAX_RECONCILE_POLLS) {
        document.getElementById('reconcile-text').innerText = "Timeout waiting for Dataplane";
        setTimeout(resetReconcileBtn, 4000);
        fetchStatus();
        return;
    }
    reconcilePollCount++;

    try {
        const res = await fetch(url);
        const data = await res.json();

        if (data.complete) {
            if (data.success) {
                document.getElementById('reconcile-text').innerText = "Success!";
                setTimeout(resetReconcileBtn, 3000);
            } else {
                document.getElementById('reconcile-text').innerText = "Failed";
                setTimeout(resetReconcileBtn, 3000);
            }
            fetchStatus(); // Refresh cards
        } else {
            // Still polling
            setTimeout(() => pollReconcileStatus(url), 2000);
        }
    } catch (e) {
        setTimeout(() => pollReconcileStatus(url), 2000);
    }
}

function resetReconcileBtn() {
    const btn = document.getElementById('btn-reconcile');
    const spinner = document.getElementById('reconcile-spinner');
    const text = document.getElementById('reconcile-text');
    
    btn.disabled = false;
    spinner.classList.add('hidden');
    text.innerText = "Reconcile Now";
}

// NOTE: configureNode() and restoreNode() moved to PR #3/PR #4 (dataplane + API integration).

function confirmOverwriteImport() {
    return new Promise((resolve) => {
        const existingModal = document.getElementById('import-overwrite-modal');
        if (existingModal) {
            existingModal.remove();
        }

        const overlay = document.createElement('div');
        overlay.id = 'import-overwrite-modal';
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4';

        const panel = document.createElement('div');
        panel.className = 'w-full max-w-md rounded-xl border border-gray-700 bg-gray-900 p-6 shadow-2xl';

        const title = document.createElement('h3');
        title.className = 'text-lg font-bold text-white';
        title.innerText = 'Replace Existing Config?';

        const body = document.createElement('p');
        body.className = 'mt-3 text-sm text-gray-300';
        body.innerText = 'A TunnelSats configuration already exists on this node. Importing will replace the active config.';

        const actions = document.createElement('div');
        actions.className = 'mt-6 flex justify-end gap-3';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'rounded-lg border border-gray-600 px-4 py-2 text-sm font-semibold text-gray-200 hover:bg-gray-800';
        cancelBtn.innerText = 'Cancel';

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'rounded-lg bg-tsyellow px-4 py-2 text-sm font-bold text-black hover:bg-yellow-400';
        confirmBtn.innerText = 'Import Anyway';

        actions.append(cancelBtn, confirmBtn);
        panel.append(title, body, actions);
        overlay.appendChild(panel);
        document.body.appendChild(overlay);

        let settled = false;
        const complete = (choice) => {
            if (settled) return;
            settled = true;
            overlay.remove();
            resolve(choice);
        };

        cancelBtn.addEventListener('click', () => complete(false));
        confirmBtn.addEventListener('click', () => complete(true));
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) {
                complete(false);
            }
        });

        confirmBtn.focus();
    });
}

// 4. Import Config
async function importConfig() {
    const txt = document.getElementById('config-text').value;
    const config = (txt || '').trim();
    const msg = document.getElementById('import-msg');
    const existingConfigs = document.getElementById('txt-configs').innerText;

    function setImportMessage(text, tone) {
        msg.innerText = text;
        if (tone === 'success') {
            msg.className = "text-center mt-4 text-sm font-bold text-tsgreen";
            return;
        }
        if (tone === 'error') {
            msg.className = "text-center mt-4 text-sm font-bold text-red-500";
            return;
        }
        msg.className = "text-center mt-4 text-sm text-gray-400";
    }

    if (existingConfigs !== "None Detected" && existingConfigs !== "Loading..." && existingConfigs !== "") {
        const shouldProceed = await confirmOverwriteImport();
        if (!shouldProceed) {
            setImportMessage("Import cancelled.", 'info');
            return;
        }
    }

    if (!config) {
        setImportMessage("Please paste a WireGuard config before importing.", 'error');
        return;
    }

    if (!/\[Interface\]/i.test(config) || !/\[Peer\]/i.test(config)) {
        setImportMessage("Invalid WireGuard configuration format. Missing [Interface] or [Peer] block.", 'error');
        return;
    }

    if (!/^\s*PrivateKey\s*=\s*.+$/mi.test(config)) {
        setImportMessage("Invalid WireGuard configuration format. Missing Interface PrivateKey.", 'error');
        return;
    }

    setImportMessage("Importing...", 'info');

    try {
        const res = await fetch('/api/local/upload-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });

        const data = await res.json();
        if (res.ok && data.success !== false) {
            setImportMessage(data.message || "Configuration saved and parsed.", 'success');
        } else {
            setImportMessage(data.error || "Import failed.", 'error');
        }
    } catch (e) {
        setImportMessage(e.message, 'error');
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

// Copy Invoice to Clipboard
async function copyInvoice(mode) {
    const input = document.getElementById(`invoice-bolt11-${mode}`);
    if (!input || !input.value) return;
    try {
        await navigator.clipboard.writeText(input.value);
        // Visual feedback: swap icon to checkmark
        const icon = document.getElementById(`copy-icon-${mode}`);
        if (icon) {
            const copyPath = icon.firstElementChild;
            const checkPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            checkPath.setAttribute('stroke-linecap', 'round');
            checkPath.setAttribute('stroke-linejoin', 'round');
            checkPath.setAttribute('stroke-width', '2');
            checkPath.setAttribute('d', 'M5 13l4 4L19 7');
            icon.replaceChild(checkPath, copyPath);
            icon.classList.remove('text-gray-400');
            icon.classList.add('text-tsgreen');
            setTimeout(() => {
                icon.replaceChild(copyPath, checkPath);
                icon.classList.remove('text-tsgreen');
                icon.classList.add('text-gray-400');
            }, 2000);
        }
    } catch (e) {
        // Fallback for older browsers
        input.select();
        document.execCommand('copy');
    }
}

// Custom Dropdown Logic
var openDropdown = null;

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
