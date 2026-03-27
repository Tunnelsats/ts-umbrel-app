// State (var for window-scope testability)
var pollInterval;
var activePaymentHash = null;
var purchaseMode = "buy"; // "buy" or "renew"

// Pricing Configuration
const BASE_PRICE_USD = 3;
const DISCOUNTS = { 1: 0, 3: 0.05, 6: 0.10, 12: 0.20 };
let currentSatsPerDollar = null;
const POLL_INTERVAL_MS = 3000;
let tsServers = [];

// Local Development Mocking
const IS_MOCK_MODE = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

// Override fetch for mocking if enabled, but skip if we are in a Jest test environment
// where global.fetch is already a mock.
const isJest = typeof process !== 'undefined' && process.env && process.env.JEST_WORKER_ID;
if (!isJest) {
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
        if (IS_MOCK_MODE && typeof args[0] === 'string' && (args[0].startsWith('/api/local/') || args[0].startsWith('/api/'))) {
            const responseData = await mockFetch(args[0]);
            const body = JSON.stringify(responseData.body || {});
            return new Response(body, {
                status: responseData.status || 200,
                headers: { 'Content-Type': 'application/json' }
            });
        }
        return originalFetch(...args);
    };
}

async function mockFetch(url) {
    console.log(`[MOCK] Fetching: ${url}`);
    if (url === '/api/local/status') {
        return {
            ok: true,
            body: {
                wg_status: 'Connected',
                wg_pubkey: 'MOCK_PUBKEY_1234567890abcdef',
                configs_found: ['tunnelsats.conf', 'mullvad.conf'],
                version: 'v3.1.0-modern',
                target_container: 'lightning_lnd_1',
                target_ip: '10.0.0.2',
                target_impl: 'lnd',
                vpn_active: true,
                lnd_detected: true,
                cln_detected: false,
                lnd_routing_active: true,
                cln_routing_active: false,
                server_domain: 'au1.tunnelsats.com',
                vpn_port: '39486',
                expires_at: '2027-03-10T12:00:00Z',
                forwarding_port: '35825',
                last_reconcile_at: new Date().toISOString(),
                last_error: null
            }
        };
    }
    if (url === '/api/local/meta') {
        return {
            ok: true,
            body: {
                serverId: 'unknown',
                serverDomain: 'au1.tunnelsats.com',
                wgPublicKey: 'MOCK_WG_PUBKEY_XYZ'
            }
        };
    }
    if (url === '/api/servers' || url === '/api/local/servers') {
        return {
            ok: true,
            body: [
                { id: 'au1', country: 'Australia', city: 'Sydney', flag: '🇦🇺' },
                { id: 'de2', country: 'Germany', city: 'Frankfurt', flag: '🇩🇪' },
                { id: 'fi1', country: 'Finland', city: 'Helsinki', flag: '🇫🇮' }
            ]
        };
    }
    if (url === '/api/subscription/create') {
        return {
            ok: true,
            body: {
                payment_hash: 'mock_hash_12345',
                payment_request: 'lnbc1mockinvoicepaythisnow1234567890abcdefghijklmnopqrstuvwxyz'
            }
        };
    }
    return { ok: false, status: 404, body: { error: 'Not Found' } };
}

function toggleMobileMenu() {
    const nav = document.getElementById('sidebar-nav');
    if (nav) {
        nav.classList.toggle('hidden');
    }
}

function setNodeType(nodeType, fromUser = true) {
    const normalized = nodeType === 'cln' ? 'cln' : 'lnd';
    const hiddenInput = document.getElementById('node-type-selected');
    const lndBtn = document.getElementById('node-type-lnd');
    const clnBtn = document.getElementById('node-type-cln');
    if (!hiddenInput || !lndBtn || !clnBtn) return;

    hiddenInput.value = normalized;
    if (fromUser) hiddenInput.dataset.userSelected = '1';

    function setButtonState(button, isActive) {
        button.classList.toggle('bg-tsyellow', isActive);
        button.classList.toggle('border-tsyellow', isActive);
        button.classList.toggle('text-black', isActive);
        button.classList.toggle('bg-gray-900', !isActive);
        button.classList.toggle('border-gray-700', !isActive);
        button.classList.toggle('text-gray-200', !isActive);
    }

    setButtonState(lndBtn, normalized === 'lnd');
    setButtonState(clnBtn, normalized === 'cln');
}

function setActionMessage(elementId, text, tone) {
    // Always update the target DOM element for legacy support (and testing)
    const el = document.getElementById(elementId);
    if (el) {
        el.textContent = text;
        el.className = 'text-center mt-3 text-sm font-semibold text-gray-400';
        if (tone === 'error') el.classList.add('text-red-500');
        if (tone === 'success') el.classList.add('text-tsgreen');
    }

    // Additionally, show a modern toast for improved UX
    if (tone === 'error' || tone === 'success' || tone === 'info') {
        showToast(text, tone);
    }
}

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    const colors = {
        success: 'border-tsgreen bg-gray-900 text-tsgreen',
        error: 'border-red-500 bg-gray-900 text-red-500',
        info: 'border-blue-400 bg-gray-900 text-white'
    };

    toast.className = `flex items-center space-x-3 px-6 py-4 rounded-xl border-l-4 shadow-2xl transition-all duration-500 transform translate-y-10 opacity-0 pointer-events-auto ${colors[type] || colors.info}`;
    
    // Securely add icon and message
    const iconSvg = type === 'error' 
        ? '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>'
        : '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>';

    toast.insertAdjacentHTML('afterbegin', iconSvg);
    const msgSpan = document.createElement('span');
    msgSpan.className = 'font-bold text-sm text-gray-200';
    msgSpan.textContent = message;
    toast.appendChild(msgSpan);
    
    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => {
        toast.classList.remove('translate-y-10', 'opacity-0');
    });

    // Auto remove
    setTimeout(() => {
        toast.classList.add('translate-y-[-20px]', 'opacity-0');
        setTimeout(() => toast.remove(), 500);
    }, 4000);
}

async function copyToClipboard(text, label) {
    // 1. Try modern Buffer/Clipboard API
    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            showToast(`${label} copied to clipboard!`, 'success');
            return;
        } catch (err) {
            console.warn('Modern clipboard API failed, trying fallback...', err);
        }
    }

    // 2. Fallback: Create temporary textarea for document.execCommand('copy')
    try {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        
        // Ensure textarea is not visible but part of DOM
        textArea.style.position = "fixed";
        textArea.style.left = "-9999px";
        textArea.style.top = "0";
        document.body.appendChild(textArea);
        
        textArea.focus();
        textArea.select();
        
        const successful = document.execCommand('copy');
        document.body.removeChild(textArea);
        
        if (successful) {
            showToast(`${label} copied to clipboard!`, 'success');
        } else {
            throw new Error('execCommand returned false');
        }
    } catch (err) {
        console.error('Clipboard fallback error:', err);
        showToast(`Failed to copy ${label}`, 'error');
    }
}

async function fetchPricing() {
    if (currentSatsPerDollar !== null) {
        renderDurations();
        return; // Already fetched this session
    }

    try {
        const res = await fetch('https://lnbits.tunnelsats.com/api/v1/conversion', {
            method: 'POST',
            headers: {
                'accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                from_: 'usd',
                amount: BASE_PRICE_USD,
                to: 'sat'
            })
        });

        if (res.ok) {
            const data = await res.json();
            if (data && data.sats && data.USD) {
                currentSatsPerDollar = data.sats / data.USD;
            } else if (data && data.sats) {
                currentSatsPerDollar = data.sats / BASE_PRICE_USD;
            }
        }
    } catch(e) {
        console.warn("Could not fetch BTC price from LNBits", e);
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
            btn.textContent = label;
            btn.addEventListener('click', () => selectOption(mode, String(months), label));
            listEl.appendChild(btn);
            
            if (String(months) === currentValue && labelEl) {
                labelEl.textContent = label;
            }
        });
    });
}

// Initialization (idempotent to avoid duplicate listener registration)
let isAppInitialized = false;
function handleScrollToClick(e) {
    const target = e.target.closest('[data-scroll-to]');
    if (!target) return;

    const id = target.getAttribute('data-scroll-to');
    const el = document.getElementById(id);
    if (el) {
        e.preventDefault();
        el.scrollIntoView({ behavior: 'smooth' });
    }
}

function initApp() {
    if (isAppInitialized) return;
    isAppInitialized = true;

    setNodeType('lnd', false);
    fetchStatus();
    fetchServers();

    // Attach spotlight effect
    const spotlight = document.querySelector('.spotlight');
    if (spotlight) {
        spotlight.addEventListener('mousemove', (e) => {
            const rect = spotlight.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            spotlight.style.setProperty('--mouse-x', `${x}px`);
            spotlight.style.setProperty('--mouse-y', `${y}px`);
        });
    }

    // Attach programmatic listeners
    const attachListener = (id, event, fn) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener(event, fn);
    };

    attachListener('btn-mobile-menu', 'click', () => toggleMobileMenu());
    attachListener('nav-dashboard', 'click', () => switchTab('dashboard'));
    attachListener('nav-buy', 'click', () => switchTab('buy'));
    attachListener('nav-renew', 'click', () => switchTab('renew'));
    attachListener('nav-import', 'click', () => switchTab('import'));
    attachListener('nav-uninstall', 'click', () => switchTab('uninstall'));
    attachListener('btn-footer-faq', 'click', () => switchTab('faq'));
    attachListener('buy-server-btn', 'click', () => toggleDropdown('buy-server'));
    attachListener('buy-duration-btn', 'click', () => toggleDropdown('buy-duration'));
    attachListener('renew-duration-btn', 'click', () => toggleDropdown('renew-duration'));
    attachListener('btn-create-buy', 'click', () => createSub('buy'));
    attachListener('btn-create-renew', 'click', () => createSub('renew'));
    attachListener('btn-copy-invoice-buy', 'click', () => copyInvoice('buy'));
    attachListener('btn-dash-enable-routing', 'click', () => switchTab('import'));
    attachListener('btn-dash-disable-routing', 'click', () => switchTab('uninstall'));
    attachListener('btn-copy-invoice-renew', 'click', () => copyInvoice('renew'));
    attachListener('btn-claim-install', 'click', () => claimSubscription('import'));
    attachListener('btn-import-config', 'click', () => importConfig());
    attachListener('node-type-lnd', 'click', () => setNodeType('lnd'));
    attachListener('node-type-cln', 'click', () => setNodeType('cln'));
    attachListener('btn-configure-node', 'click', () => configureNode());
    attachListener('btn-restore-node', 'click', () => restoreNode());
    attachListener('btn-copy-pubkey', 'click', () => {
        const val = document.getElementById('renew-pubkey').value;
        if (val && val !== 'Not found') copyToClipboard(val, 'Public Key');
    });
    attachListener('btn-copy-ip', 'click', () => {
        const val = document.getElementById('renew-ip-suffix').textContent;
        if (val && val !== '.---') copyToClipboard(val.replace('.', ''), 'IP Suffix');
    });

    // Global scroll handler: remove any prior instance before registering.
    if (window.__tsScrollToHandler) {
        document.removeEventListener('click', window.__tsScrollToHandler);
    }
    window.__tsScrollToHandler = handleScrollToClick;
    document.addEventListener('click', window.__tsScrollToHandler);
}

document.addEventListener("DOMContentLoaded", initApp);
if (document.readyState !== 'loading') {
    initApp();
}

// UI Routing
function switchTab(tabId) {
    document.querySelectorAll('main section').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('nav > button').forEach(el => {
        el.classList.remove('nav-active', 'bg-gray-800', 'text-white', 'border-tsgreen');
        el.classList.add('text-gray-400', 'border-transparent');
    });
    const footerFaqBtn = document.getElementById('btn-footer-faq');
    if (footerFaqBtn) {
        footerFaqBtn.classList.remove('text-blue-400');
        footerFaqBtn.classList.add('text-gray-500');
    }

    document.getElementById(`view-${tabId}`).classList.remove('hidden');
    const mainEl = document.querySelector('main');
    if (mainEl) mainEl.scrollTop = 0;
    const btn = document.getElementById(`nav-${tabId}`);
    if (btn) {
        btn.classList.add('nav-active', 'bg-gray-800', 'text-white', 'border-tsgreen');
        btn.classList.remove('text-gray-400', 'border-transparent');
    }
    if (footerFaqBtn && tabId === 'faq') {
        footerFaqBtn.classList.remove('text-gray-500');
        footerFaqBtn.classList.add('text-blue-400');
    }

    // Reset polling and invoice UI when navigating away
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }

    if (tabId === 'dashboard') {
        fetchStatus();
    }
    ['buy', 'renew'].forEach(mode => {
        const box = document.getElementById(`invoice-box-${mode}`);
        const btnCreate = document.getElementById(`btn-create-${mode}`);
        
        if (activePaymentHash && tabId === mode && purchaseMode === mode) {
            // Restore active invoice UI and resume polling
            if (box) box.classList.remove('hidden');
            if (btnCreate) {
                btnCreate.textContent = "Invoice Active...";
                btnCreate.disabled = true;
            }
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
        } else {
            // Hide and reset inactive or completed flows
            if (box) box.classList.add('hidden');
            if (btnCreate) {
                btnCreate.textContent = mode === 'renew' ? "Generate Renewal Invoice" : "Generate Lightning Invoice";
                btnCreate.disabled = false;
            }
        }
    });

    // Fetch pricing on-demand when entering purchase flows
    if (tabId === 'buy' || tabId === 'renew') {
        fetchPricing();
    }

    if (tabId === 'renew') {
        fetch('/api/local/meta').then(r => r.json()).then(data => {
            let lookupId = data.serverId;
            // Handle naming mismatch: /api/local/meta (metadata JSON) uses camelCase.
            const sDomain = data.serverDomain || data.server_domain;
            if (lookupId === 'unknown' && sDomain) {
                lookupId = sDomain;
            }
            let serverStr = lookupId || 'Not found';
            
            if (lookupId && lookupId !== 'unknown' && tsServers) {
                const sId = lookupId.split('.')[0];
                const prefix = sId.replace(/[0-9]/g, '');
                const srv = tsServers.find(s => s.id === prefix || s.id === sId);
                if (srv) {
                    serverStr = `${srv.flag} ${srv.country} — ${srv.city}`;
                }
            }
            document.getElementById('renew-server').value = serverStr;
            // Handle naming mismatch: /api/local/meta (metadata JSON) uses camelCase.
            document.getElementById('renew-pubkey').value = data.wgPublicKey || data.wg_pubkey || 'Not found';
        }).catch(e => {
            console.error("Could not load metadata for renew:", e);
            document.getElementById('renew-server').value = 'Error loading';
            document.getElementById('renew-pubkey').value = 'Error loading';
        });
    }

    // Close sidebar on mobile after navigation
    if (window.innerWidth < 768) {
        const nav = document.getElementById('sidebar-nav');
        if (nav) nav.classList.add('hidden');
    }
}

// 1. Fetch Local Status
async function fetchStatus() {
    try {
        const res = await fetch('/api/local/status');
        const data = await res.json();

        const vpnActive = data.vpn_active === true;
        const lndDetected = data.lnd_detected === true;
        const clnDetected = data.cln_detected === true;
        const lndRouting = data.lnd_routing_active === true;
        const clnRouting = data.cln_routing_active === true;
        
        const hasNode = lndDetected || clnDetected;
        const routingActive = lndRouting || clnRouting;

        // Update Header Badge
        const badge = document.getElementById('statusBadge');
        if (vpnActive && hasNode && routingActive) {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-green-900/50 text-tsgreen border border-green-700";
            badge.textContent = "Protected";
            const pingDot = document.getElementById('ping-tunnel');
            if (pingDot) pingDot.classList.remove('hidden');
            const statusIcon = document.getElementById('icon-tunnel-state') ? document.getElementById('icon-tunnel-state').querySelector('svg') : null;
            if (statusIcon) {
                statusIcon.classList.add('text-tsgreen', 'animate-pulse');
                statusIcon.classList.remove('text-tsyellow', 'text-red-500');
            }
            document.getElementById('txt-wg-status').className = "text-2xl font-mono text-tsgreen font-bold";
            document.getElementById('txt-wg-status').textContent = "Connected";
        } else if (vpnActive) {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-yellow-900/50 text-tsyellow border border-yellow-700";
            badge.textContent = "Connected";
            const pingDot = document.getElementById('ping-tunnel');
            if (pingDot) pingDot.classList.add('hidden');
            const statusIcon = document.getElementById('icon-tunnel-state') ? document.getElementById('icon-tunnel-state').querySelector('svg') : null;
            if (statusIcon) {
                statusIcon.classList.add('text-tsyellow');
                statusIcon.classList.remove('animate-pulse', 'text-tsgreen', 'text-red-500');
            }
            document.getElementById('txt-wg-status').className = "text-2xl font-mono text-tsyellow font-bold";
            document.getElementById('txt-wg-status').textContent = "Connected";
        } else {
            badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-red-900/50 text-red-500 border border-red-700";
            badge.textContent = "Tunnel Down";
            const pingDot = document.getElementById('ping-tunnel');
            if (pingDot) pingDot.classList.add('hidden');
            const statusIcon = document.getElementById('icon-tunnel-state') ? document.getElementById('icon-tunnel-state').querySelector('svg') : null;
            if (statusIcon) {
                statusIcon.classList.add('text-red-500');
                statusIcon.classList.remove('animate-pulse', 'text-tsgreen', 'text-tsyellow');
            }
            document.getElementById('txt-wg-status').className = "text-2xl font-mono text-red-500 font-bold";
            document.getElementById('txt-wg-status').textContent = "Disconnected";
        }
        const pk = data.wg_pubkey || "Not available";
        const boxPubkeyEl = document.getElementById('box-pubkey');
        if (boxPubkeyEl) {
            boxPubkeyEl.replaceChildren(document.createTextNode(pk));
        }

        const boxNodeEl = document.getElementById('box-node');
        if (boxNodeEl) {
            let nodeText = "None";
            if (data.target_impl === "lnd") nodeText = "LND";
            else if (data.target_impl === "cln") nodeText = "Core-Lightning";
            else if (data.lnd_detected) nodeText = "LND (Unconfigured)";
            else if (data.cln_detected) nodeText = "Core-Lightning (Unconfigured)";
            
            boxNodeEl.replaceChildren(document.createTextNode(nodeText));
        }

        const boxServerEl = document.getElementById('box-server');
        if (boxServerEl) {
            boxServerEl.replaceChildren(document.createTextNode(data.server_domain || "Not setup"));
        }

        const boxPortEl = document.getElementById('box-port');
        if (boxPortEl) {
            boxPortEl.replaceChildren(document.createTextNode(data.vpn_port || "Not setup"));
        }

        const boxExpirationEl = document.getElementById('box-expiration');
        if (boxExpirationEl) {
            let expText = data.expires_at ? data.expires_at.split('T')[0] : "Not setup";
            boxExpirationEl.replaceChildren(document.createTextNode(expText));
        }

        // Update Renew IP Suffix
        if (data.vpn_internal_ip) {
            const parts = data.vpn_internal_ip.split('.');
            if (parts.length === 4) {
                const suffix = '.' + parts[3];
                const suffixEl = document.getElementById('renew-ip-suffix');
                if (suffixEl) suffixEl.textContent = suffix;
            }
        }

        // Note: renew-pubkey is populated via /api/local/meta on tab switch instead.

        const configsArr = data.configs_found || [];
        let confs = configsArr.length > 0 ? configsArr.join(", ") : "None Detected";
        const configsEl = document.getElementById('txt-configs');
        if (configsEl) {
            configsEl.replaceChildren(document.createTextNode(confs));
        }

        if (data.version) {
            document.getElementById('app-version').textContent = data.version;
        }

        // Node Routing explicit UI states
        const badgeRouting = document.getElementById('badge-routing');
        const txtRoutingStatus = document.getElementById('txt-routing-status');
        const routingActions = document.getElementById('routing-actions');
        const btnDashEnable = document.getElementById('btn-dash-enable-routing');
        const btnDashDisable = document.getElementById('btn-dash-disable-routing');

        if (routingActions) routingActions.classList.remove('hidden');
        if (btnDashEnable) btnDashEnable.classList.add('hidden');
        if (btnDashDisable) btnDashDisable.classList.add('hidden');

        if (!hasNode) {
            // State 1
            if (badgeRouting) { badgeRouting.textContent = "Not Found"; badgeRouting.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-gray-700 bg-gray-900/50 text-gray-500"; }
            if (txtRoutingStatus) txtRoutingStatus.textContent = "No Nodes Detected";
        } else if (!vpnActive) {
            // State 2
            if (badgeRouting) { badgeRouting.textContent = "Offline"; badgeRouting.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-red-700 bg-red-900/50 text-red-500"; }
            if (txtRoutingStatus) txtRoutingStatus.textContent = "VPN Disconnected";
        } else if (!routingActive) {
            // State 3
            if (badgeRouting) { badgeRouting.textContent = "Unsecured"; badgeRouting.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-yellow-700 bg-yellow-900/50 text-tsyellow"; }
            if (txtRoutingStatus) txtRoutingStatus.textContent = "Routing: Default (Tor/Clearnet)";
            if (btnDashEnable) btnDashEnable.classList.remove('hidden');
        } else {
            // State 4
            if (badgeRouting) { badgeRouting.textContent = "Secured"; badgeRouting.className = "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-green-700 bg-green-900/50 text-tsgreen"; }
            if (txtRoutingStatus) txtRoutingStatus.textContent = "Routing: Secured via Tunnelsats";
            if (btnDashDisable) btnDashDisable.classList.remove('hidden');
        }

        // Update Dashboard Banner
        const bannerTitle = document.getElementById('dashboard-banner-title');
        const bannerText = document.getElementById('dashboard-banner-text');
        const bannerDots = document.getElementById('dashboard-banner-dots');

        if (vpnActive) {
            if (bannerTitle) bannerTitle.textContent = "Network Layer Active";
            if (bannerText) bannerText.textContent = "Secure WireGuard tunneling provided by Tunnelsats. Your Lightning P2P traffic is now encrypted and routed through our private global exit nodes.";
            if (bannerDots) bannerDots.classList.remove('hidden');
        } else {
            if (bannerTitle) bannerTitle.textContent = "Hybrid Lightning Connectivity";
            if (bannerText) bannerText.textContent = "TunnelSats enables privacy-preserving clearnet connectivity for your node. Keep your home IP hidden while benefiting from faster, more reliable Lightning routing.";
            if (bannerDots) bannerDots.classList.add('hidden');
        }

        return data;
    } catch (e) {
        console.error("Failed to fetch status", e);
        return null;
    }
}

// 2. Fetch Servers
async function fetchServers() {
    try {
        const res = await fetch('/api/servers');
        const data = await res.json();
        const servers = Array.isArray(data) ? data : (data.servers || []);
        
        tsServers = servers;

        const selBuyList = document.getElementById('buy-server-list');
        if (selBuyList) {
            selBuyList.replaceChildren(); // Clear skeletons
            servers.forEach(s => {
                let btn = document.createElement('button');
                btn.type = 'button';
                const label = `${s.flag} ${s.country} — ${s.city}`;
                btn.addEventListener('click', () => selectOption('buy-server', s.id, label));
                btn.className = 'w-full text-left px-4 py-3 text-white hover:bg-gray-700 transition-colors border-b border-gray-700/50 hover:pl-6 block';
                btn.textContent = label;
                selBuyList.appendChild(btn);
            });
        }

        if (servers.length > 0) {
            const firstLabel = `${servers[0].flag} ${servers[0].country} — ${servers[0].city}`;
            selectOption('buy-server', servers[0].id, firstLabel);
        }
    } catch (e) {
        console.warn("Failed to fetch servers", e);
    }
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
    const createBtn = document.getElementById(`btn-create-${mode}`);
    const previousPaymentHash = activePaymentHash;
    const previousPurchaseMode = purchaseMode;
    const previousPollInterval = pollInterval;
    const hadActiveInvoiceForModeBeforeCall = Boolean(activePaymentHash && purchaseMode === mode);
    let invoiceCreatedInThisCall = false;
    if (mode === 'buy') {
        serverId = document.getElementById('buy-server-select').value;
        if (!serverId) return;
    }

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
        errEl.textContent = msg;
    }

    const oldErr = document.getElementById(`purchase-error-${mode}`);
    if (oldErr) oldErr.remove();

    createBtn.textContent = "Loading...";
    createBtn.disabled = true;

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
                return;
            }
        }

        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();

        if (res.ok && data.paymentHash && data.invoice) {
            activePaymentHash = data.paymentHash;
            purchaseMode = mode;
            invoiceCreatedInThisCall = true;
            document.getElementById(`invoice-bolt11-${mode}`).value = data.invoice;
            document.getElementById(`pay-link-${mode}`).href = `lightning:${data.invoice}`;

            renderQR(mode, data.invoice);
            document.getElementById(`invoice-box-${mode}`).classList.remove('hidden');

            // Start Polling (clear any existing interval first)
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
        } else {
            const fallbackError = mode === 'renew'
                ? "Unable to create renewal invoice."
                : "Unable to create subscription invoice.";
            displayPurchaseError(data.error || data.message || fallbackError);
        }
    } catch (e) {
        displayPurchaseError("Error creating subscription: " + e.message);
        // If invoice setup fails after receiving data, reset state so retry is possible.
        if (invoiceCreatedInThisCall && purchaseMode === mode) {
            activePaymentHash = previousPaymentHash;
            purchaseMode = previousPurchaseMode;
            invoiceCreatedInThisCall = false;
            // Only clear polling if this call created a new interval.
            if (pollInterval && pollInterval !== previousPollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        }
    } finally {
        const hasActiveInvoice = invoiceCreatedInThisCall || hadActiveInvoiceForModeBeforeCall;
        if (hasActiveInvoice) {
            createBtn.textContent = "Invoice Active...";
            createBtn.disabled = true;
        } else {
            createBtn.textContent = mode === 'renew' ? "Generate Renewal Invoice" : "Generate Lightning Invoice";
            createBtn.disabled = false;
        }
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
            invoiceBox.replaceChildren(); // Clear content

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
                button.addEventListener('click', () => {
                    document.getElementById('pending-install-section').classList.remove('hidden');
                    switchTab('import');
                });

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

                // Trigger UI refresh to show new expiry date
                fetchStatus();

                const button = document.createElement('button');
                button.className = 'mt-4 w-full bg-tsyellow hover:bg-yellow-500 text-black font-bold py-2 px-6 rounded transition shadow-lg';
                button.textContent = 'Return to Dashboard';
                button.addEventListener('click', () => switchTab('dashboard'));

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
        btnInstall.textContent = "Installing...";
    }

    try {
        const res = await fetch('/api/subscription/claim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paymentHash: activePaymentHash, wgPublicKey: "", wgPresharedKey: "", referralCode: null })
        });

        const invoiceBox = document.getElementById(`invoice-box-${mode}`);
        invoiceBox.replaceChildren();
        
        let data;
        try {
            data = await res.json();
        } catch (e) {
            console.warn("Failed to parse JSON response from claim");
            data = { error: "Server returned an invalid response." };
        }

        if (res.ok && data.success !== false && data.status !== "error") {
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
            button.addEventListener('click', async () => {
                const ok = await restartTunnel({
                    maxAttempts: 5,
                    intervalMs: 2000,
                    onConnected: () => {
                        showToast("VPN tunnel is UP! Now configure your Lightning Node below.", "success");
                        const configNodeInput = document.getElementById('node-type-selected');
                        if (configNodeInput && configNodeInput.parentElement) {
                            configNodeInput.parentElement.scrollIntoView({ behavior: 'smooth', block: 'start' });
                        }
                    },
                    onTimeout: () => {
                        showToast("VPN restart requested, but connection verification timed out. Please check the dashboard.", "warning");
                    }
                });
                if (ok) {
                    document.getElementById('pending-install-section').classList.add('hidden');
                    activePaymentHash = null;
                } else {
                    showToast("Restart request failed — please restart manually from the dashboard.", "error");
                }
            });

            if (btnInstall) btnInstall.classList.add('hidden'); // Hide the install button now
            invoiceBox.append(h3, p1, p2, button);
        } else {
            const h3 = document.createElement('h3');
            h3.className = 'text-red-500 font-bold text-center mb-2';
            h3.textContent = 'Provisioning Error';

            const p = document.createElement('p');
            p.className = 'text-sm text-gray-300 text-center';
            p.textContent = data.error || data.message || 'Payment was successful, but config provisioning failed.';

            invoiceBox.append(h3, p);
            if (btnInstall) {
                btnInstall.disabled = false;
                btnInstall.textContent = "Retry Installation";
            }
        }
    } catch (e) {
        if (btnInstall) {
            btnInstall.disabled = false;
            btnInstall.textContent = "Retry Installation";
        }
    }
}

// Removed Dataplane Reconcile Logic
async function confirmRestartModal(nodeType) {
    return new Promise((resolve) => {
        const existingModal = document.getElementById('restart-confirmation-modal');
        if (existingModal) {
            existingModal.remove();
        }

        const overlay = document.createElement('div');
        overlay.id = 'restart-confirmation-modal';
        overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm transition-opacity duration-300';

        const panel = document.createElement('div');
        panel.className = 'w-full max-w-md rounded-2xl border border-gray-700/50 bg-gray-950 p-8 shadow-[0_20px_50px_rgba(0,0,0,0.5)] transform transition-all duration-300 scale-95 opacity-0';
        
        // Modal Content
        const title = document.createElement('h3');
        title.className = 'text-xl font-bold text-white flex items-center gap-3 mb-4';

        const titleIconContainer = document.createElement('div');
        titleIconContainer.className = 'p-2 bg-tsyellow/10 rounded-lg';

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'w-6 h-6 text-tsyellow');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('viewBox', '0 0 24 24');

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('stroke-linecap', 'round');
        path.setAttribute('stroke-linejoin', 'round');
        path.setAttribute('stroke-width', '2');
        path.setAttribute('d', 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z');

        svg.appendChild(path);
        titleIconContainer.appendChild(svg);
        title.append(titleIconContainer, ' Restart Required');

        const body = document.createElement('p');
        body.className = 'text-gray-300 leading-relaxed mb-8';
        body.textContent = `Applying these settings requires a restart of your ${nodeType.toUpperCase()} container. This will cause a brief (10-20s) downtime for your Lightning node while it re-initializes with the new TunnelSats configuration.`;

        const actions = document.createElement('div');
        actions.className = 'flex flex-col sm:flex-row gap-3';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'flex-1 rounded-xl border border-gray-700 px-6 py-3.5 text-sm font-bold text-gray-400 hover:bg-gray-800 hover:text-white transition-all cursor-pointer';
        cancelBtn.textContent = 'No, Cancel';

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'flex-1 rounded-xl bg-gradient-to-r from-tsyellow to-yellow-500 px-6 py-3.5 text-sm font-bold text-black hover:from-yellow-400 hover:to-yellow-300 transition-all shadow-lg hover:shadow-tsyellow/20 cursor-pointer';
        confirmBtn.textContent = 'Yes, Restart Node';

        actions.append(cancelBtn, confirmBtn);
        panel.append(title, body, actions);
        overlay.appendChild(panel);
        document.body.appendChild(overlay);

        // Animate in
        setTimeout(() => {
            panel.classList.remove('scale-95', 'opacity-0');
            panel.classList.add('scale-100', 'opacity-100');
        }, 10);

        let settled = false;
        const complete = (choice) => {
            if (settled) return;
            settled = true;
            
            // Animate out
            panel.classList.add('scale-95', 'opacity-0');
            overlay.classList.add('opacity-0');
            
            setTimeout(() => {
                overlay.remove();
                resolve(choice);
            }, 200);
        };

        cancelBtn.addEventListener('click', () => complete(false));
        confirmBtn.addEventListener('click', () => complete(true));
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) complete(false);
        });

        confirmBtn.focus();
    });
}

async function configureNode() {
    const selectedNodeType = (document.getElementById('node-type-selected') || {}).value || 'lnd';
    
    // Warn user about restart
    const confirmed = await confirmRestartModal(selectedNodeType);
    if (!confirmed) return;

    const btn = document.getElementById('btn-configure-node');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Configuring...';
    }
    setActionMessage('configure-node-msg', 'Applying node configuration...', 'info');

    try {
        const res = await fetch('/api/local/configure-node', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeType: selectedNodeType })
        });
        const data = await res.json();

        if (res.ok && data.success !== false) {
            const location = data.dns && data.port ? `${data.dns}:${data.port}` : 'configured endpoint';
            setActionMessage('configure-node-msg', `Node configured successfully: ${location}`, 'success');
            fetchStatus();
        } else {
            setActionMessage('configure-node-msg', data.error || 'Failed to configure node.', 'error');
        }
    } catch (e) {
        setActionMessage('configure-node-msg', `Failed to configure node: ${e.message}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Configure Node';
        }
    }
}

async function restoreNode() {
    // Warn user about restart
    const confirmed = await confirmRestartModal('Lightning');
    if (!confirmed) return;

    const btn = document.getElementById('btn-restore-node');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Restoring...';
    }
    setActionMessage('restore-node-msg', 'Restoring node configuration...', 'info');

    try {
        const res = await fetch('/api/local/restore-node', { method: 'POST' });
        const data = await res.json();

        if (res.ok) {
            const lndState = data.lnd ? (data.lnd_changed ? 'updated' : 'no changes') : 'config not found';
            const clnState = data.cln ? (data.cln_changed ? 'updated' : 'no changes') : 'config not found';
            setActionMessage('restore-node-msg', `Restore complete. LND: ${lndState}. CLN: ${clnState}.`, 'success');
            fetchStatus();
        } else {
            setActionMessage('restore-node-msg', data.error || 'Failed to restore node configuration.', 'error');
        }
    } catch (e) {
        setActionMessage('restore-node-msg', `Failed to restore node configuration: ${e.message}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Restore Node Networking';
        }
    }
}

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
        title.textContent = 'Replace Existing Config?';

        const body = document.createElement('p');
        body.className = 'mt-3 text-sm text-gray-300';
        body.textContent = 'A TunnelSats configuration already exists on this node. Importing will replace the active config.';

        const actions = document.createElement('div');
        actions.className = 'mt-6 flex justify-end gap-3';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'rounded-lg border border-gray-600 px-4 py-2 text-sm font-semibold text-gray-200 hover:bg-gray-800';
        cancelBtn.textContent = 'Cancel';

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'rounded-lg bg-tsyellow px-4 py-2 text-sm font-bold text-black hover:bg-yellow-400';
        confirmBtn.textContent = 'Import Anyway';

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
    const existingConfigs = document.getElementById('txt-configs').textContent;

    function setImportMessage(text, tone) {
        msg.textContent = text;
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
            setTimeout(() => switchTab('import'), 1500);
        } else {
            setImportMessage(data.error || "Import failed.", 'error');
        }
    } catch (e) {
        setImportMessage(e.message, 'error');
    }
}

async function pollUntilConnected(options = {}) {
    const maxAttempts = Number.isInteger(options.maxAttempts) && options.maxAttempts > 0 ? options.maxAttempts : 5;
    const intervalMs = Number.isInteger(options.intervalMs) && options.intervalMs > 0 ? options.intervalMs : 2000;
    const onConnected = typeof options.onConnected === 'function' ? options.onConnected : null;
    const onTimeout = typeof options.onTimeout === 'function' ? options.onTimeout : null;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        const status = await fetchStatus();
        if (status && status.vpn_active === true) {
            if (onConnected) onConnected(status);
            return true;
        }
        if (attempt < maxAttempts) {
            await new Promise((resolve) => setTimeout(resolve, intervalMs));
        }
    }

    if (onTimeout) onTimeout();
    return false;
}

async function restartTunnel(pollOptions = {}) {
    try {
        const res = await fetch('/api/local/restart', { method: 'POST' });
        if (res.ok) {
            // The container entrypoint will catch the trigger file, and restart `wg-quick`.
            void pollUntilConnected(pollOptions);
        }
        return res.ok;
    } catch (e) {
        return false;
    }
}

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
        labelEl.textContent = label;
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
