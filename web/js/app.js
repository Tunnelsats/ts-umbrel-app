// State (var for window-scope testability)
var pollInterval;
var activePaymentHash = null;
var purchaseMode = "buy"; // "buy", "renew", or "reset"
var latestStatusData = null;
var subscriptionCountdownInterval = null;
var invoiceExpiryInterval = null;
window.secureModeActive = false;

// Pricing Configuration
const BASE_PRICE_USD = 2.89;
const DISCOUNTS = { 1: 0, 3: 0.05, 6: 0.10, 12: 0.20 };
const PLAN_TOTALS_USD = { 1: 2.89, 3: 8.24, 6: 15.61, 12: 27.74 };
const SUBSCRIPTION_RING_CIRCUMFERENCE = 314.16;
const BANDWIDTH_ARC_LENGTH = 182.21;
const NWC_DURATION_USD = {
    match: 2.89,
    '1m': 2.89,
    '3m': 8.24,
    '6m': 15.61,
    '12m': 27.74
};
const REGION_COUNTRY_MAP = {
    germany: 'Europe', finland: 'Europe', switzerland: 'Europe', netherlands: 'Europe',
    france: 'Europe', uk: 'Europe', 'united kingdom': 'Europe', sweden: 'Europe',
    norway: 'Europe', austria: 'Europe', poland: 'Europe', spain: 'Europe',
    italy: 'Europe', iceland: 'Europe', ireland: 'Europe', czechia: 'Europe',
    romania: 'Europe',
    usa: 'North America', 'united states': 'North America', canada: 'North America',
    brazil: 'Latin America', argentina: 'Latin America', chile: 'Latin America',
    mexico: 'Latin America', colombia: 'Latin America',
    japan: 'Asia-Pacific', singapore: 'Asia-Pacific', australia: 'Asia-Pacific',
    'new zealand': 'Asia-Pacific', 'south korea': 'Asia-Pacific', india: 'Asia-Pacific',
    'hong kong': 'Asia-Pacific', taiwan: 'Asia-Pacific',
    'south africa': 'Middle East & Africa', uae: 'Middle East & Africa', israel: 'Middle East & Africa'
};
const REGION_PREFIX_MAP = {
    de: 'Europe', fi: 'Europe', ch: 'Europe', nl: 'Europe', fr: 'Europe',
    uk: 'Europe', gb: 'Europe', se: 'Europe', no: 'Europe', at: 'Europe',
    pl: 'Europe', es: 'Europe', it: 'Europe', is: 'Europe', ie: 'Europe',
    cz: 'Europe', ro: 'Europe', eu: 'Europe',
    us: 'North America', ca: 'North America', na: 'North America',
    br: 'Latin America', ar: 'Latin America', cl: 'Latin America', mx: 'Latin America',
    co: 'Latin America', latam: 'Latin America', sa: 'Latin America',
    jp: 'Asia-Pacific', sg: 'Asia-Pacific', au: 'Asia-Pacific', nz: 'Asia-Pacific',
    kr: 'Asia-Pacific', in: 'Asia-Pacific', hk: 'Asia-Pacific', tw: 'Asia-Pacific', ap: 'Asia-Pacific',
    za: 'Middle East & Africa', ae: 'Middle East & Africa', il: 'Middle East & Africa'
};
let currentSatsPerDollar = null;
const POLL_INTERVAL_MS = 3000;
let tsServers = [];
const MANAGEMENT_CSRF_HEADER = 'X-TunnelSats-CSRF-Token';
const MANAGEMENT_CSRF_REFRESH_HEADER = 'X-TunnelSats-CSRF-Refresh';
class ManagementSession {
    constructor() {
        this.csrfToken = null;
        this.csrfPromise = null;
    }

    async ensureCsrfToken() {
        if (this.csrfToken) return this.csrfToken;
        if (!this.csrfPromise) {
            this.csrfPromise = fetch('/api/local/session', {
                credentials: 'same-origin'
            }).then(async (response) => {
                const payload = await response.json();
                if (!response.ok || typeof payload.csrf_token !== 'string' || !payload.csrf_token) {
                    throw new Error('Unable to initialize the management session.');
                }
                this.csrfToken = payload.csrf_token;
                return this.csrfToken;
            }).finally(() => {
                this.csrfPromise = null;
            });
        }
        return this.csrfPromise;
    }

    async fetch(url, options = {}) {
        const { requireCsrf = false, ...fetchOptions } = options;
        const method = String(fetchOptions.method || 'GET').toUpperCase();
        const requestOptions = {
            ...fetchOptions,
            credentials: 'same-origin'
        };
        const csrfRequired = requireCsrf || !['GET', 'HEAD', 'OPTIONS'].includes(method);
        let csrfToken = null;
        if (csrfRequired) {
            csrfToken = await this.ensureCsrfToken();
            requestOptions.headers = {
                ...(fetchOptions.headers || {}),
                [MANAGEMENT_CSRF_HEADER]: csrfToken
            };
        }
        const response = await fetch(url, requestOptions);
        const refreshRequired = response.status === 403
            && response.headers
            && typeof response.headers.get === 'function'
            && response.headers.get(MANAGEMENT_CSRF_REFRESH_HEADER) === 'required';
        if (!csrfRequired || !refreshRequired) return response;

        // A backend restart rotates its process-scoped token while the dashboard
        // can remain open. Refresh and retry exactly once, and do not discard a
        // token that another concurrent request may already have refreshed.
        if (this.csrfToken === csrfToken) this.csrfToken = null;
        const refreshedToken = await this.ensureCsrfToken();
        requestOptions.headers = {
            ...requestOptions.headers,
            [MANAGEMENT_CSRF_HEADER]: refreshedToken
        };
        return fetch(url, requestOptions);
    }
}

const managementSession = new ManagementSession();

async function managementFetch(url, options = {}) {
    return managementSession.fetch(url, options);
}

window.managementFetch = managementFetch;

// Badge States Constants for Routing Status
const BADGE_STATES = {
    notFound: {
        text: "Not Found",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-gray-700 bg-gray-900/50 text-gray-500"
    },
    offline: {
        text: "Offline",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-red-700 bg-red-900/50 text-red-500"
    },
    unsecured: {
        text: "Unsecured",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-yellow-700 bg-yellow-900/50 text-tsyellow pulse-yellow"
    },
    active: {
        text: "Active",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-tsgreen bg-green-900/50 text-tsgreen pulse-green"
    },
    configVerified: {
        text: "\u24d8 Config Verified",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-tsgreen bg-green-900/50 text-tsgreen pulse-green"
    },
    actionNeeded: {
        text: "Action Needed",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-tsyellow text-tsyellow bg-yellow-950/40"
    },
    verifying: {
        text: "Verifying",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-blue-500 text-blue-300 bg-gray-900/50"
    },
    unknown: {
        text: "Unknown",
        className: "text-[10px] uppercase font-bold px-2 py-0.5 rounded border border-gray-700 bg-gray-900/50 text-gray-500"
    }
};

// 3D Visualization State
let myGlobe = null;
let globeInitAttempts = 0;
let globeInitRetryTimer = null;
const GLOBE_INIT_MAX_ATTEMPTS = 3;
const GLOBE_INIT_RETRY_DELAY_MS = 1000;

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

// Local review of every diagnostics state without a node: open the dashboard on localhost
// with ?mock=secure-wrong-port | live-gossip | verifying | no-subscription | unavailable.
function mockStatusScenario(base) {
    const scenario = new URLSearchParams(window.location.search).get('mock');
    if (scenario === 'unavailable') {
        return { ok: false, status: 503, body: { success: false, error: 'TunnelSats daemon is unavailable.' } };
    }
    const expected = 'us3.tunnelsats.com:23217';
    const lndConf = '/home/umbrel/umbrel/app-data/lightning/data/lnd/lnd.conf';
    const compose = '/home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml';
    const help = (mode, container) => ({
        impl: 'lnd',
        mode,
        config_path: lndConf,
        expected,
        expected_line: `externalhosts=${expected}`,
        verify_command: `sudo docker exec ${container} lncli getinfo`,
        container_lookup_command: "sudo docker ps --format '{{.Names}}' | grep -iE 'lnd|lightning|tunnelsats'",
        config_grep_command: `sudo grep -nE '^[[:space:]]*(externalip|externalhosts|nat)[[:space:]]*=' ${lndConf}`,
        logs_commands: [`sudo docker compose -f ${compose} logs --tail 100 tunnelsats-daemon`, `sudo docker compose -f ${compose} logs --tail 100 tunnelsats-web`]
    });
    const diag = (fields) => ({
        impl: 'lnd', kind: 'real-ip', source: 'config_file', setting: null, value: null, line: null, line_number: null,
        address: null, port_defaulted: false, expected, expected_line: `externalhosts=${expected}`, config_path: lndConf,
        withdraw_command: null, detail: null, ...fields
    });
    const withdraw = (address, container = 'lightning_lnd_1') => `sudo docker exec ${container} lncli peers updatenodeannouncement --address_remove=${address}`;
    const blocked = { ...base, server_domain: 'us3.tunnelsats.com', vpn_port: '23217', rules_synced: false };
    const conflict = 'Conflicting node announcement setting detected in node configuration.';
    const unverified = 'Lightning node gossip announcements have not been verified free of conflicting clearnet addresses.';
    const scenarios = {
        'secure-wrong-port': {
            ...blocked, secure_mode: true, announcement_verification_source: 'config_file',
            last_error: conflict, last_error_code: 'announcement_conflict', announcement_help: help('secure', 'lightning_lnd_1'),
            announcement_diagnostics: [
                diag({ kind: 'wrong-port', setting: 'externalip', value: '178.156.167.202', line: 'externalip=178.156.167.202', line_number: 12, address: '178.156.167.202:9735', port_defaulted: true, withdraw_command: withdraw('178.156.167.202:9735') }),
                diag({ kind: 'auto-discovery', setting: 'nat', value: 'true', line: 'nat=true', line_number: 14 })
            ]
        },
        'live-gossip': {
            ...blocked, secure_mode: false, announcement_verification_source: 'live_gossip',
            last_error: unverified, last_error_code: 'announcement_unverified', announcement_help: help('standard', 'lightning_lnd_1'),
            announcement_diagnostics: [
                diag({ source: 'live_gossip', value: '203.0.113.5:9735', address: '203.0.113.5:9735', withdraw_command: withdraw('203.0.113.5:9735') }),
                diag({ kind: 'missing-endpoint', source: 'live_gossip' })
            ]
        },
        verifying: {
            ...blocked, secure_mode: false, announcement_verification_source: 'live_gossip',
            last_error: unverified, last_error_code: 'announcement_unverified', announcement_help: help('standard', 'lightning_lnd_1'),
            announcement_diagnostics: [diag({ kind: 'verification-pending', source: 'live_gossip', detail: 'lnd_initializing' })]
        },
        'no-subscription': {
            ...base, vpn_active: false, wg_status: 'Disconnected', rules_synced: false, server_domain: '', vpn_port: '',
            last_error: 'No WireGuard config found in /data', last_error_code: 'no_wireguard_config', announcement_diagnostics: []
        }
    };
    return { ok: true, body: scenarios[scenario] || base };
}

async function mockFetch(url) {
    console.log(`[MOCK] Fetching: ${url}`);
    if (url === '/api/local/status') {
        return mockStatusScenario({
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
                rules_synced: true,
                last_error: null
        });
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
    if (url === '/api/local/session') {
        return {
            ok: true,
            body: {
                authenticated: true,
                csrf_token: 'mock-management-csrf-token'
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

function initGlobe() {
    const container = document.getElementById('globe-container');
    if (!container || myGlobe) return;

    // Immediate synchronous guard to prevent race conditions during concurrent bootstrap
    myGlobe = "initializing";
    globeInitAttempts += 1;

    // Purge any existing elements (redundant but safe)
    container.innerHTML = "";

    try {
        const globe = Globe()
            (container)
            .backgroundColor('rgba(0,0,0,0)')
            .showAtmosphere(true)
            .atmosphereColor('#22c55e')
            .atmosphereAltitude(0.1)
            .globeImageUrl('img/globe-day.png')
            .bumpImageUrl('img/globe-topology.png')
            .pointsData([])
            .pointAltitude(0.01)
            .pointColor(() => '#22c55e')
            .pointRadius(0.5)
            .ringsData([])
            .ringColor(() => '#22c55e')
            .ringMaxRadius(5)
            .ringPropagationSpeed(1.5)
            .ringRepeatPeriod(2000)
            .labelsData([])
            .labelLat(d => d.lat - 1)
            .labelLng(d => d.lng)
            .labelText(d => d.label)
            .labelSize(1.2)
            .labelDotRadius(0)
            .labelColor(() => 'rgba(34, 197, 94, 0.7)')
            .labelResolution(3);

        globe.controls().autoRotate = true;
        globe.controls().autoRotateSpeed = 0.5;
        globe.controls().enableZoom = false;

        if (typeof globe.width === 'function' && container.offsetWidth) {
            globe.width(container.offsetWidth);
        }
        if (typeof globe.height === 'function' && container.offsetHeight) {
            globe.height(container.offsetHeight);
        }

        myGlobe = globe;
        globeInitAttempts = 0;
        if (globeInitRetryTimer) {
            clearTimeout(globeInitRetryTimer);
            globeInitRetryTimer = null;
        }

        // Resize handler
        window.addEventListener('resize', () => {
            if (myGlobe && typeof myGlobe !== 'string') {
                myGlobe.width(container.offsetWidth);
                myGlobe.height(container.offsetHeight);
            }
        });

        // Optimization: Pause when tab hidden
        document.addEventListener('visibilitychange', () => {
            if (myGlobe && typeof myGlobe !== 'string') {
                myGlobe.controls().autoRotate = document.visibilityState === 'visible';
            }
        });
    } catch (e) {
        console.error("Globe initialization failed:", e);
        myGlobe = null;
        if (!globeInitRetryTimer && globeInitAttempts < GLOBE_INIT_MAX_ATTEMPTS) {
            globeInitRetryTimer = setTimeout(() => {
                globeInitRetryTimer = null;
                initGlobe();
            }, GLOBE_INIT_RETRY_DELAY_MS);
        }
    }
}

function updateGlobeMarker(forcedCoords = null) {
    const coords = forcedCoords;
    const captionEl = document.getElementById('globe-selected-caption');
    if (captionEl) {
        if (coords && (coords.label || coords.city || coords.country)) {
            const parts = [coords.flag, coords.country, coords.city].filter(Boolean);
            captionEl.textContent = coords.label || parts.join(' - ');
        } else {
            captionEl.textContent = 'Global Network';
        }
    }

    if (!myGlobe || typeof myGlobe === 'string') return;

    if (coords && coords.lat != null && coords.lng != null) {
        // Update Core Point
        myGlobe.pointsData([{
            lat: coords.lat,
            lng: coords.lng,
            label: coords.label
        }]);

        // Update Pulsing Rings
        myGlobe.ringsData([{
            lat: coords.lat,
            lng: coords.lng
        }]);

        // Update Labels
        myGlobe.labelsData([{
            lat: coords.lat,
            lng: coords.lng,
            label: coords.label
        }]);

        // Smooth camera transitions to the new location
        myGlobe.pointOfView({
            lat: coords.lat,
            lng: coords.lng,
            altitude: 2.2
        }, 2000);
    } else {
        myGlobe.pointsData([]);
        myGlobe.ringsData([]);
        myGlobe.labelsData([]);
    }
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
    } catch (e) {
        console.warn("Could not fetch BTC price from LNBits", e);
    }
    renderDurations();
}

function calculatePrice(months) {
    const discount = DISCOUNTS[months] || 0;
    const amountUsd = PLAN_TOTALS_USD[months] !== undefined
        ? PLAN_TOTALS_USD[months]
        : Number((BASE_PRICE_USD * months * (1 - discount)).toFixed(2));
    const perMonthUsd = Number((amountUsd / months).toFixed(2));

    let satsStr = "";
    let amountSats = null;
    if (currentSatsPerDollar) {
        amountSats = Math.floor(amountUsd * currentSatsPerDollar);
        satsStr = ` (~${amountSats.toLocaleString()} sats)`;
    }

    return { amountUsd, perMonthUsd, amountSats, satsStr, discount };
}

function attachRadiogroupKeyboardNav(container) {
    if (!container || container.dataset.kbNavAttached === '1') return;
    container.dataset.kbNavAttached = '1';
    container.addEventListener('keydown', (e) => {
        if (!['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key)) return;
        const buttons = Array.from(container.querySelectorAll('button[role="radio"]:not(:disabled)'));
        if (buttons.length === 0) return;
        const currentIndex = buttons.indexOf(document.activeElement);
        if (currentIndex === -1) return;
        e.preventDefault();
        const delta = (e.key === 'ArrowRight' || e.key === 'ArrowDown') ? 1 : -1;
        const nextIndex = (currentIndex + delta + buttons.length) % buttons.length;
        const nextBtn = buttons[nextIndex];
        if (nextBtn) {
            nextBtn.focus();
            nextBtn.click();
        }
    });
}

function renderDurations() {
    const durations = [1, 3, 6, 12];
    const lists = ['buy-duration', 'renew-duration'];

    lists.forEach(mode => {
        const listEl = document.getElementById(`${mode}-list`);
        if (!listEl) return;
        listEl.replaceChildren();
        attachRadiogroupKeyboardNav(listEl);

        const currentSelect = document.getElementById(`${mode}-select`);
        const currentValue = currentSelect && currentSelect.value ? currentSelect.value : "1";
        const labelEl = document.getElementById(`${mode}-label`);

        durations.forEach((months) => {
            const { amountUsd, perMonthUsd, amountSats, satsStr, discount } = calculatePrice(months);
            const discountPercent = Math.round(discount * 100);
            const discountBadgeText = discountPercent > 0 ? `-${discountPercent}%` : "";
            const discountStr = discountPercent > 0 ? ` (${discountBadgeText})` : "";
            const monthStr = months === 1 ? "1 Month" : `${months} Months`;
            const label = `${monthStr}${discountStr} - $${amountUsd.toFixed(2)}${satsStr}`;
            const isSelected = String(months) === String(currentValue);

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.setAttribute('role', 'radio');
            btn.setAttribute('aria-checked', isSelected ? 'true' : 'false');
            btn.tabIndex = isSelected ? 0 : -1;
            btn.dataset.optionValue = String(months);
            btn.dataset.duration = String(months);
            btn.className = isSelected
                ? 'duration-pill text-left p-3 rounded-xl border border-tsgreen bg-tsgreen/10 text-white transition-all flex flex-col justify-between gap-1 cursor-pointer shadow-sm'
                : 'duration-pill text-left p-3 rounded-xl border border-gray-700 bg-gray-900/80 text-gray-200 hover:border-tsgreen/60 transition-all flex flex-col justify-between gap-1 cursor-pointer';

            const topRow = document.createElement('div');
            topRow.className = 'flex items-center justify-between gap-1 w-full';
            const titleSpan = document.createElement('span');
            titleSpan.className = 'text-xs font-bold text-white';
            titleSpan.textContent = monthStr;
            topRow.appendChild(titleSpan);

            if (discountBadgeText) {
                const badgeSpan = document.createElement('span');
                badgeSpan.className = 'text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-tsgreen/20 text-tsgreen';
                badgeSpan.textContent = discountBadgeText;
                topRow.appendChild(badgeSpan);
            }

            const priceRow = document.createElement('div');
            priceRow.className = 'text-sm font-mono font-bold text-tsyellow mt-0.5';
            priceRow.textContent = `$${amountUsd.toFixed(2)}`;

            const subRow = document.createElement('div');
            subRow.className = 'text-[10px] font-mono text-gray-400';
            subRow.textContent = amountSats !== null
                ? `$${perMonthUsd.toFixed(2)}/mo · ~${amountSats.toLocaleString()} sats`
                : `$${perMonthUsd.toFixed(2)}/mo`;

            btn.append(topRow, priceRow, subRow);
            btn.addEventListener('click', () => selectOption(mode, String(months), label));
            listEl.appendChild(btn);

            if (isSelected && labelEl) {
                labelEl.textContent = label;
            }
        });
    });

    if (typeof updateNwcRecommendedBudget === 'function') {
        const nwcSel = document.getElementById('nwc-duration-select');
        updateNwcRecommendedBudget(nwcSel ? nwcSel.value : 'match');
    }
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
        const section = el.closest('main section');
        let needsDelay = false;
        if (section && section.classList.contains('hidden')) {
            const tabId = section.id.replace('view-', '');
            if (typeof switchTab === 'function') {
                switchTab(tabId);
                needsDelay = true;
            }
        }
        if (needsDelay) {
            setTimeout(() => {
                el.scrollIntoView({ behavior: 'smooth' });
            }, 100);
        } else {
            el.scrollIntoView({ behavior: 'smooth' });
        }
    }
}

function handleHashRoute() {
    const hash = window.location.hash;
    if (hash && hash.startsWith('#faq-')) {
        const id = hash.substring(1);
        const el = document.getElementById(id);
        if (el) {
            if (typeof switchTab === 'function') {
                switchTab('faq');
            }
            setTimeout(() => {
                el.scrollIntoView({ behavior: 'smooth' });
            }, 100);
        }
    }
}

function initApp() {
    window._lastSecureMode = undefined;
    // Keep globe bootstrap independent from one-time app initialization.
    if (!myGlobe) initGlobe();

    if (isAppInitialized) return;
    isAppInitialized = true;

    setNodeType('lnd', false);
    renderDurations();
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

    window.switchTab = switchTab;

    attachListener('btn-mobile-menu', 'click', () => toggleMobileMenu());
    attachListener('nav-dashboard', 'click', () => switchTab('dashboard'));
    attachListener('nav-backup', 'click', () => switchTab('backup'));
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
    attachListener('btn-copy-invoice-reset', 'click', () => copyInvoice('reset'));
    attachListener('btn-claim-install', 'click', () => claimSubscription('import'));
    attachListener('btn-import-config', 'click', () => importConfig());
    attachListener('node-type-lnd', 'click', () => setNodeType('lnd'));
    attachListener('node-type-cln', 'click', () => setNodeType('cln'));
    attachListener('btn-configure-node', 'click', () => configureNode());
    attachListener('btn-restore-node', 'click', () => restoreNode());
    attachListener('btn-dash-renew', 'click', () => switchTab('renew'));
    attachListener('dash-nwc-pill', 'click', () => openNwcModal());
    attachListener('btn-open-nwc-modal', 'click', () => openNwcModal());
    attachListener('btn-close-nwc-modal', 'click', () => closeNwcModal());
    attachListener('btn-toggle-nwc-uri', 'click', () => toggleNwcUriVisibility());
    attachListener('btn-nwc-connect', 'click', () => connectNwcWallet());
    attachListener('btn-nwc-disconnect', 'click', () => disconnectNwcWallet());
    attachListener('btn-bandwidth-reset', 'click', () => handleBandwidthResetClick());
    attachListener('btn-confirm-bandwidth-reset', 'click', () => confirmBandwidthReset());
    attachListener('btn-cancel-bandwidth-reset', 'click', () => closeResetConfirmModal());
    attachListener('btn-close-reset-invoice', 'click', () => closeResetInvoiceBox());
    attachListener('btn-pay-nwc-buy', 'click', () => payWithNwc('buy'));
    attachListener('btn-pay-nwc-renew', 'click', () => payWithNwc('renew'));
    attachListener('btn-pay-nwc-reset', 'click', () => payWithNwc('reset'));
    attachListener('btn-pay-webln-buy', 'click', () => payWithWebln('buy'));
    attachListener('btn-pay-webln-renew', 'click', () => payWithWebln('renew'));
    attachListener('btn-pay-webln-reset', 'click', () => payWithWebln('reset'));
    attachListener('btn-test-reachability', 'click', () => testReachability());

    const nwcPillsContainer = document.getElementById('nwc-duration-pills');
    if (nwcPillsContainer) {
        attachRadiogroupKeyboardNav(nwcPillsContainer);
        nwcPillsContainer.querySelectorAll('button[data-nwc-duration]').forEach((pill) => {
            pill.addEventListener('click', () => selectNwcDuration(pill.getAttribute('data-nwc-duration')));
        });
    }

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

    // Hash routing for deep-linking
    if (window.__tsHashHandler) {
        window.removeEventListener('hashchange', window.__tsHashHandler);
    }
    window.__tsHashHandler = handleHashRoute;
    window.addEventListener('hashchange', window.__tsHashHandler);
    handleHashRoute();
}

if (document.readyState !== 'loading') {
    initApp();
} else {
    document.addEventListener("DOMContentLoaded", initApp);
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
        if (activePaymentHash && purchaseMode === 'reset') {
            const resetBox = document.getElementById('bandwidth-reset-invoice-box');
            if (resetBox) resetBox.classList.remove('hidden');
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
        }
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
        managementFetch('/api/local/meta').then(r => r.json()).then(data => {
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
                    serverStr = `${srv.flag} ${srv.country} - ${srv.city}`;
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

function applySecureModeUI(isSecureMode) {
    if (window._lastSecureMode === isSecureMode) {
        return;
    }
    window._lastSecureMode = isSecureMode;
    window.secureModeActive = isSecureMode;

    const elements = {
        installDesc: document.getElementById('install-desc'),
        uninstallDesc: document.getElementById('uninstall-desc'),
        faqUninstallSteps: document.getElementById('faq-uninstall-steps'),
        warningBox: document.getElementById('secure-mode-warning-box'),
        uninstallWarningBox: document.getElementById('secure-mode-uninstall-warning-box')
    };

    if (elements.installDesc) {
        elements.installDesc.textContent = isSecureMode
            ? "Choose your node implementation and follow the manual configuration steps. Since TunnelSats is running in Secure Mode, the configuration cannot be applied automatically."
            : "Choose your node implementation and inject the TunnelSats forwarding host. This step applies the WireGuard configuration to your node and restarts the necessary containers so your Lightning node connects through the secure TunnelSats network.";
    }

    if (elements.uninstallDesc) {
        elements.uninstallDesc.textContent = isSecureMode
            ? "To restore your lightning node networking back to default, click the button below to view the manual steps to revert the configuration parameter changes from your config files. Since TunnelSats is running in Secure Mode, the configuration cannot be modified automatically."
            : "Revert TunnelSats node configuration changes before uninstalling.";
    }

    if (elements.faqUninstallSteps) {
        elements.faqUninstallSteps.innerHTML = isSecureMode
            ? `
                <li>Go to the <strong>Uninstall</strong> tab in the TunnelSats App navigation.</li>
                <li>Click the <strong>Restore Node Networking</strong> button.</li>
                <li>Follow the modal instructions to manually comment out or delete the configuration parameters from your lightning node config files.</li>
                <li>Restart your <strong>Lightning Node or Core Lightning</strong> app from the Umbrel dashboard.</li>
            `
            : `
                <li>Go to the <strong>Uninstall</strong> tab in the TunnelSats App navigation.</li>
                <li>Click the <strong>Restore Node Networking</strong> button.</li>
                <li>Wait for the UI to confirm that your node configuration has been restored to its default state without TunnelSats routing.</li>
            `;
    }

    // Declarative UI visibility toggles via data-secure-mode attributes
    document.querySelectorAll('[data-secure-mode="only"]').forEach(el => {
        el.classList.toggle('hidden', !isSecureMode);
    });

    document.querySelectorAll('[data-secure-mode="exclude"]').forEach(el => {
        el.classList.toggle('hidden', isSecureMode);
    });
}

// --- Actionable status diagnostics (issue #170) ---
// The backend classifies every announcement problem (announcement_diagnostics) and gives
// a stable code for every status error (last_error_code). This module turns both into
// plain-language guidance: what is wrong, why it matters, how to fix it without a
// terminal, and how to confirm the fix (Re-check now). SSH commands are optional extras.
// Every value is rendered with textContent; config values never reach innerHTML.
const ANNOUNCEMENT_ERROR_CODES = ['announcement_conflict', 'announcement_unverified'];

const ANNOUNCEMENT_KIND_LABELS = {
    'real-ip': 'Non-TunnelSats address',
    'wrong-port': 'TunnelSats server, wrong port',
    'pinned-ip': 'TunnelSats address pinned by IP',
    'stale-endpoint': 'Old TunnelSats endpoint',
    'auto-discovery': 'Automatic IP discovery enabled',
    'missing-endpoint': 'TunnelSats endpoint not announced',
    'config-unreadable': 'Node config file not readable',
    'verification-pending': 'Waiting for your Lightning node'
};

const STATUS_ERROR_HINTS = {
    target_switch_pending: 'TunnelSats is switching protection to the Lightning node you selected. This usually finishes within a minute.',
    k3s_dns_unprotected: 'External DNS protection for your Lightning pod is not active yet, so traffic stays blocked. Check the TunnelSats pod logs and the k3s README.',
    no_wireguard_config: 'No TunnelSats subscription is installed yet. Buy one or import your WireGuard configuration on the Subscribe / Import tab.',
    no_lightning_node: 'TunnelSats cannot reach a Lightning node. Start the Lightning Node or Core Lightning app on your Umbrel, then re-check.',
    protected_node_not_running: 'Your protected Lightning node is not running. TunnelSats keeps it blocked until it starts. To protect your other Lightning node instead, switch the node type.',
    rules_not_synced: 'TunnelSats has not finished applying its protection rules yet. This can take a moment after a restart. If it does not clear, restart the TunnelSats app from the Umbrel dashboard.',
    dataplane_error: 'The TunnelSats background service reported this problem while protecting your node. Traffic stays blocked until it is resolved. Restart the TunnelSats app from the Umbrel dashboard, then re-check. If it keeps happening, open an issue and include the TunnelSats logs (see the FAQ).',
    daemon_unavailable: 'The TunnelSats background service is not responding, so the dashboard cannot read your protection status. Restart the TunnelSats app from the Umbrel dashboard, then re-check.',
    connection_lost: 'The dashboard could not reach your Umbrel, so it cannot show your protection status. Check your network connection, then click Re-check now or reload this page.',
    session_expired: 'Your Umbrel session may have expired. Reload this page and sign in to Umbrel again.'
};

function createUiElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
}

const LIGHTNING_IMPL_NAMES = { lnd: 'LND', cln: 'Core Lightning' };

function knownLightningImpl(status) {
    const impl = status && typeof status === 'object' ? status.target_impl : null;
    return impl === 'lnd' || impl === 'cln' ? impl : null;
}

function otherLightningImpl(impl) {
    if (impl === 'lnd') return 'cln';
    if (impl === 'cln') return 'lnd';
    return null;
}

// Hints that name the protected node are built from the status payload.
function statusHintText(code, status) {
    const impl = knownLightningImpl(status);
    if (code === 'protected_node_not_running' && impl) {
        const otherName = LIGHTNING_IMPL_NAMES[otherLightningImpl(impl)];
        return `Your protected node (${LIGHTNING_IMPL_NAMES[impl]}) is not running. TunnelSats keeps it blocked until it starts. To protect ${otherName} instead, switch the node type.`;
    }
    return STATUS_ERROR_HINTS[code] || STATUS_ERROR_HINTS.dataplane_error;
}

function createCopyButton(value, label) {
    const button = createUiElement('button', 'ts-copy-btn', 'Copy');
    button.type = 'button';
    button.setAttribute('aria-label', `Copy ${label}`);
    button.addEventListener('click', () => copyToClipboard(String(value), label));
    return button;
}

// Rich text parts: strings, { code, copy? } for inline code, { strong } for emphasis, or nested arrays.
function appendRichText(parent, parts) {
    (Array.isArray(parts) ? parts : [parts]).forEach((part) => {
        if (part === null || part === undefined || part === '') return;
        if (Array.isArray(part)) {
            appendRichText(parent, part);
        } else if (typeof part === 'object' && part.code !== undefined) {
            parent.appendChild(createUiElement('code', 'ts-code', part.code));
            if (part.copy) parent.appendChild(createCopyButton(part.code, part.copy));
        } else if (typeof part === 'object' && part.strong !== undefined) {
            parent.appendChild(createUiElement('strong', 'ts-strong', part.strong));
        } else {
            parent.appendChild(document.createTextNode(String(part)));
        }
    });
    return parent;
}

function createCommandBlock(command, label) {
    const wrapper = createUiElement('div', 'ts-command');
    wrapper.appendChild(createUiElement('pre', 'ts-command-text', command));
    wrapper.appendChild(createCopyButton(command, label));
    return wrapper;
}

function splitEndpoint(address) {
    const text = String(address || '');
    const separator = text.lastIndexOf(':');
    if (separator <= 0) return { host: text, port: '' };
    return { host: text.slice(0, separator), port: text.slice(separator + 1) };
}

function diagnosticLocation(diag) {
    if (diag.source === 'live_gossip') return 'Live gossip';
    const fileName = diag.impl === 'cln' ? 'config' : 'lnd.conf';
    return diag.line_number ? `${fileName}, line ${diag.line_number}` : fileName;
}

function announcementGuidance(diag, ctx) {
    const impl = diag.impl === 'cln' ? 'cln' : 'lnd';
    const appName = impl === 'cln' ? 'Core Lightning' : 'Lightning Node';
    const nodeName = impl === 'cln' ? 'Core Lightning' : 'LND';
    const fileName = impl === 'cln' ? 'config' : 'lnd.conf';
    const line = diag.line || '';
    const lineRef = diag.line_number ? `line ${diag.line_number}` : 'the line';
    const expected = diag.expected || '';
    const expectedLine = diag.expected_line || '';
    const address = diag.address || '';
    const live = diag.source === 'live_gossip';
    // The offending line is rendered in its own box above the summary, so refer to it.
    const shown = line ? 'This line' : { code: diag.value || address || fileName };
    const expectedRef = expected ? [' ', { code: expected }] : '';

    const openStep = diag.config_path
        ? ['Open ', { code: diag.config_path, copy: 'Config file path' }, ctx.isK3s ? '.' : ' in the Umbrel Files app or over SSH.']
        : ['Open your ', { code: fileName }, ' file.'];
    const restartStep = ctx.isK3s
        ? ['Save the file and restart the ', nodeName, ' pod so it reloads the configuration.']
        : ['Save the file and restart the ', { strong: appName }, ' app from the Umbrel dashboard.'];
    const recheckStep = ['Come back here and click ', { strong: 'Re-check now' }, '.'];
    const disableStep = line
        ? ['Disable ', lineRef, ' by putting ', { code: '#' }, ' in front of it: ', { code: `# ${line}` }]
        : null;
    const ensureStep = expectedLine
        ? ['Make sure this line exists exactly once', impl === 'lnd' ? ' (in the [Application Options] section)' : '', ': ', { code: expectedLine, copy: 'Configuration line' }]
        : null;
    const configFix = (needsExpectedLine) => [openStep, disableStep, needsExpectedLine ? ensureStep : null, restartStep, recheckStep].filter(Boolean);
    const restartNodeStep = ctx.isK3s
        ? ['Restart the ', nodeName, ' pod.']
        : ['Restart the ', { strong: appName }, ' app from the Umbrel dashboard.'];
    // Only LND's live check withdraws and adds addresses. Core Lightning reads its
    // announcement from the config file at startup, so it needs a fixed file and a restart.
    const liveRetrySteps = impl === 'cln'
        ? [
            ['Core Lightning only changes what it announces when it restarts. Make sure ', { code: fileName }, ' no longer announces this address (use ', { strong: 'Fix automatically' }, ' or edit the file).'],
            restartNodeStep,
            recheckStep
        ]
        : [
            ['Click ', { strong: 'Re-check now' }, '. Every check retries the automatic withdrawal.'],
            ['If it keeps coming back, the address is still set in ', { code: fileName }, '. Use ', { strong: 'Fix automatically' }, ' or remove the matching line, then re-check.']
        ];
    const ssh = diag.withdraw_command
        ? {
            intro: live
                ? ['Optional, over SSH: withdraw it manually. Pass only ip:port, without the pubkey@ prefix.']
                : ['Optional, over SSH: if ', { code: 'getinfo' }, ' still lists ', { code: address }, ' after the restart, withdraw it from live gossip. Pass only ip:port, without the pubkey@ prefix.'],
            command: diag.withdraw_command
        }
        : null;
    const guidance = { title: ANNOUNCEMENT_KIND_LABELS[diag.kind] || 'Announcement issue', summary: [], steps: [], ssh };

    switch (diag.kind) {
        case 'wrong-port': {
            const announced = splitEndpoint(address);
            const forwarded = splitEndpoint(expected).port;
            guidance.summary = [
                live ? [nodeName, ' still announces ', { code: address }, ' in live gossip. '] : [shown, ' announces ', { code: address }, '. '],
                { code: announced.host }, ' is your TunnelSats server, but port ', { code: announced.port },
                diag.port_defaulted ? ' (the default, because no port is set)' : '',
                ' is not your forwarded port', forwarded ? [' ', { code: forwarded }] : '',
                '. Other nodes cannot reach you on that port.'
            ];
            guidance.steps = live ? liveRetrySteps : configFix(true);
            break;
        }
        case 'real-ip': {
            // Without the server's DNS records TunnelSats cannot tell an IP literal apart from
            // the TunnelSats server itself, so it must not call it a home IP.
            const unresolvedNote = diag.detail === 'server_unresolved'
                ? ' TunnelSats could not look up your TunnelSats server\'s IP addresses right now, so it cannot tell whether this is your server or another address, such as your home IP.'
                : '';
            if (live) {
                guidance.summary = [nodeName, ' still announces ', { code: address || diag.value || '' }, ' in live gossip. That is not your TunnelSats endpoint', expectedRef, '.', unresolvedNote];
            } else if (address && unresolvedNote) {
                guidance.summary = [shown, ' announces ', { code: address }, ', which is not your TunnelSats endpoint', expectedRef, '.', unresolvedNote];
            } else if (address) {
                guidance.summary = [shown, ' announces ', { code: address }, ', which is not your TunnelSats endpoint', expectedRef, '. This is usually your home or ISP IP, or a leftover from an earlier setup, and it can reveal where your node runs.'];
            } else {
                guidance.summary = [shown, ' can publish an address other than your TunnelSats endpoint', expectedRef, '.'];
            }
            // Also ensure the TunnelSats line: disabling the only externalhosts/announce-addr
            // line would otherwise leave the node announcing no TunnelSats endpoint at all.
            guidance.steps = live ? liveRetrySteps : configFix(true);
            break;
        }
        case 'pinned-ip':
            guidance.summary = [shown, ' points at your TunnelSats server by a fixed address. TunnelSats only accepts the hostname form', expectedLine ? [' ', { code: expectedLine }] : '', ', so your announcement keeps working if the server IP changes.'];
            guidance.steps = configFix(true);
            break;
        case 'stale-endpoint':
            guidance.summary = live
                ? [nodeName, ' still announces ', { code: address }, ' in live gossip, an older TunnelSats endpoint that does not match your subscription', expectedRef, '.']
                : [shown, ' announces ', { code: address }, ', a TunnelSats endpoint that does not match your current subscription', expectedRef, '. This often happens after switching servers.'];
            guidance.steps = live ? liveRetrySteps : configFix(true);
            break;
        case 'auto-discovery': {
            const disabledLine = impl === 'cln' ? 'ip-discovery=false' : 'nat=false';
            guidance.summary = impl === 'cln'
                ? [shown, ' lets Core Lightning discover and announce your public IP, which can reveal your home IP.']
                : [shown, ' lets LND discover your public IP through your router (UPnP or NAT-PMP) and announce it, which can reveal your home IP.'];
            // Without a TunnelSats line routing stays inactive, so no separate missing-endpoint
            // card appears; the manual fix must add the line as well as disable discovery.
            guidance.steps = [openStep, ['Change ', lineRef, ' to ', { code: disabledLine, copy: 'Configuration line' }], ensureStep, restartStep, recheckStep].filter(Boolean);
            break;
        }
        case 'missing-endpoint':
            if (diag.detail === 'endpoint_unknown' || !expectedLine) {
                guidance.summary = ['TunnelSats does not know your assigned server and port, so it cannot check what your node announces.'];
                guidance.steps = [['Open the ', { strong: 'Subscribe / Import' }, ' tab and import your WireGuard configuration again, or renew an expired subscription.'], recheckStep];
            } else if (live && impl === 'cln') {
                guidance.summary = [nodeName, ' does not announce ', { code: expected }, '. Core Lightning reads its announcement from ', { code: fileName }, ' when it starts.'];
                guidance.steps = [
                    ['Make sure ', { code: fileName }, ' contains ', { code: expectedLine, copy: 'Configuration line' }, ' exactly once.'],
                    restartNodeStep,
                    recheckStep
                ];
            } else if (live) {
                guidance.summary = [nodeName, ' does not announce ', { code: expected }, ' yet. TunnelSats adds it automatically while it can reach ', nodeName, '.'];
                guidance.steps = [
                    ['Click ', { strong: 'Re-check now' }, ' to retry.'],
                    ['If it stays missing, make sure ', { code: fileName }, ' contains ', { code: expectedLine, copy: 'Configuration line' }, ' and restart the ', { strong: appName }, ' app.']
                ];
            } else {
                guidance.summary = [{ code: fileName }, ' does not contain ', { code: expectedLine }, ', so other nodes cannot find your node through TunnelSats.'];
                guidance.steps = [openStep, ['Add this line exactly once', impl === 'lnd' ? ' in the [Application Options] section' : '', ': ', { code: expectedLine, copy: 'Configuration line' }], restartStep, recheckStep];
            }
            break;
        case 'config-unreadable':
            guidance.summary = diag.detail === 'unreadable'
                ? ['TunnelSats cannot read ', { code: diag.config_path || fileName }, ' (permissions or a disk error).']
                : ['TunnelSats detected ', appName, ' but cannot find its config file', diag.config_path ? [' at ', { code: diag.config_path }] : '', '.'];
            guidance.steps = [
                ['Make sure the ', { strong: appName }, ' app is installed and has started at least once.'],
                ['Restart the ', { strong: appName }, ' app, then the ', { strong: 'TunnelSats' }, ' app.'],
                recheckStep,
                ['Still failing? Open an issue and include the TunnelSats logs (see Check over SSH below).']
            ];
            break;
        case 'verification-pending':
            guidance.summary = String(diag.detail || '').endsWith('_not_found')
                ? ['TunnelSats cannot find the ', nodeName, ' container, so it cannot check your live announcement yet.']
                : [nodeName, impl === 'cln'
                    ? ' is not answering yet. It is usually still starting or syncing.'
                    : ' is not answering yet. It is usually still starting, syncing, or waiting for its wallet to be unlocked.',
                ' TunnelSats keeps traffic blocked until the live check passes.'];
            guidance.steps = [['Open the ', { strong: appName }, impl === 'cln' ? ' app and make sure it is running and synced.' : ' app and make sure it is running, synced, and unlocked.'], recheckStep];
            break;
        default:
            guidance.summary = [shown];
            guidance.steps = [recheckStep];
    }
    return guidance;
}

function renderDiagnosticCard(diag, ctx) {
    const guidance = announcementGuidance(diag, ctx);
    const card = createUiElement('li', 'ts-diag-card');
    card.dataset.kind = String(diag.kind || '');
    card.dataset.source = String(diag.source || '');

    const header = createUiElement('div', 'ts-diag-card-header');
    header.appendChild(createUiElement('span', 'ts-diag-kind', guidance.title));
    const chips = createUiElement('span', 'ts-diag-chips');
    chips.appendChild(createUiElement('span', 'ts-chip', diag.impl === 'cln' ? 'Core Lightning' : 'LND'));
    chips.appendChild(createUiElement('span', 'ts-chip', diagnosticLocation(diag)));
    header.appendChild(chips);
    card.appendChild(header);

    if (diag.line) card.appendChild(createUiElement('code', 'ts-diag-line', diag.line));
    card.appendChild(appendRichText(createUiElement('p', 'ts-diag-text'), guidance.summary));
    if (guidance.steps.length) {
        card.appendChild(createUiElement('p', 'ts-diag-subtitle', 'How to fix'));
        const steps = createUiElement('ol', 'ts-diag-steps');
        guidance.steps.forEach((step) => steps.appendChild(appendRichText(createUiElement('li'), step)));
        card.appendChild(steps);
    }
    if (guidance.ssh) {
        const ssh = createUiElement('div', 'ts-diag-ssh');
        ssh.appendChild(appendRichText(createUiElement('p', 'ts-diag-meta'), guidance.ssh.intro));
        ssh.appendChild(createCommandBlock(guidance.ssh.command, 'Withdrawal command'));
        card.appendChild(ssh);
    }
    // Both node configs are audited, but the dashboard buttons only act on the protected node.
    const cardImpl = diag.impl === 'cln' ? 'cln' : 'lnd';
    const protectedImpl = ctx.targetImpl === 'cln' ? 'cln' : 'lnd';
    if (cardImpl !== protectedImpl && diag.source === 'config_file') {
        const protectedName = protectedImpl === 'cln' ? 'Core Lightning' : 'LND';
        const cardName = cardImpl === 'cln' ? 'Core Lightning' : 'LND';
        card.dataset.otherNode = 'true';
        card.appendChild(createUiElement('p', 'ts-diag-meta',
            `TunnelSats is protecting ${protectedName}, so its buttons never change ${cardName} settings. Fix this ${cardName} line manually with the steps above.`));
    }
    return card;
}

function createRecheckButton() {
    const button = createUiElement('button', 'ts-btn ts-btn-primary', 'Re-check now');
    button.type = 'button';
    button.id = 'btn-recheck-status';
    return button;
}

function createFaqLink(faqId, text) {
    const link = createUiElement('a', 'ts-link', text);
    link.href = `#${faqId}`;
    link.setAttribute('data-scroll-to', faqId);
    return link;
}

function renderSshHelp(help) {
    const items = [];
    if (help.container_lookup_command) items.push(['Find the container names', help.container_lookup_command]);
    if (help.config_grep_command) items.push(['Show the active announcement lines in your config', help.config_grep_command]);
    if (help.verify_command) items.push(['Show what your node announces right now (the uris or address list)', help.verify_command]);
    (Array.isArray(help.logs_commands) ? help.logs_commands : []).forEach((command) => items.push(['TunnelSats logs', command]));
    if (!items.length) return null;

    const details = createUiElement('details', 'ts-diag-details');
    details.appendChild(createUiElement('summary', 'ts-diag-summary', 'Check over SSH (optional)'));
    details.appendChild(createUiElement('p', 'ts-diag-meta', 'You do not need SSH to fix this. These read-only commands help if you want to double-check, or to share details with support.'));
    items.forEach(([label, command]) => {
        details.appendChild(createUiElement('p', 'ts-diag-meta', label));
        details.appendChild(createCommandBlock(command, 'Command'));
    });
    return details;
}

function renderAnnouncementDiagnostics(container, data, diagnostics, ctx) {
    const help = data.announcement_help && typeof data.announcement_help === 'object' ? data.announcement_help : {};
    const panel = createUiElement('div', ctx.onlyPending ? 'ts-diag-panel ts-diag-panel-pending' : 'ts-diag-panel');
    panel.id = 'announcement-diagnostics-panel';

    panel.appendChild(createUiElement('h3', 'ts-diag-title', ctx.onlyPending ? 'Waiting for your Lightning node' : "Fix your node's announcement settings"));
    panel.appendChild(createUiElement('p', 'ts-diag-text', ctx.onlyPending
        ? 'TunnelSats needs your Lightning node to answer before it can confirm that only your TunnelSats endpoint is announced. Traffic stays blocked until then.'
        : "TunnelSats keeps your node's clearnet traffic blocked until your node announces only your TunnelSats endpoint. Each item below shows what was found, why it is a problem, and how to fix it."));

    const expected = help.expected || (diagnostics.find((diag) => diag.expected) || {}).expected;
    if (expected) {
        panel.appendChild(appendRichText(createUiElement('p', 'ts-diag-meta'), ['Expected announcement: ', { code: expected, copy: 'Expected announcement' }]));
    }

    // Fix actions call configure-node, which also selects the protected node. Only offer them
    // for the node TunnelSats protects, so a conflict in the other node's config never
    // silently switches the protected target.
    const protectedImpl = ctx.targetImpl === 'cln' ? 'cln' : 'lnd';
    const protectedName = protectedImpl === 'cln' ? 'Core Lightning' : 'LND';
    const actionable = diagnostics.filter((diag) => diag.kind !== 'verification-pending');
    const affectsProtected = actionable.some((diag) => (diag.impl === 'cln' ? 'cln' : 'lnd') === protectedImpl);
    const affectsOther = actionable.some((diag) => (diag.impl === 'cln' ? 'cln' : 'lnd') !== protectedImpl);

    let modeNote = 'TunnelSats can fix configuration conflicts for you with Fix automatically, or you can follow the steps yourself.';
    if (ctx.onlyPending) {
        modeNote = 'Nothing to change in your configuration right now. Once your node answers, click Re-check now.';
    } else if (affectsOther && !affectsProtected) {
        modeNote = `These settings belong to a Lightning node that TunnelSats is not protecting right now (it protects ${protectedName}). Fix them manually with the steps below, then click Re-check now.`;
    } else if (ctx.isSecureMode) {
        modeNote = 'Secure Mode: TunnelSats can read your node config file but cannot change it or query your node, so you apply the fix and click Re-check now.';
    } else if (ctx.isK3s) {
        modeNote = 'k3s: TunnelSats checks your node config file. Apply the fix where you manage that file, then click Re-check now.';
    }
    panel.appendChild(createUiElement('p', 'ts-diag-meta', modeNote));

    if (diagnostics.length) {
        const list = createUiElement('ol', 'ts-diag-list');
        diagnostics.forEach((diag) => list.appendChild(renderDiagnosticCard(diag, ctx)));
        panel.appendChild(list);
    } else {
        panel.appendChild(createUiElement('p', 'ts-diag-text', String(data.last_error || 'An announcement check did not pass.')));
    }

    const actions = createUiElement('div', 'ts-diag-actions');
    actions.appendChild(createRecheckButton());
    // k3s: the configure flow cannot finish for LND there (live verification is unavailable),
    // so k3s users get the manual steps and the k3s note instead of an automatic action.
    if (!ctx.onlyPending && !ctx.isK3s && (affectsProtected || diagnostics.length === 0)) {
        const baseLabel = ctx.isSecureMode ? 'Show step-by-step fix' : 'Fix automatically';
        const fixButton = createUiElement('button', 'ts-btn ts-btn-secondary', affectsOther ? `${baseLabel} (${protectedName})` : baseLabel);
        fixButton.type = 'button';
        fixButton.id = 'btn-goto-instructions';
        fixButton.dataset.impl = protectedImpl;
        actions.appendChild(fixButton);
    }
    actions.appendChild(createFaqLink('faq-8', 'Troubleshooting guide'));
    panel.appendChild(actions);

    const sshHelp = renderSshHelp(help);
    if (sshHelp) panel.appendChild(sshHelp);

    container.replaceChildren(panel);
    container.classList.remove('hidden');
}

function renderStatusHelp(container, code, status) {
    const panel = createUiElement('div', 'ts-diag-panel ts-diag-panel-generic');
    panel.id = 'status-help-panel';
    panel.dataset.code = String(code || '');
    panel.appendChild(createUiElement('p', 'ts-diag-text', statusHintText(code, status)));
    const actions = createUiElement('div', 'ts-diag-actions');
    if (code === 'session_expired') {
        const reload = createUiElement('button', 'ts-btn ts-btn-primary', 'Reload page');
        reload.type = 'button';
        reload.id = 'btn-reload-page';
        actions.appendChild(reload);
    } else {
        actions.appendChild(createRecheckButton());
    }
    if (code === 'no_wireguard_config') {
        const subscribe = createUiElement('button', 'ts-btn ts-btn-secondary', 'Open Subscribe / Import');
        subscribe.type = 'button';
        subscribe.id = 'btn-open-subscribe';
        actions.appendChild(subscribe);
    }
    if (code === 'protected_node_not_running') {
        // Opens Configure Lightning Node with the other implementation preselected.
        // Nothing changes until the user confirms there with Configure Node.
        const switchNode = createUiElement('button', 'ts-btn ts-btn-secondary', 'Switch node type');
        switchNode.type = 'button';
        switchNode.id = 'btn-switch-node-type';
        const otherImpl = otherLightningImpl(knownLightningImpl(status));
        if (otherImpl) switchNode.dataset.impl = otherImpl;
        actions.appendChild(switchNode);
    }
    actions.appendChild(createFaqLink('faq-3', 'How to check status and logs'));
    panel.appendChild(actions);
    container.replaceChildren(panel);
    container.classList.remove('hidden');
}

function clearStatusHelp(container) {
    if (!container) return;
    container.replaceChildren();
    container.classList.add('hidden');
}

const STATUS_UNAVAILABLE_TITLES = {
    session_expired: 'Session Expired',
    connection_lost: 'Connection Problem',
    daemon_unavailable: 'TunnelSats Service Unavailable'
};

function renderStatusUnavailable(res, data) {
    // No response at all is a network problem between the browser and the Umbrel. A redirect
    // (umbrelOS sends expired sessions to its sign-in page) or an HTTP success without a status
    // body (a sign-in page served directly) means the session expired; 401 and 403 say the same.
    const notStatusBody = Boolean(res) && res.ok !== false;
    let code = 'daemon_unavailable';
    if (!res) {
        code = 'connection_lost';
    } else if (res.type === 'opaqueredirect' || res.status === 401 || res.status === 403 || notStatusBody) {
        code = 'session_expired';
    }
    const message = notStatusBody
        ? 'The dashboard received another page instead of the TunnelSats status, usually the Umbrel sign-in page.'
        : ((data && (data.error || data.message)) || 'TunnelSats status is unavailable.');

    const badge = document.getElementById('statusBadge');
    if (badge) {
        badge.className = "px-4 py-2 rounded-full font-bold text-sm bg-gray-800 border border-gray-700 text-gray-400";
        badge.textContent = "Status Unavailable";
    }
    // Clear the green tunnel indicators that an earlier "Protected" render may have left behind.
    const pingDot = document.getElementById('ping-tunnel');
    if (pingDot) pingDot.classList.add('hidden');
    const tunnelIcon = document.querySelector('#icon-tunnel-state svg');
    if (tunnelIcon) tunnelIcon.classList.remove('animate-pulse', 'text-tsgreen', 'text-tsyellow', 'text-red-500');
    const wgStatus = document.getElementById('txt-wg-status');
    if (wgStatus) {
        wgStatus.className = "text-2xl font-mono text-gray-500 font-bold";
        wgStatus.textContent = "Unknown";
    }
    const bannerTitle = document.getElementById('dashboard-banner-title');
    if (bannerTitle) bannerTitle.textContent = 'Status Unavailable';
    const bannerText = document.getElementById('dashboard-banner-text');
    if (bannerText) bannerText.textContent = 'The dashboard could not read the current protection status. See Node Routing Status below.';
    const bannerDots = document.getElementById('dashboard-banner-dots');
    if (bannerDots) bannerDots.classList.add('hidden');
    const badgeRouting = document.getElementById('badge-routing');
    if (badgeRouting) {
        badgeRouting.textContent = BADGE_STATES.unknown.text;
        badgeRouting.className = BADGE_STATES.unknown.className;
        badgeRouting.removeAttribute('title');
    }
    const routingStatus = document.getElementById('txt-routing-status');
    if (routingStatus) routingStatus.textContent = STATUS_UNAVAILABLE_TITLES[code];
    const routingSubtitle = document.getElementById('txt-routing-subtitle');
    if (routingSubtitle) {
        routingSubtitle.textContent = '';
        routingSubtitle.classList.add('hidden');
    }
    const routingError = document.getElementById('txt-routing-error');
    if (routingError) {
        routingError.textContent = message;
        routingError.classList.remove('hidden');
    }
    ['btn-dash-enable-routing', 'btn-dash-disable-routing'].forEach((id) => {
        const button = document.getElementById(id);
        if (button) button.classList.add('hidden');
    });
    const diagnosticsContainer = document.getElementById('routing-diagnostics');
    if (diagnosticsContainer) renderStatusHelp(diagnosticsContainer, code);
}

// Single source of truth for the protection verdict shown by the header badge and Re-check.
function deriveStatusFlags(data) {
    const status = data && typeof data === 'object' ? data : {};
    const vpnActive = status.vpn_active === true;
    const lndDetected = status.lnd_detected === true;
    const clnDetected = status.cln_detected === true;
    const targetImpl = status.target_impl || (clnDetected && !lndDetected ? 'cln' : 'lnd');
    const hasNode = lndDetected || clnDetected;
    const routingActive = targetImpl === 'cln' ? status.cln_routing_active === true : status.lnd_routing_active === true;
    const rulesSynced = status.rules_synced === true;
    const dnsProtected = status.dataplane_mode !== 'k3s' || status.dns_protected !== false;
    const targetSwitchPending = status.target_switch_pending === true;
    const dataplaneError = typeof status.last_error === 'string' && status.last_error.trim() !== ''
        ? status.last_error.trim()
        : (targetSwitchPending
            ? 'Lightning target switch is pending dataplane reconciliation.'
            : (hasNode && (!rulesSynced || !dnsProtected) ? 'Dataplane protection is not fully synced.' : ''));
    const isProtected = vpnActive && hasNode && routingActive && rulesSynced && dnsProtected && !dataplaneError;
    return {
        vpnActive, lndDetected, clnDetected, targetImpl, hasNode, routingActive,
        rulesSynced, dnsProtected, targetSwitchPending, dataplaneError, isProtected
    };
}

async function recheckStatus(button) {
    if (button) {
        button.disabled = true;
        button.textContent = 'Checking...';
    }
    const status = await fetchStatus();
    if (!status) {
        showToast('Could not read the TunnelSats status. See the message on the dashboard.', 'error');
    } else {
        const flags = deriveStatusFlags(status);
        if (flags.isProtected) {
            showToast('All checks passed. Your node routing is protected.', 'success');
        } else if (flags.dataplaneError) {
            const diagnostics = Array.isArray(status.announcement_diagnostics) ? status.announcement_diagnostics : [];
            // "Waiting for your Lightning node" needs no change, so it is not an item to fix.
            const count = diagnostics.filter((diag) => diag && diag.kind !== 'verification-pending').length;
            if (diagnostics.length > 0 && count === 0) {
                showToast('Still verifying: TunnelSats is waiting for your Lightning node to answer.', 'info');
            } else {
                showToast(count > 0 ? `Still blocked: ${count} item${count === 1 ? ' needs' : 's need'} attention.` : 'Still blocked. See the details on the dashboard.', 'error');
            }
        } else {
            showToast('No blocking errors, but node routing is not protected yet. See the dashboard for the next step.', 'info');
        }
    }
    // fetchStatus re-renders the panel; only restore the button if it is still mounted.
    if (button && button.isConnected) {
        button.disabled = false;
        button.textContent = 'Re-check now';
    }
    return status;
}

// 1. Fetch Local Status
async function fetchStatus() {
    try {
        let res;
        try {
            // umbrelOS answers an expired session with a 302 to its sign-in page on another
            // origin (:2000/app-auth), which a followed fetch reports as a network error.
            // Not following redirects lets the dashboard recognise it as an expired session.
            res = await managementFetch('/api/local/status', { redirect: 'manual' });
        } catch (networkError) {
            console.error("Failed to fetch status", networkError);
            // A network failure must not leave the previous verdict (for example "Protected") on screen.
            renderStatusUnavailable(null, { error: 'No response from your Umbrel.' });
            return null;
        }
        if (res && res.type === 'opaqueredirect') {
            renderStatusUnavailable(res, { error: 'Umbrel redirected this request to its sign-in page.' });
            return null;
        }
        const data = await safeFetchJson(res);
        // The status payload never has a `success` field. An error object or a non-object body
        // means this was not a status at all (for example the Umbrel sign-in page, served with
        // HTTP 200 after the session expired), so it must never be rendered as node status.
        const isStatusPayload = Boolean(data) && typeof data === 'object' && !Array.isArray(data) && data.success !== false;
        if ((res && res.ok === false) || !isStatusPayload) {
            renderStatusUnavailable(res, data);
            return null;
        }

        const {
            vpnActive, targetImpl, hasNode, routingActive,
            rulesSynced, targetSwitchPending, dataplaneError, isProtected
        } = deriveStatusFlags(data);
        const verificationSource = data.announcement_verification_source || null;
        const isConfigFileVerification = verificationSource === 'config_file';

        // Update Header Badge
        const badge = document.getElementById('statusBadge');
        if (isProtected) {
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
        const nwcDashLink = document.getElementById('nwc-dashboard-link');
        if (nwcDashLink) {
            nwcDashLink.href = data.wg_pubkey
                ? `https://tunnelsats.com/dashboard?wgPublicKey=${encodeURIComponent(data.wg_pubkey)}&section=link`
                : 'https://tunnelsats.com/dashboard?section=link';
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

        // Update Globe Marker for currently active VPN server
        if (data.server_domain) {
            updateGlobeMarker({
                lat: data.lat,
                lng: data.lng,
                label: data.label
            });
        } else {
            updateGlobeMarker(null);
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
        const configsEl = document.getElementById('txt-configs');
        if (configsEl) {
            if (configsArr.length > 0) {
                configsEl.replaceChildren(...configsArr.map(c => {
                    const span = document.createElement('span');
                    span.className = 'px-2 py-0.5 rounded border border-gray-700 bg-gray-900/50 text-xs font-mono text-gray-400';
                    span.textContent = c;
                    return span;
                }));
            } else {
                configsEl.replaceChildren(document.createTextNode("None Detected"));
            }
        }

        if (data.version) {
            document.getElementById('app-version').textContent = data.version;
        }

        // Update Secure Mode dynamic description text & warning boxes
        const isSecureMode = data.secure_mode === true;
        applySecureModeUI(isSecureMode);

        // Node Routing explicit UI states
        const badgeRouting = document.getElementById('badge-routing');
        const txtRoutingStatus = document.getElementById('txt-routing-status');
        const txtRoutingError = document.getElementById('txt-routing-error');
        const routingDiagnostics = document.getElementById('routing-diagnostics');
        const routingActions = document.getElementById('routing-actions');
        const btnDashEnable = document.getElementById('btn-dash-enable-routing');
        const btnDashDisable = document.getElementById('btn-dash-disable-routing');

        const errorCode = typeof data.last_error_code === 'string' && data.last_error_code ? data.last_error_code : null;
        const diagnostics = Array.isArray(data.announcement_diagnostics)
            ? data.announcement_diagnostics.filter((diag) => diag && typeof diag === 'object')
            : [];
        const isAnnouncementIssue = Boolean(dataplaneError) && ANNOUNCEMENT_ERROR_CODES.includes(errorCode);
        const onlyPending = isAnnouncementIssue && diagnostics.length > 0
            && diagnostics.every((diag) => diag.kind === 'verification-pending');
        let statusCode = errorCode;
        if (!statusCode) {
            if (targetSwitchPending) statusCode = 'target_switch_pending';
            else statusCode = typeof data.last_error === 'string' && data.last_error.trim() ? 'dataplane_error' : 'rules_not_synced';
        }

        if (routingActions) {
            routingActions.classList.remove('hidden');
            routingActions.classList.add('flex');
        }
        if (btnDashEnable) btnDashEnable.classList.add('hidden');
        if (btnDashDisable) btnDashDisable.classList.add('hidden');
        if (txtRoutingError) {
            // Announcement problems are explained by the diagnostics panel instead.
            if (dataplaneError && !isAnnouncementIssue) {
                txtRoutingError.textContent = dataplaneError;
                txtRoutingError.classList.remove('hidden');
            } else {
                txtRoutingError.textContent = '';
                txtRoutingError.classList.add('hidden');
            }
        }
        if (routingDiagnostics) {
            if (isAnnouncementIssue) {
                renderAnnouncementDiagnostics(routingDiagnostics, data, diagnostics, {
                    isSecureMode,
                    // dataplane_mode defaults to docker until the daemon's first state write;
                    // announcement_help.mode comes from K3S_MODE itself.
                    isK3s: data.dataplane_mode === 'k3s' || Boolean(data.announcement_help && data.announcement_help.mode === 'k3s'),
                    onlyPending,
                    targetImpl
                });
            } else if (dataplaneError) {
                renderStatusHelp(routingDiagnostics, statusCode, data);
            } else {
                clearStatusHelp(routingDiagnostics);
            }
        }

        let badgeState;
        if (dataplaneError) {
            if (isAnnouncementIssue) {
                badgeState = onlyPending ? BADGE_STATES.verifying : BADGE_STATES.actionNeeded;
                if (txtRoutingStatus) txtRoutingStatus.textContent = onlyPending ? "Waiting for Lightning Node" : "Dataplane Blocked";
            } else if (statusCode === 'no_wireguard_config') {
                badgeState = BADGE_STATES.notFound;
                if (txtRoutingStatus) txtRoutingStatus.textContent = "No Subscription Installed";
            } else if (statusCode === 'no_lightning_node') {
                badgeState = BADGE_STATES.notFound;
                if (txtRoutingStatus) txtRoutingStatus.textContent = "No Nodes Detected";
            } else if (statusCode === 'protected_node_not_running') {
                badgeState = BADGE_STATES.notFound;
                if (txtRoutingStatus) txtRoutingStatus.textContent = `Waiting for ${LIGHTNING_IMPL_NAMES[knownLightningImpl(data)] || 'Lightning Node'}`;
            } else {
                badgeState = BADGE_STATES.offline;
                if (txtRoutingStatus) txtRoutingStatus.textContent = "Dataplane Blocked";
            }
        } else if (!hasNode) {
            badgeState = BADGE_STATES.notFound;
            if (txtRoutingStatus) txtRoutingStatus.textContent = "No Nodes Detected";
        } else if (!vpnActive) {
            badgeState = BADGE_STATES.offline;
            if (txtRoutingStatus) txtRoutingStatus.textContent = "VPN Disconnected";
        } else if (!routingActive) {
            badgeState = BADGE_STATES.unsecured;
            if (txtRoutingStatus) txtRoutingStatus.textContent = "Hybrid Lightning Connectivity";
            if (btnDashEnable) btnDashEnable.classList.remove('hidden');
        } else {
            if (isConfigFileVerification) {
                badgeState = BADGE_STATES.configVerified;
            } else {
                badgeState = BADGE_STATES.active;
            }
            if (txtRoutingStatus) txtRoutingStatus.textContent = "Node Routing Secured";
            if (btnDashDisable) btnDashDisable.classList.remove('hidden');
        }

        if (badgeRouting && badgeState) {
            badgeRouting.textContent = badgeState.text;
            badgeRouting.className = badgeState.className;
            if (isConfigFileVerification && data.announcement_verified === true && !dataplaneError) {
                const targetImpl = data.target_impl || (data.cln_detected ? 'cln' : 'lnd');
                const configFileName = targetImpl === 'cln' ? 'config.cln' : (targetImpl === 'lnd' ? 'lnd.conf' : 'configuration');
                badgeRouting.title = `Gossip announcement verified via node ${configFileName}`;
            } else {
                badgeRouting.removeAttribute('title');
            }
        }

        // Subtle routing verification subtitle
        const txtRoutingSubtitle = document.getElementById('txt-routing-subtitle');
        if (txtRoutingSubtitle) {
            const showSubtitle = isConfigFileVerification && !dataplaneError && routingActive && rulesSynced;
            if (showSubtitle) {
                txtRoutingSubtitle.textContent = "Verified via node configuration";
                txtRoutingSubtitle.classList.remove('hidden');
            } else {
                txtRoutingSubtitle.textContent = "";
                txtRoutingSubtitle.classList.add('hidden');
            }
        }

        // Update Dashboard Banner
        const bannerTitle = document.getElementById('dashboard-banner-title');
        const bannerText = document.getElementById('dashboard-banner-text');
        const bannerDots = document.getElementById('dashboard-banner-dots');

        if (vpnActive && !dataplaneError) {
            if (bannerTitle) bannerTitle.textContent = "Network Layer Active";
            if (bannerText) bannerText.textContent = "Secure WireGuard tunneling provided by Tunnelsats. Your Lightning P2P traffic is now encrypted and routed through our private global exit nodes.";
            if (bannerDots) {
                bannerDots.classList.remove('hidden');
                bannerDots.classList.add('flex');
            }
        } else {
            if (bannerTitle) bannerTitle.textContent = "Hybrid Lightning Connectivity";
            if (bannerText) bannerText.textContent = "TunnelSats enables privacy-preserving clearnet connectivity for your node. Keep your home IP hidden while benefiting from faster, more reliable Lightning routing.";
            if (bannerDots) bannerDots.classList.add('hidden');
        }

        // Update v3.5.0 Subscription Timeline, Monthly Bandwidth Gauge, NWC State & Notices
        latestStatusData = data;
        if (typeof renderSubscriptionTimeline === 'function') renderSubscriptionTimeline(data);
        if (typeof renderBandwidthGauge === 'function') renderBandwidthGauge(data);
        if (typeof renderNwcStatusUI === 'function') renderNwcStatusUI(data);
        if (typeof renderNotices === 'function') renderNotices(data);

        return data;
    } catch (e) {
        console.error("Failed to fetch status", e);
        return null;
    }
}

// Server Region Classification (v3.5.0)
function getServerRegion(server) {
    if (!server || typeof server !== 'object') return 'Other';
    if (typeof server.region === 'string' && server.region.trim()) {
        return server.region.trim();
    }
    const countryKey = String(server.country || '').trim().toLowerCase();
    if (REGION_COUNTRY_MAP[countryKey]) {
        return REGION_COUNTRY_MAP[countryKey];
    }
    const idPrefix = String(server.id || '').toLowerCase().replace(/[0-9]+.*$/, '');
    if (REGION_PREFIX_MAP[idPrefix]) {
        return REGION_PREFIX_MAP[idPrefix];
    }
    return 'Other';
}
window.getServerRegion = getServerRegion;

// 2. Fetch Servers
async function fetchServers() {
    try {
        const res = await fetch('/api/servers');
        const data = await res.json();
        const servers = Array.isArray(data) ? data : (data.servers || []);

        tsServers = servers;

        const selBuyList = document.getElementById('buy-server-list');
        const firstOnline = servers.find(s => !s.status || s.status === 'online') || servers[0] || null;
        const selectedId = firstOnline ? firstOnline.id : '';

        if (selBuyList) {
            selBuyList.replaceChildren(); // Clear skeletons
            attachRadiogroupKeyboardNav(selBuyList);

            // Group servers by region while preserving discovery order
            const regionGroups = new Map();
            servers.forEach(s => {
                const region = getServerRegion(s);
                if (!regionGroups.has(region)) {
                    regionGroups.set(region, []);
                }
                regionGroups.get(region).push(s);
            });

            regionGroups.forEach((groupServers, regionName) => {
                const groupWrap = document.createElement('div');
                groupWrap.className = 'server-region-group space-y-2';
                groupWrap.dataset.region = regionName;

                const header = document.createElement('div');
                header.className = 'text-[10px] uppercase tracking-wider font-bold text-gray-400 px-1';
                header.textContent = regionName;
                groupWrap.appendChild(header);

                const grid = document.createElement('div');
                grid.className = 'grid grid-cols-1 sm:grid-cols-2 gap-2';

                groupServers.forEach(s => {
                    const status = s.status ? String(s.status).toLowerCase() : 'online';
                    const isOnline = status === 'online';
                    const isSelected = s.id === selectedId && isOnline;
                    const label = `${s.flag || ''} ${s.country || ''} - ${s.city || ''}`.trim();

                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.setAttribute('role', 'radio');
                    btn.setAttribute('aria-checked', isSelected ? 'true' : 'false');
                    btn.tabIndex = isSelected ? 0 : -1;
                    btn.dataset.optionValue = s.id;
                    btn.dataset.serverId = s.id;
                    btn.dataset.region = regionName;

                    if (!isOnline) {
                        btn.disabled = true;
                        btn.setAttribute('aria-disabled', 'true');
                        btn.className = 'server-pill w-full text-left px-3.5 py-2.5 rounded-xl border border-gray-800 bg-gray-950/60 text-gray-500 opacity-60 cursor-not-allowed flex items-center justify-between gap-2';
                    } else {
                        btn.className = isSelected
                            ? 'server-pill w-full text-left px-3.5 py-2.5 rounded-xl border border-tsgreen bg-tsgreen/10 text-white transition-all flex items-center justify-between gap-2 cursor-pointer shadow-sm'
                            : 'server-pill w-full text-left px-3.5 py-2.5 rounded-xl border border-gray-700 bg-gray-900/80 text-white hover:border-tsgreen/60 transition-all flex items-center justify-between gap-2 cursor-pointer';
                        btn.addEventListener('click', () => {
                            selectOption('buy-server', s.id, label);
                            updateGlobeMarker(s);
                        });
                    }

                    const labelSpan = document.createElement('span');
                    labelSpan.className = 'text-xs font-semibold truncate';
                    labelSpan.textContent = label;
                    btn.appendChild(labelSpan);

                    const statusBadge = document.createElement('span');
                    if (isOnline) {
                        statusBadge.className = 'w-2 h-2 rounded-full bg-tsgreen shrink-0';
                        statusBadge.title = 'Online';
                    } else {
                        statusBadge.className = 'text-[10px] uppercase font-mono font-bold px-1.5 py-0.5 rounded border border-gray-700 bg-gray-800 text-gray-400 shrink-0';
                        statusBadge.textContent = status;
                    }
                    btn.appendChild(statusBadge);

                    grid.appendChild(btn);
                });

                groupWrap.appendChild(grid);
                selBuyList.appendChild(groupWrap);
            });
        }

        if (firstOnline) {
            const firstLabel = `${firstOnline.flag || ''} ${firstOnline.country || ''} - ${firstOnline.city || ''}`.trim();
            selectOption('buy-server', firstOnline.id, firstLabel);
            updateGlobeMarker(firstOnline);
        }
    } catch (e) {
        console.warn("Failed to fetch servers", e);
    }
}

// Initialize QRCodes
var qrBuy = null;
var qrRenew = null;
var qrReset = null;

function createPureDomQrSvg(text, size = 192) {
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('role', 'img');

    if (typeof QRCode === 'function') {
        try {
            const tempDiv = document.createElement('div');
            const qrInstance = new QRCode(tempDiv, {
                text: String(text || ''),
                width: size,
                height: size,
                correctLevel: (QRCode.CorrectLevel && QRCode.CorrectLevel.M !== undefined) ? QRCode.CorrectLevel.M : 0
            });
            const qrModel = qrInstance && qrInstance._oQRCode;
            if (qrModel && typeof qrModel.getModuleCount === 'function' && typeof qrModel.isDark === 'function') {
                const count = qrModel.getModuleCount();
                const margin = 2;
                const total = count + margin * 2;
                svg.setAttribute('viewBox', `0 0 ${total} ${total}`);
                svg.setAttribute('aria-label', 'Lightning Invoice QR Code');
                svg.setAttribute('data-qr-mode', 'matrix');

                const bg = document.createElementNS(svgNS, 'rect');
                bg.setAttribute('width', String(total));
                bg.setAttribute('height', String(total));
                bg.setAttribute('fill', '#ffffff');
                svg.appendChild(bg);

                for (let r = 0; r < count; r++) {
                    for (let c = 0; c < count; c++) {
                        if (qrModel.isDark(r, c)) {
                            const cell = document.createElementNS(svgNS, 'rect');
                            cell.setAttribute('x', String(c + margin));
                            cell.setAttribute('y', String(r + margin));
                            cell.setAttribute('width', '1');
                            cell.setAttribute('height', '1');
                            cell.setAttribute('fill', '#000000');
                            svg.appendChild(cell);
                        }
                    }
                }
                return svg;
            }
        } catch (err) {
            // Fall through to unavailable notice if encoding fails
        }
    }

    // Honest fallback notice when QR encoder library is unavailable (never draw a fake un-scannable matrix)
    svg.setAttribute('viewBox', '0 0 160 160');
    svg.setAttribute('aria-label', 'QR code unavailable - copy invoice or use wallet button');
    svg.setAttribute('data-qr-fallback', 'unavailable');

    const bg = document.createElementNS(svgNS, 'rect');
    bg.setAttribute('width', '160');
    bg.setAttribute('height', '160');
    bg.setAttribute('rx', '8');
    bg.setAttribute('fill', '#111827');
    svg.appendChild(bg);

    const line1 = document.createElementNS(svgNS, 'text');
    line1.setAttribute('x', '80');
    line1.setAttribute('y', '74');
    line1.setAttribute('text-anchor', 'middle');
    line1.setAttribute('fill', '#facc15');
    line1.setAttribute('font-size', '12');
    line1.setAttribute('font-weight', 'bold');
    line1.textContent = 'QR Code Unavailable';

    const line2 = document.createElementNS(svgNS, 'text');
    line2.setAttribute('x', '80');
    line2.setAttribute('y', '94');
    line2.setAttribute('text-anchor', 'middle');
    line2.setAttribute('fill', '#9ca3af');
    line2.setAttribute('font-size', '10');
    line2.textContent = 'Use Copy or Wallet button';

    svg.append(line1, line2);
    return svg;
}
window.createPureDomQrSvg = createPureDomQrSvg;

function renderQR(mode, text) {
    const boxId = `qr-placeholder-${mode}`;
    const box = document.getElementById(boxId);
    if (!box) return;

    if (typeof QRCode === 'function') {
        if (mode === 'buy') {
            if (qrBuy) {
                qrBuy.clear();
            } else {
                box.innerHTML = "";
                qrBuy = new QRCode(box, { width: 192, height: 192 });
            }
            qrBuy.makeCode(text);
        } else if (mode === 'renew') {
            if (qrRenew) {
                qrRenew.clear();
            } else {
                box.innerHTML = "";
                qrRenew = new QRCode(box, { width: 192, height: 192 });
            }
            qrRenew.makeCode(text);
        } else {
            if (qrReset) {
                qrReset.clear();
            } else {
                box.innerHTML = "";
                qrReset = new QRCode(box, { width: 160, height: 160 });
            }
            qrReset.makeCode(text);
        }
    } else {
        box.replaceChildren(createPureDomQrSvg(text, mode === 'reset' ? 160 : 192));
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
        const referralInput = document.getElementById('buy-referral-code');
        const referralCode = (mode === 'buy' && referralInput && typeof referralInput.value === 'string' && referralInput.value.trim())
            ? referralInput.value.trim()
            : null;
        let payload = { serverId, duration, referralCode };

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

        const requestFunction = endpoint === '/api/subscription/renew'
            ? managementFetch
            : fetch;
        const res = await requestFunction(endpoint, {
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

            if (typeof updateInvoiceWalletButtons === 'function') {
                updateInvoiceWalletButtons(mode);
            }
            if (typeof startInvoiceExpiryTimer === 'function') {
                startInvoiceExpiryTimer(mode, data.expiresAt || data.expires_at);
            }

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
        const res = await managementFetch(`/api/subscription/${activePaymentHash}`, {
            requireCsrf: true
        });
        const data = await res.json();

        if (data.status === 'paid') {
            clearInterval(pollInterval);
            if (invoiceExpiryInterval) {
                clearInterval(invoiceExpiryInterval);
                invoiceExpiryInterval = null;
            }
            const invoiceBoxId = purchaseMode === 'reset'
                ? 'bandwidth-reset-invoice-box'
                : `invoice-box-${purchaseMode}`;
            const invoiceBox = document.getElementById(invoiceBoxId);
            if (invoiceBox && purchaseMode !== 'reset') {
                invoiceBox.replaceChildren(); // Clear content for buy/renew
            }

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

                if (invoiceBox) invoiceBox.append(celebrationSvg, h3, p, button);
            } else if (purchaseMode === 'reset') {
                activePaymentHash = null;
                const controlsEl = document.getElementById('bandwidth-reset-invoice-controls');
                if (controlsEl) controlsEl.classList.add('hidden');

                let successEl = document.getElementById('bandwidth-reset-success-msg');
                if (!successEl && invoiceBox) {
                    successEl = document.createElement('div');
                    successEl.id = 'bandwidth-reset-success-msg';
                    successEl.className = 'py-2';
                    invoiceBox.appendChild(successEl);
                }
                const h3 = document.createElement('h3');
                h3.className = 'text-tsgreen font-bold text-center text-sm mb-1';
                h3.textContent = 'Bandwidth Quota Reset!';

                const p = document.createElement('p');
                p.className = 'text-xs text-gray-300 text-center';
                p.textContent = 'Your monthly bandwidth counter has been reset to 0 GB.';

                if (successEl) {
                    successEl.replaceChildren(h3, p);
                    successEl.classList.remove('hidden');
                } else if (invoiceBox) {
                    invoiceBox.append(h3, p);
                }
                fetchStatus();
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

                if (invoiceBox) invoiceBox.append(h3, p, button);
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
        const referralInput = document.getElementById('buy-referral-code');
        const referralCode = (referralInput && typeof referralInput.value === 'string' && referralInput.value.trim())
            ? referralInput.value.trim()
            : null;
        const res = await managementFetch('/api/subscription/claim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paymentHash: activePaymentHash, wgPublicKey: "", wgPresharedKey: "", referralCode })
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

        if (res.status === 202 || (data && data.status === "processing")) {
            const p = document.createElement('p');
            p.className = 'text-sm text-tsyellow text-center animate-pulse';
            p.textContent = (data && data.message) || 'Provisioning peer on VPN server...';
            invoiceBox.append(p);
            if (btnInstall) {
                btnInstall.disabled = false;
                btnInstall.textContent = "Retry Installation";
            }
            return;
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
                    showToast("Restart request failed - please restart manually from the dashboard.", "error");
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
let cachedAppShell = null;
function getAppShell() {
    if (!cachedAppShell) {
        cachedAppShell = document.getElementById('app-shell') || document.body;
    }
    return cachedAppShell;
}

async function confirmRestartModal(nodeType) {
    return new Promise((resolve) => {
        const existingModal = document.getElementById('restart-confirmation-modal');
        if (existingModal) {
            existingModal.remove();
        }

        const overlay = document.createElement('div');
        overlay.id = 'restart-confirmation-modal';
        overlay.className = 'absolute inset-0 z-50 flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm transition-opacity duration-300';

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
        // Configuring LND updates lnd.conf and live gossip without restarting it; only Core
        // Lightning configuration and Restore (nodeType "Lightning") restart containers.
        const appliesWithoutRestart = String(nodeType).toLowerCase() === 'lnd';
        title.append(titleIconContainer, appliesWithoutRestart ? ' Apply TunnelSats Settings' : ' Restart Required');

        const body = document.createElement('p');
        body.className = 'text-gray-300 leading-relaxed mb-8';
        body.textContent = appliesWithoutRestart
            ? 'TunnelSats will back up and update your LND announcement settings and refresh its live gossip announcements. LND keeps running; restart it when convenient so it also loads the saved file. Lightning traffic may pause briefly while routing is reconciled.'
            : `Applying these settings requires a restart of your ${nodeType.toUpperCase()} container. This will cause a brief (10-20s) downtime for your Lightning node while it re-initializes with the new TunnelSats configuration.`;

        const actions = document.createElement('div');
        actions.className = 'flex flex-col sm:flex-row gap-3';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'flex-1 rounded-xl border border-gray-700 px-6 py-3.5 text-sm font-bold text-gray-400 hover:bg-gray-800 hover:text-white transition-all cursor-pointer';
        cancelBtn.textContent = 'No, Cancel';

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'flex-1 rounded-xl bg-gradient-to-r from-tsyellow to-yellow-500 px-6 py-3.5 text-sm font-bold text-black hover:from-yellow-400 hover:to-yellow-300 transition-all shadow-lg hover:shadow-tsyellow/20 cursor-pointer';
        confirmBtn.textContent = appliesWithoutRestart ? 'Yes, Apply' : 'Yes, Restart Node';

        actions.append(cancelBtn, confirmBtn);
        panel.append(title, body, actions);
        overlay.appendChild(panel);
        getAppShell().appendChild(overlay);

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

async function confirmAnnouncementChanges(nodeType, conflicts, diagnostics) {
    return new Promise((resolve) => {
        const { overlay, panel, mountAndAnimate } = createModalOverlay('announcement-confirmation-modal');
        let settled = false;
        const complete = (confirmed, retainTorAnnouncements = true) => {
            if (settled) return;
            settled = true;
            overlay.remove();
            resolve({ confirmed, retainTorAnnouncements });
        };

        const title = document.createElement('h3');
        title.className = 'text-xl font-bold text-white mb-4';
        title.textContent = 'Announcement conflict detected';

        const body = document.createElement('p');
        body.className = 'text-sm text-gray-300 leading-relaxed mb-4';
        // configure-node never restarts LND; only Core Lightning is restarted to load its config.
        const afterChange = String(nodeType).toLowerCase() === 'lnd'
            ? 'and refresh its live gossip announcement. LND keeps running.'
            : 'before restarting the node.';
        body.textContent = `TunnelSats found ${nodeType.toUpperCase()} settings that announce something other than your TunnelSats endpoint. Continuing will back up and disable these user-owned settings ${afterChange}`;

        const list = document.createElement('ul');
        list.className = 'list-disc pl-5 space-y-1 text-sm font-mono text-red-300 mb-4 break-all';
        const labelled = (Array.isArray(diagnostics) ? diagnostics : []).filter((diag) => diag && (diag.line || diag.value));
        if (labelled.length > 0) {
            labelled.forEach((diag) => {
                const item = document.createElement('li');
                item.textContent = diag.line || diag.value;
                const label = ANNOUNCEMENT_KIND_LABELS[diag.kind];
                if (label) {
                    const kind = document.createElement('span');
                    kind.className = 'text-gray-400 font-sans';
                    kind.textContent = ` (${label})`;
                    item.appendChild(kind);
                }
                list.appendChild(item);
            });
        } else {
            (Array.isArray(conflicts) ? conflicts : []).forEach((conflict) => {
                const item = document.createElement('li');
                item.textContent = conflict;
                list.appendChild(item);
            });
        }

        const torLabel = document.createElement('label');
        torLabel.className = 'flex items-start gap-3 rounded-lg border border-gray-800 bg-black/30 p-3 text-sm text-gray-300 mb-4 cursor-pointer';
        const torCheckbox = document.createElement('input');
        torCheckbox.type = 'checkbox';
        torCheckbox.checked = true;
        torCheckbox.className = 'mt-1';
        const torText = document.createElement('span');
        torText.textContent = 'Retain existing Tor .onion announcements. Uncheck to disable and back them up too.';
        torLabel.append(torCheckbox, torText);

        const history = document.createElement('p');
        history.className = 'text-xs text-tsyellow leading-relaxed mb-6';
        history.textContent = 'This withdraws addresses from current gossip, but third-party explorers and historical collectors may retain earlier snapshots.';

        const actions = document.createElement('div');
        actions.className = 'flex gap-3';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'flex-1 rounded-xl border border-gray-700 px-4 py-3 text-sm font-bold text-gray-300';
        cancelBtn.textContent = 'Cancel';
        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'flex-1 rounded-xl bg-red-500 px-4 py-3 text-sm font-bold text-white';
        confirmBtn.textContent = 'Back Up & Disable';
        actions.append(cancelBtn, confirmBtn);

        cancelBtn.addEventListener('click', () => complete(false));
        confirmBtn.addEventListener('click', () => complete(true, torCheckbox.checked));
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) complete(false);
        });

        panel.append(title, body, list, torLabel, history, actions);
        mountAndAnimate();
        confirmBtn.focus();
    });
}

function createModalOverlay(id) {
    const existingModal = document.getElementById(id);
    if (existingModal) {
        existingModal.remove();
    }

    const overlay = document.createElement('div');
    overlay.id = id;
    overlay.className = 'fixed inset-0 z-[100] w-screen h-screen flex items-center justify-center bg-black/85 p-4 backdrop-blur-md transition-opacity duration-300';

    const panel = document.createElement('div');
    panel.className = 'w-full max-w-lg rounded-2xl border border-gray-700/50 bg-gray-950 p-6 shadow-[0_20px_50px_rgba(0,0,0,0.5)] transform transition-all duration-300 scale-95 opacity-0 text-left';

    const OVERLAY_TRANSITION_MS = 300;
    const closeModal = () => {
        document.removeEventListener('keydown', handleKeyDown);
        panel.classList.add('scale-95', 'opacity-0');
        overlay.classList.add('opacity-0');
        setTimeout(() => overlay.remove(), OVERLAY_TRANSITION_MS);
    };

    const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
            closeModal();
        }
    };
    document.addEventListener('keydown', handleKeyDown);

    overlay.addEventListener('click', (e) => {
        if (!panel.contains(e.target)) closeModal();
    });

    const mountAndAnimate = () => {
        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        setTimeout(() => {
            panel.classList.remove('scale-95', 'opacity-0');
            panel.classList.add('scale-100', 'opacity-100');
        }, 10);
    };

    return { overlay, panel, closeModal, mountAndAnimate };
}

async function confirmManualConfigureReconcile(nodeType) {
    const selectedNodeType = (nodeType || (document.getElementById('node-type-selected') || {}).value || 'lnd').toLowerCase();
    setActionMessage('configure-node-msg', 'Reconciling TunnelSats routing and network guards...', 'info');
    try {
        const res = await managementFetch('/api/local/configure-node', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                nodeType: selectedNodeType,
                confirmManualConfigure: true
            })
        });
        const data = await safeFetchJson(res);
        if (res.ok && data.success !== false && data.reconciled !== false) {
            const location = data.dns && data.port ? `${data.dns}:${data.port}` : 'configured endpoint';
            setActionMessage('configure-node-msg', `Node configured successfully: ${location}`, 'success');
            fetchStatus();
        } else {
            setActionMessage('configure-node-msg', data.error || 'Failed to reconcile TunnelSats routing.', 'error');
        }
    } catch (e) {
        setActionMessage('configure-node-msg', `Failed to reconcile TunnelSats routing: ${e.message}`, 'error');
    }
}

function showManualConfigModal(nodeType, path, lines, cleanupLines = [], gossipCommand = '', historicalWarning = '', requiresConfirmation = true, diagnostics = [], gossipCommands = []) {
    const { overlay, panel, closeModal, mountAndAnimate } = createModalOverlay('manual-config-modal');

    const title = document.createElement('h3');
    title.className = 'text-xl font-bold text-white flex items-center gap-3 mb-4';
    title.innerHTML = `<span class="text-tsyellow">⚠️</span> Manual Setup Required (Secure Mode)`;

    const desc = document.createElement('p');
    desc.className = 'text-sm text-gray-400 mb-6 leading-relaxed';
    desc.textContent = `To comply with Umbrel's security guidelines, TunnelSats is running in Secure Mode (without root Docker socket access). Please follow these steps to configure your ${nodeType.toUpperCase()} node manually:`;

    const pathLabel = document.createElement('span');
    pathLabel.className = 'block text-xs text-gray-500 uppercase tracking-wider mb-2';
    pathLabel.textContent = 'Target Config File Path';

    const pathValContainer = document.createElement('div');
    pathValContainer.className = 'flex items-center justify-between bg-black/40 border border-gray-800 rounded-xl p-3 font-mono text-sm text-gray-300 mb-6';
    
    const pathText = document.createElement('span');
    pathText.className = 'truncate mr-2';
    pathText.textContent = path;

    const copyPathBtn = document.createElement('button');
    copyPathBtn.className = 'text-tsgreen hover:text-green-400 font-bold text-sm cursor-pointer bg-transparent border-0';
    copyPathBtn.textContent = 'Copy';
    copyPathBtn.addEventListener('click', () => copyToClipboard(path, 'File path'));

    pathValContainer.append(pathText, copyPathBtn);

    const linesLabel = document.createElement('span');
    linesLabel.className = 'block text-xs text-gray-500 uppercase tracking-wider mb-2';
    linesLabel.textContent = 'Configuration Lines to Add/Update';

    const linesContent = lines.join('\n');
    const pre = document.createElement('pre');
    pre.className = 'bg-black/60 border border-gray-800 rounded-xl p-4 font-mono text-sm text-tsgreen overflow-x-auto mb-3';
    pre.textContent = linesContent;

    const copyLinesBtn = document.createElement('button');
    copyLinesBtn.className = 'w-full bg-gray-900 border border-gray-800 hover:border-tsgreen text-sm text-gray-300 font-bold py-2.5 rounded-lg transition-all mb-6 cursor-pointer';
    copyLinesBtn.textContent = 'Copy Configuration Lines';
    copyLinesBtn.addEventListener('click', () => copyToClipboard(linesContent, 'Configuration lines'));

    const privacyBox = document.createElement('div');
    privacyBox.className = 'rounded-xl border border-red-500/40 bg-red-950/20 p-4 mb-6 text-sm text-gray-300 space-y-3';
    const privacyTitle = document.createElement('p');
    privacyTitle.className = 'font-bold text-red-300';
    privacyTitle.textContent = 'Required privacy cleanup';
    privacyBox.appendChild(privacyTitle);

    // The backend audits the user's actual config (read-only), so show the exact lines first.
    const foundLines = (Array.isArray(diagnostics) ? diagnostics : []).filter((diag) => diag && diag.line);
    if (foundLines.length > 0) {
        const foundTitle = document.createElement('p');
        foundTitle.className = 'text-xs text-gray-300';
        foundTitle.textContent = 'Found in your config file. Disable each line by putting # in front of it:';
        const foundList = document.createElement('ul');
        foundList.id = 'manual-config-found-lines';
        foundList.className = 'list-disc pl-5 space-y-1 text-xs break-all';
        foundLines.forEach((diag) => {
            const item = document.createElement('li');
            if (diag.line_number) item.appendChild(document.createTextNode(`Line ${diag.line_number}: `));
            const code = document.createElement('code');
            code.className = 'font-mono text-red-300';
            code.textContent = diag.line;
            item.appendChild(code);
            const label = ANNOUNCEMENT_KIND_LABELS[diag.kind];
            if (label) item.appendChild(document.createTextNode(` (${label})`));
            foundList.appendChild(item);
        });
        privacyBox.append(foundTitle, foundList);
    }
    if (cleanupLines.length > 0) {
        if (foundLines.length > 0) {
            const alsoTitle = document.createElement('p');
            alsoTitle.className = 'text-xs text-gray-400';
            alsoTitle.textContent = 'Also make sure none of these settings are active:';
            privacyBox.appendChild(alsoTitle);
        }
        const cleanupList = document.createElement('ul');
        cleanupList.className = 'list-disc pl-5 space-y-1 font-mono text-xs break-all';
        cleanupLines.forEach((line) => {
            const item = document.createElement('li');
            item.textContent = `Remove/comment or disable: ${line}`;
            cleanupList.appendChild(item);
        });
        privacyBox.appendChild(cleanupList);
    }

    const appendCommand = (commandText) => {
        const command = document.createElement('pre');
        command.className = 'bg-black/60 border border-gray-800 rounded-lg p-3 text-xs text-tsgreen overflow-x-auto whitespace-pre-wrap break-all';
        command.textContent = commandText;
        const copyCommand = document.createElement('button');
        copyCommand.type = 'button';
        copyCommand.className = 'w-full rounded-lg border border-gray-700 py-2 text-xs font-bold text-gray-200';
        copyCommand.textContent = 'Copy gossip-withdrawal command';
        copyCommand.addEventListener('click', () => copyToClipboard(commandText, 'Gossip-withdrawal command'));
        privacyBox.append(command, copyCommand);
    };
    const prefilledCommands = (Array.isArray(gossipCommands) ? gossipCommands : []).filter(Boolean);
    if (prefilledCommands.length > 0) {
        const prefilledTitle = document.createElement('p');
        prefilledTitle.className = 'text-xs text-gray-300';
        prefilledTitle.textContent = 'Optional, over SSH after the restart: withdraw these old addresses from live gossip if they are still listed.';
        privacyBox.appendChild(prefilledTitle);
        prefilledCommands.forEach((commandText) => appendCommand(commandText));
    }
    if (gossipCommand) {
        if (prefilledCommands.length > 0) {
            const otherTitle = document.createElement('p');
            otherTitle.className = 'text-xs text-gray-400';
            otherTitle.textContent = 'For any other old address, replace the placeholder (ip:port only, without the pubkey@ prefix):';
            privacyBox.appendChild(otherTitle);
        }
        appendCommand(gossipCommand);
    }
    if (historicalWarning) {
        const warning = document.createElement('p');
        warning.className = 'text-xs text-tsyellow leading-relaxed';
        warning.textContent = historicalWarning;
        privacyBox.appendChild(warning);
    }

    const instructionsHeader = document.createElement('p');
    instructionsHeader.className = 'font-bold text-sm text-white mb-2';
    instructionsHeader.textContent = 'Instructions:';

    const ol = document.createElement('ol');
    ol.className = 'list-decimal pl-5 space-y-2 text-sm text-gray-300 mb-8 leading-relaxed';
    if (nodeType && nodeType.toLowerCase() === "lnd") {
        ol.innerHTML = `
            <li>Open the Umbrel <b>Files</b> app (or use SSH).</li>
            <li>Navigate to the configuration file path shown above.</li>
            <li>Disable every line listed under <b>Required privacy cleanup</b> by putting <code>#</code> in front of it, and set <code>nat=false</code>. You may retain deliberate <code>.onion</code> announcements.</li>
            <li>Add or update the TunnelSats lines under the existing <b>[Application Options]</b> section (do not create duplicate lines).</li>
            <li>Go back to the Umbrel dashboard and restart your <b>Lightning Node</b> app.</li>
            <li>Optional (SSH): run <code>sudo docker exec lightning_lnd_1 lncli getinfo</code>. If <code>uris</code> still lists an old clearnet address, run the matching withdrawal command above.</li>
            <li>Click <b>Done</b> below after restarting your node to reconcile TunnelSats routing and fail-closed network guards, then use <b>Re-check now</b> on the Dashboard to confirm.</li>
        `;
    } else {
        ol.innerHTML = `
            <li>Open the Umbrel <b>Files</b> app (or use SSH).</li>
            <li>Navigate to the configuration file path shown above.</li>
            <li>Remove/comment non-TunnelSats <code>announce-addr=</code> lines, retain deliberate <code>.onion</code> values if desired, and disable <code>ip-discovery</code>.</li>
            <li>Add or update each TunnelSats configuration line shown above (do not create duplicate lines).</li>
            <li>Go back to the Umbrel dashboard and restart your <b>Core Lightning</b> app.</li>
            <li>Click <b>Done</b> below after restarting your node to reconcile TunnelSats routing and fail-closed network guards.</li>
        `;
    }

    const doneBtn = document.createElement('button');
    doneBtn.type = 'button';
    doneBtn.className = 'w-full rounded-xl bg-gradient-to-r from-tsgreen to-green-500 px-6 py-3.5 text-base font-bold text-black hover:from-green-400 hover:to-green-300 transition-all shadow-lg hover:shadow-tsgreen/20 cursor-pointer';
    doneBtn.textContent = 'Done';
    doneBtn.addEventListener('click', () => {
        closeModal();
        if (requiresConfirmation) {
            confirmManualConfigureReconcile(nodeType);
        }
    });

    panel.append(title, desc, pathLabel, pathValContainer, linesLabel, pre, copyLinesBtn, privacyBox, instructionsHeader, ol, doneBtn);
    mountAndAnimate();
}

async function confirmManualRestoreDecommission() {
    setActionMessage('restore-node-msg', 'Removing fail-closed network guards...', 'info');
    try {
        const res = await managementFetch('/api/local/restore-node', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirmManualRestore: true })
        });
        const data = await safeFetchJson(res);
        if (res.ok && data.success !== false && data.decommissioned !== false) {
            setActionMessage('restore-node-msg', 'Restore complete. Fail-closed network guards removed.', 'success');
            fetchStatus();
        } else {
            setActionMessage('restore-node-msg', data.error || 'Failed to remove fail-closed network guards.', 'error');
        }
    } catch (e) {
        setActionMessage('restore-node-msg', `Failed to remove fail-closed network guards: ${e.message}`, 'error');
    }
}

function showManualRestoreModal(targets, requiresConfirmation = false) {
    const { overlay, panel, closeModal, mountAndAnimate } = createModalOverlay('manual-restore-modal');

    const title = document.createElement('h3');
    title.className = 'text-xl font-bold text-white flex items-center gap-3 mb-4';
    title.innerHTML = `<span class="text-tsyellow">⚠️</span> Manual Restore Required (Secure Mode)`;

    const desc = document.createElement('p');
    desc.className = 'text-sm text-gray-400 mb-6 leading-relaxed';
    desc.textContent = `To restore your lightning node networking back to default, please comment out or delete the following configuration parameters from your config files:`;

    const container = document.createElement('div');
    container.className = 'space-y-4 mb-6';

    targets.forEach(t => {
        const itemDiv = document.createElement('div');
        itemDiv.className = 'border border-gray-800 rounded-xl p-4 bg-black/30';

        const nodeLabel = document.createElement('h4');
        nodeLabel.className = 'text-sm font-bold text-white mb-3 uppercase tracking-wide';
        nodeLabel.textContent = `${t.node_type.toUpperCase()} Node`;

        const pathValContainer = document.createElement('div');
        pathValContainer.className = 'flex items-center justify-between bg-black/40 border border-gray-800 rounded-lg p-2.5 font-mono text-xs text-gray-300 mb-3';
        
        const pathText = document.createElement('span');
        pathText.className = 'truncate mr-2';
        pathText.textContent = t.config_path;

        const copyPathBtn = document.createElement('button');
        copyPathBtn.className = 'text-tsgreen hover:text-green-400 font-bold text-xs cursor-pointer bg-transparent border-0';
        copyPathBtn.textContent = 'Copy';
        copyPathBtn.addEventListener('click', () => copyToClipboard(t.config_path, 'File path'));

        pathValContainer.append(pathText, copyPathBtn);

        const linesLabel = document.createElement('span');
        linesLabel.className = 'block text-xs text-gray-500 uppercase tracking-wider mb-2';
        linesLabel.textContent = 'Lines to delete or comment out';

        const linesList = document.createElement('ul');
        linesList.className = 'list-disc pl-5 text-sm text-gray-300 space-y-1.5';
        t.config_lines.forEach(line => {
            const li = document.createElement('li');
            li.className = 'leading-relaxed text-sm';
            li.appendChild(document.createTextNode('Remove/comment out: '));
            const code = document.createElement('code');
            code.className = 'bg-black/50 border border-gray-800/80 px-1.5 py-0.5 rounded text-tsyellow font-mono text-sm font-bold';
            code.textContent = line;
            li.appendChild(code);
            linesList.appendChild(li);
        });

        const notesList = document.createElement('ul');
        notesList.className = 'list-disc pl-5 mt-3 text-xs text-tsyellow space-y-1';
        (Array.isArray(t.restore_notes) ? t.restore_notes : []).forEach((note) => {
            const li = document.createElement('li');
            li.textContent = note;
            notesList.appendChild(li);
        });

        itemDiv.append(nodeLabel, pathValContainer, linesLabel, linesList, notesList);
        container.appendChild(itemDiv);
    });

    const instructionsHeader = document.createElement('p');
    instructionsHeader.className = 'font-bold text-sm text-white mb-2';
    instructionsHeader.textContent = 'Instructions:';

    const ol = document.createElement('ol');
    ol.className = 'list-decimal pl-5 space-y-2 text-sm text-gray-300 mb-8 leading-relaxed';
    const hasLnd = targets.some(t => t.node_type === 'lnd');
    const hasCln = targets.some(t => t.node_type === 'cln');
    const restartApps = [];
    if (hasLnd) restartApps.push('<b>Lightning Node</b>');
    if (hasCln) restartApps.push('<b>Core Lightning</b>');
    const restartStr = restartApps.length > 0 ? restartApps.join(' and ') : '<b>Lightning Node</b>';
    ol.innerHTML = `
        <li>Open the Umbrel <b>Files</b> app (or use SSH).</li>
        <li>Navigate to the configuration file path(s) shown above.</li>
        <li>Comment out (add <code>#</code> at the start of the line) or delete each configuration line.</li>
        <li>Go back to the Umbrel dashboard and restart your ${restartStr} app${restartApps.length > 1 ? 's' : ''}.</li>
        <li>Click <b>Done</b> below only after your node has been restored and restarted to remove the TunnelSats fail-closed network guards.</li>
    `;

    const actionsRow = document.createElement('div');
    actionsRow.className = 'flex items-center gap-3';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'w-1/3 rounded-xl border border-gray-700 bg-gray-900/70 px-4 py-3.5 text-sm font-semibold text-gray-300 hover:bg-gray-800 transition-all cursor-pointer';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
        setActionMessage('restore-node-msg', 'Manual restore pending; fail-closed network guards remain active.', 'info');
        closeModal();
    });

    const doneBtn = document.createElement('button');
    doneBtn.type = 'button';
    doneBtn.className = 'w-2/3 rounded-xl bg-gradient-to-r from-tsgreen to-green-500 px-6 py-3.5 text-base font-bold text-black hover:from-green-400 hover:to-green-300 transition-all shadow-lg hover:shadow-tsgreen/20 cursor-pointer';
    doneBtn.textContent = 'Done';
    doneBtn.addEventListener('click', () => {
        closeModal();
        if (requiresConfirmation) {
            confirmManualRestoreDecommission();
        }
    });

    actionsRow.append(cancelBtn, doneBtn);
    panel.append(title, desc, container, instructionsHeader, ol, actionsRow);
    mountAndAnimate();
}

async function safeFetchJson(res) {
    const contentType = (res.headers && typeof res.headers.get === 'function' && res.headers.get('content-type')) || '';
    if (contentType.includes('application/json') || (!contentType && typeof res.json === 'function')) {
        try {
            const data = await res.json();
            if (data !== undefined && data !== null) {
                return data;
            }
        } catch (e) {
            // Fall through to text handling if json() throws a SyntaxError (e.g. HTML response)
        }
    }
    let text = '';
    if (typeof res.text === 'function') {
        try {
            text = await res.text();
        } catch (e) {
            text = '';
        }
    }
    const cleanText = text.replace(/<[^>]*>?/gm, ' ').replace(/\s+/g, ' ').trim();
    const shortText = cleanText.length > 120 ? cleanText.substring(0, 120) + '...' : cleanText;
    const statusStr = res.status ? ` (HTTP ${res.status})` : '';
    return {
        success: false,
        error: shortText ? `Server error${statusStr}: ${shortText}` : `Server error${statusStr}`
    };
}

async function configureNode() {
    const selectedNodeType = (document.getElementById('node-type-selected') || {}).value || 'lnd';

    if (!window.secureModeActive) {
        // Warn user about restart
        const confirmed = await confirmRestartModal(selectedNodeType);
        if (!confirmed) return;
    }

    const btn = document.getElementById('btn-configure-node');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Configuring...';
    }
    setActionMessage('configure-node-msg', 'Applying node configuration...', 'info');

    try {
        let res = await managementFetch('/api/local/configure-node', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeType: selectedNodeType })
        });
        let data = await safeFetchJson(res);

        if (res.status === 409 && data.requires_confirmation === true) {
            const decision = await confirmAnnouncementChanges(selectedNodeType, data.conflicts, data.diagnostics);
            if (!decision.confirmed) {
                setActionMessage('configure-node-msg', 'Configuration cancelled; existing announcement settings were not changed.', 'info');
                return;
            }
            res = await managementFetch('/api/local/configure-node', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    nodeType: selectedNodeType,
                    confirmAddressChanges: true,
                    retainTorAnnouncements: decision.retainTorAnnouncements
                })
            });
            data = await safeFetchJson(res);
        }

        if (res.ok && data.success !== false) {
            if (data.manual_mode) {
                setActionMessage('configure-node-msg', 'Manual configuration required.', 'info');
                showManualConfigModal(
                    data.node_type,
                    data.config_path,
                    data.config_lines,
                    data.cleanup_lines,
                    data.gossip_command,
                    data.historical_warning,
                    data.requires_confirmation !== false,
                    data.diagnostics,
                    data.gossip_commands
                );
            } else {
                const location = data.dns && data.port ? `${data.dns}:${data.port}` : 'configured endpoint';
                setActionMessage('configure-node-msg', `Node configured successfully: ${location}`, 'success');
            }
            fetchStatus();
        } else {
            const remaining = Array.isArray(data.remaining_conflicts) ? data.remaining_conflicts : [];
            const stillAnnounced = remaining.filter((item) => typeof item === 'string' && /^\S+:\d+$/.test(item));
            const baseError = data.error || 'Failed to configure node.';
            let message = baseError;
            if (stillAnnounced.length > 0) {
                message = `${baseError} Still announced: ${stillAnnounced.join(', ')}. See the Dashboard for how to fix it.`;
            } else if (remaining.length > 0) {
                message = `${baseError} See the Dashboard for details.`;
            }
            setActionMessage('configure-node-msg', message, 'error');
            if (remaining.length > 0) fetchStatus();
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
    if (!window.secureModeActive) {
        // Warn user about restart
        const confirmed = await confirmRestartModal('Lightning');
        if (!confirmed) return;
    }

    const btn = document.getElementById('btn-restore-node');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Restoring...';
    }
    setActionMessage('restore-node-msg', 'Restoring node configuration...', 'info');

    try {
        const res = await managementFetch('/api/local/restore-node', { method: 'POST' });
        const data = await safeFetchJson(res);

        if (res.ok && data.success !== false) {
            if (data.manual_mode) {
                if (!data.targets || data.targets.length === 0) {
                    setActionMessage('restore-node-msg', 'No active Lightning nodes detected for manual restore.', 'info');
                    if (data.requires_confirmation) {
                        showManualRestoreModal([], true);
                    }
                } else {
                    setActionMessage('restore-node-msg', 'Manual restore required.', 'info');
                    showManualRestoreModal(data.targets, Boolean(data.requires_confirmation));
                }
            } else {
                const lndState = data.lnd ? (data.lnd_changed ? 'updated' : 'no changes') : 'config not found';
                const clnState = data.cln ? (data.cln_changed ? 'updated' : 'no changes') : 'config not found';
                setActionMessage('restore-node-msg', `Restore complete. LND: ${lndState}. CLN: ${clnState}.`, 'success');
            }
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
        overlay.className = 'absolute inset-0 z-50 flex items-center justify-center bg-black/70 px-4';

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
        getAppShell().appendChild(overlay);

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

function confirmExpiredImport(meta) {
    return new Promise((resolve) => {
        const existingModal = document.getElementById('import-expired-modal');
        if (existingModal) {
            existingModal.remove();
        }

        const overlay = document.createElement('div');
        overlay.id = 'import-expired-modal';
        overlay.className = 'absolute inset-0 z-50 flex items-center justify-center bg-black/70 px-4';

        const panel = document.createElement('div');
        panel.className = 'w-full max-w-lg rounded-xl border border-red-900/50 bg-gray-900 p-6 shadow-2xl';

        const title = document.createElement('div');
        title.className = 'flex items-center gap-3 text-red-500 mb-4';

        const iconSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        iconSvg.setAttribute('class', 'w-6 h-6');
        iconSvg.setAttribute('fill', 'none');
        iconSvg.setAttribute('stroke', 'currentColor');
        iconSvg.setAttribute('viewBox', '0 0 24 24');
        const iconPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        iconPath.setAttribute('stroke-linecap', 'round');
        iconPath.setAttribute('stroke-linejoin', 'round');
        iconPath.setAttribute('stroke-width', '2');
        iconPath.setAttribute('d', 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z');
        iconSvg.appendChild(iconPath);

        const titleText = document.createElement('h3');
        titleText.className = 'text-lg font-bold';
        titleText.textContent = 'Expired Configuration Detected';

        title.appendChild(iconSvg);
        title.appendChild(titleText);

        const body = document.createElement('div');
        body.className = 'space-y-4';

        const desc = document.createElement('p');
        desc.className = 'text-sm text-gray-300 leading-relaxed';
        desc.textContent = "The subscription for this configuration has expired. You can still import it and proceed directly to the renewal section to extend it.";

        const details = document.createElement('div');
        details.className = 'bg-black/40 border border-gray-800 rounded-lg p-4 space-y-3 font-mono text-xs';

        const addRow = (label, value, valueClass, isVertical = false) => {
            const row = document.createElement('div');
            row.className = isVertical ? 'flex flex-col gap-1' : 'flex justify-between border-b border-gray-800 pb-2';
            const labelEl = document.createElement('span');
            labelEl.className = 'text-gray-500 uppercase';
            labelEl.textContent = label;
            const valueEl = document.createElement('span');
            valueEl.className = valueClass;
            valueEl.textContent = value || 'N/A';
            row.appendChild(labelEl);
            row.appendChild(valueEl);
            details.appendChild(row);
        };

        addRow('Subdomain', meta.serverDomain, 'text-white');
        addRow('Expiration', meta.expiresAt, 'text-red-400');
        addRow('WG Public Key', meta.wgPublicKey, 'text-gray-400 break-all', true);

        const actions = document.createElement('div');
        actions.className = 'mt-8 flex justify-end gap-3';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'rounded-lg border border-gray-600 px-4 py-2 text-sm font-semibold text-gray-200 hover:bg-gray-800';
        cancelBtn.textContent = 'Abort';

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'rounded-lg bg-tsyellow px-4 py-2 text-sm font-bold text-black hover:bg-yellow-400 transition-colors shadow-lg shadow-yellow-500/10';
        confirmBtn.textContent = 'Continue & Renew';

        actions.append(cancelBtn, confirmBtn);
        body.append(desc, details, actions);
        panel.append(title, body);
        overlay.appendChild(panel);
        getAppShell().appendChild(overlay);

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
            if (event.target === overlay) complete(false);
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
        let res = await managementFetch('/api/local/upload-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config })
        });

        let data = await res.json();

        // Authoritative sync: Handle expired config warning
        if (data.success && data.warning === 'Expired' && data.is_expired) {
            const confirmed = await confirmExpiredImport(data.meta);
            if (!confirmed) {
                setImportMessage("Import cancelled.", 'info');
                return;
            }

            // Retry with confirm=true
            setImportMessage("Saving expired config...", 'info');
            res = await managementFetch('/api/local/upload-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config, confirm: true })
            });
            data = await res.json();
        }

        if (res.ok && data.success !== false) {
            setImportMessage(data.message || "Configuration saved and parsed.", 'success');

            // If it was an expired config, move to renew tab after a short delay
            if (data.is_expired) {
                setTimeout(() => {
                    switchTab('renew');
                    showToast("Subscription expired. Please consider to renew.", 'error');
                }, 1500);
            } else {
                setTimeout(() => switchTab('import'), 1500);
            }
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
        if (
            status
            && status.vpn_active === true
            && status.rules_synced === true
            && !status.last_error
        ) {
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
        const res = await managementFetch('/api/local/restart', { method: 'POST' });
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

    const listEl = document.getElementById(`${dropdownId}-list`);
    if (listEl && listEl.getAttribute('role') === 'radiogroup') {
        listEl.querySelectorAll('button[role="radio"]').forEach((btn) => {
            const isMatch = String(btn.dataset.optionValue) === String(value);
            btn.setAttribute('aria-checked', isMatch ? 'true' : 'false');
            btn.tabIndex = isMatch ? 0 : -1;
            btn.classList.toggle('border-tsgreen', isMatch);
            btn.classList.toggle('bg-tsgreen/10', isMatch);
            btn.classList.toggle('border-gray-700', !isMatch && !btn.disabled);
            btn.classList.toggle('bg-gray-900/80', !isMatch && !btn.disabled);
        });
        return;
    }

    closeDropdown(dropdownId);
}

// --- v3.5.0 Subscription Timeline, Bandwidth Arc Gauge, NWC, Notices & Reachability ---
function formatSubscriptionCountdown(remainingMs) {
    if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
        return 'Expired';
    }
    const dayMs = 24 * 60 * 60 * 1000;
    if (remainingMs < dayMs) {
        const totalSeconds = Math.max(0, Math.floor(remainingMs / 1000));
        const hours = String(Math.floor(totalSeconds / 3600)).padStart(2, '0');
        const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, '0');
        const seconds = String(totalSeconds % 60).padStart(2, '0');
        return `${hours}:${minutes}:${seconds}`;
    }
    const days = Math.floor(remainingMs / dayMs);
    const hours = Math.floor((remainingMs % dayMs) / (60 * 60 * 1000));
    return `${days}d ${hours}h`;
}
window.formatSubscriptionCountdown = formatSubscriptionCountdown;

function extractNwcState(data) {
    const status = data && typeof data === 'object' ? data : {};
    const nwc = (status.nwc && typeof status.nwc === 'object')
        ? status.nwc
        : (status.subscription_telemetry && status.subscription_telemetry.nwc && typeof status.subscription_telemetry.nwc === 'object'
            ? status.subscription_telemetry.nwc
            : {});
    const stateObj = (nwc.state && typeof nwc.state === 'object') ? nwc.state : {};
    const connected = nwc.connected === true || nwc.enabled === true || status.nwc_connected === true;
    const lastError = nwc.last_error || nwc.lastRenewalError || stateObj.lastError || status.nwc_last_error || null;
    const healthy = connected && nwc.healthy !== false && !lastError && !stateObj.fallbackTaskRaised && !stateObj.budgetWarning && !stateObj.restoreReconnectNeeded;
    const renewalDuration = nwc.renewal_duration || nwc.renewalDuration || nwc.auto_renew_duration || nwc.autoRenewDuration || nwc.duration || 'match';
    const routeViaTor = nwc.route_via_tor === true || nwc.routeViaTor === true;
    return { connected, healthy, lastError, renewalDuration, routeViaTor };
}
window.extractNwcState = extractNwcState;

function formatNwcDurationShort(code) {
    const map = { match: 'Match', '1m': '1m', '3m': '3m', '6m': '6m', '12m': '12m', '1': '1m', '3': '3m', '6': '6m', '12': '12m' };
    return map[String(code || 'match').toLowerCase()] || 'Match';
}

function renderSubscriptionTimeline(data) {
    const status = data && typeof data === 'object' ? data : {};
    const telemetry = (status.subscription_telemetry && typeof status.subscription_telemetry === 'object')
        ? status.subscription_telemetry
        : {};
    const rawExpires = telemetry.expires_at || telemetry.expiresAt || status.expires_at || null;
    const ringEl = document.getElementById('subscription-ring-progress');
    const countdownEl = document.getElementById('subscription-countdown');
    const subEl = document.getElementById('subscription-expiry-sub');
    const badgeEl = document.getElementById('subscription-phase-badge');
    const srEl = document.getElementById('subscription-progress-sr');
    const descEl = document.getElementById('subscription-timeline-desc');

    if (subscriptionCountdownInterval) {
        clearInterval(subscriptionCountdownInterval);
        subscriptionCountdownInterval = null;
    }

    const parsedMs = rawExpires ? Date.parse(rawExpires) : NaN;
    if (!rawExpires || Number.isNaN(parsedMs)) {
        if (ringEl) {
            ringEl.setAttribute('stroke-dashoffset', String(SUBSCRIPTION_RING_CIRCUMFERENCE));
            ringEl.className.baseVal = 'text-gray-700 transition-all duration-500';
        }
        if (countdownEl) countdownEl.textContent = '--';
        if (subEl) subEl.textContent = 'Not setup';
        if (badgeEl) {
            badgeEl.textContent = 'No subscription';
            badgeEl.className = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-gray-700 bg-gray-800 text-gray-400';
        }
        if (srEl) {
            srEl.value = 0;
            srEl.textContent = '0%';
        }
        if (descEl) {
            descEl.textContent = 'Activate or import a TunnelSats subscription to monitor remaining time and enable NWC auto-renewal.';
        }
        return;
    }

    const dateStr = String(rawExpires).split('T')[0];
    const totalDaysGuess = Number(telemetry.plan_days || telemetry.total_days || 0);

    const updateTick = () => {
        const now = Date.now();
        const remainingMs = parsedMs - now;
        const remainingDays = remainingMs / (24 * 60 * 60 * 1000);
        const cycleDays = totalDaysGuess > 0
            ? totalDaysGuess
            : (remainingDays > 180 ? 365 : (remainingDays > 90 ? 180 : (remainingDays > 30 ? 90 : 30)));
        const pct = remainingMs <= 0
            ? 0
            : Math.max(0, Math.min(100, (remainingDays / cycleDays) * 100));
        const offset = (SUBSCRIPTION_RING_CIRCUMFERENCE * (1 - pct / 100)).toFixed(2);

        if (ringEl) {
            ringEl.setAttribute('stroke-dashoffset', String(offset));
        }
        if (countdownEl) {
            countdownEl.textContent = formatSubscriptionCountdown(remainingMs);
        }
        if (subEl) {
            subEl.textContent = remainingMs <= 0 ? `Expired ${dateStr}` : `Expires ${dateStr}`;
        }
        if (srEl) {
            srEl.value = Math.round(pct);
            srEl.textContent = `${Math.round(pct)}%`;
        }

        let phaseText = 'On track';
        let badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-tsgreen/50 bg-green-950/40 text-tsgreen';
        let ringColor = 'text-tsgreen transition-all duration-500';
        let descText = `Subscription active until ${dateStr}.`;

        if (remainingMs <= 0) {
            phaseText = 'Expired';
            badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-red-600 bg-red-950/50 text-red-400';
            ringColor = 'text-red-500 transition-all duration-500';
            descText = `Subscription expired on ${dateStr}. Renew now to restore VPN port forwarding.`;
        } else if (remainingDays <= 3) {
            phaseText = 'Renew now';
            badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-red-500/70 bg-red-950/40 text-red-300';
            ringColor = 'text-red-500 transition-all duration-500';
            descText = `Less than 3 days remaining before ${dateStr}. Renew now or verify NWC auto-renewal.`;
        } else if (remainingDays <= 7) {
            phaseText = 'Renew soon';
            badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-tsyellow/60 bg-yellow-950/40 text-tsyellow';
            ringColor = 'text-tsyellow transition-all duration-500';
            descText = `Expires within 7 days (${dateStr}). Enable NWC auto-renewal or extend manually.`;
        }

        if (badgeEl) {
            badgeEl.textContent = phaseText;
            badgeEl.className = badgeClass;
        }
        if (ringEl) {
            ringEl.setAttribute('class', ringColor);
        }
        if (descEl) {
            descEl.textContent = descText;
        }
    };

    updateTick();
    if (parsedMs > Date.now() && !isJest) {
        subscriptionCountdownInterval = setInterval(updateTick, 1000);
    }
}
window.renderSubscriptionTimeline = renderSubscriptionTimeline;

function extractBandwidthData(data) {
    const status = data && typeof data === 'object' ? data : {};
    const telemetry = (status.subscription_telemetry && typeof status.subscription_telemetry === 'object')
        ? status.subscription_telemetry
        : {};
    const bw = (telemetry.bandwidth && typeof telemetry.bandwidth === 'object')
        ? telemetry.bandwidth
        : (status.bandwidth && typeof status.bandwidth === 'object' ? status.bandwidth : telemetry);

    const usedGbRaw = bw.used_gb ?? bw.usedGb ?? bw.bandwidth_used_gb ?? 0;
    const quotaGbRaw = bw.quota_gb ?? bw.quotaGb ?? bw.limit_gb ?? bw.bandwidth_limit_gb ?? 90;
    const usedGb = Number.isFinite(Number(usedGbRaw)) ? Math.max(0, Number(usedGbRaw)) : 0;
    const quotaGb = Number.isFinite(Number(quotaGbRaw)) && Number(quotaGbRaw) > 0 ? Number(quotaGbRaw) : 90;
    const usedPctRaw = bw.used_pct ?? bw.usedPct ?? ((usedGb / quotaGb) * 100);
    const usedPct = Math.max(0, Math.min(100, Number.isFinite(Number(usedPctRaw)) ? Number(usedPctRaw) : 0));
    const cycleElapsedPctRaw = bw.cycle_elapsed_pct ?? bw.cycleElapsedPct ?? bw.pace_pct ?? bw.pacePct ?? null;
    const cycleElapsedPct = cycleElapsedPctRaw !== null && cycleElapsedPctRaw !== undefined && Number.isFinite(Number(cycleElapsedPctRaw))
        ? Math.max(0, Math.min(100, Number(cycleElapsedPctRaw)))
        : null;
    const daysUntilReset = bw.days_until_reset ?? bw.daysUntilReset ?? null;
    const nextResetAt = bw.next_reset_at ?? bw.nextResetAt ?? bw.resets_at ?? bw.next_free_reset_iso ?? bw.next_free_reset_utc ?? null;
    return { usedGb, quotaGb, usedPct, cycleElapsedPct, daysUntilReset, nextResetAt };
}
window.extractBandwidthData = extractBandwidthData;

function renderBandwidthGauge(data) {
    const { usedGb, quotaGb, usedPct, cycleElapsedPct, daysUntilReset, nextResetAt } = extractBandwidthData(data);
    const arcFill = document.getElementById('bandwidth-arc-fill');
    const paceMarker = document.getElementById('bandwidth-pace-marker');
    const pctText = document.getElementById('bandwidth-pct-text');
    const usedText = document.getElementById('bandwidth-used-text');
    const paceText = document.getElementById('bandwidth-pace-text');
    const resetsText = document.getElementById('bandwidth-resets-text');
    const nextFreeReset = document.getElementById('bandwidth-next-free-reset');
    const statusBadge = document.getElementById('bandwidth-status-badge');
    const meterSr = document.getElementById('bandwidth-meter-sr');
    const resetBtn = document.getElementById('btn-bandwidth-reset');

    const offset = (BANDWIDTH_ARC_LENGTH * (1 - usedPct / 100)).toFixed(2);
    if (arcFill) {
        arcFill.setAttribute('stroke-dashoffset', String(offset));
    }
    if (pctText) pctText.textContent = `${Math.round(usedPct)}%`;
    if (usedText) usedText.textContent = `${usedGb.toFixed(2)} / ${quotaGb} GB`;
    if (meterSr) {
        meterSr.value = Math.round(usedPct);
        meterSr.textContent = `${Math.round(usedPct)}%`;
    }

    let badgeLabel = 'Normal';
    let badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-tsgreen/50 bg-green-950/40 text-tsgreen';
    let arcColor = 'text-tsgreen transition-all duration-500';
    let btnClass = 'bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 font-bold text-xs px-3.5 py-2 rounded-xl transition shadow';

    if (usedPct >= 90) {
        badgeLabel = 'Critical';
        badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-red-500/70 bg-red-950/40 text-red-300';
        arcColor = 'text-red-500 transition-all duration-500';
        btnClass = 'bg-tsyellow hover:bg-yellow-400 text-black font-bold text-xs px-3.5 py-2 rounded-xl transition shadow';
    } else if (usedPct >= 70) {
        badgeLabel = 'High Usage';
        badgeClass = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-tsyellow/60 bg-yellow-950/40 text-tsyellow';
        arcColor = 'text-tsyellow transition-all duration-500';
        btnClass = 'bg-tsyellow hover:bg-yellow-400 text-black font-bold text-xs px-3.5 py-2 rounded-xl transition shadow';
    }

    if (statusBadge) {
        statusBadge.textContent = badgeLabel;
        statusBadge.className = badgeClass;
    }
    if (arcFill) {
        arcFill.setAttribute('class', arcColor);
    }
    if (resetBtn) {
        resetBtn.className = btnClass;
    }

    if (paceMarker) {
        if (cycleElapsedPct !== null) {
            const p = cycleElapsedPct / 100;
            const theta = Math.PI * (1 - p);
            const cx = (70 + 58 * Math.cos(theta)).toFixed(2);
            const cy = (72 - 58 * Math.sin(theta)).toFixed(2);
            paceMarker.setAttribute('cx', cx);
            paceMarker.setAttribute('cy', cy);
            paceMarker.classList.remove('hidden');
            if (paceText) {
                paceText.textContent = usedPct > cycleElapsedPct + 5
                    ? `Pace: Projected to exceed quota (${Math.round(cycleElapsedPct)}% of cycle elapsed)`
                    : `Pace: On track (${Math.round(cycleElapsedPct)}% of cycle elapsed)`;
            }
        } else {
            paceMarker.classList.add('hidden');
            if (paceText) {
                paceText.textContent = `Usage: ${Math.round(usedPct)}% of monthly quota`;
            }
        }
    }

    if (resetsText) {
        resetsText.textContent = daysUntilReset !== null && daysUntilReset !== undefined
            ? `Resets in ${daysUntilReset}d`
            : 'Resets monthly';
    }
    if (nextFreeReset) {
        const cleanDate = nextResetAt ? String(nextResetAt).split('T')[0] : '--';
        nextFreeReset.textContent = `Next free reset: ${cleanDate}`;
    }
}
window.renderBandwidthGauge = renderBandwidthGauge;

function renderNwcStatusUI(data) {
    const { connected, healthy, lastError, renewalDuration, routeViaTor } = extractNwcState(data);
    const shortDur = formatNwcDurationShort(renewalDuration);

    const dashDot = document.getElementById('dash-nwc-dot');
    const dashPillText = document.getElementById('dash-nwc-pill-text');
    const renewBadge = document.getElementById('nwc-status-badge');
    const renewActiveDur = document.getElementById('nwc-active-duration');
    const disconnectBtn = document.getElementById('btn-nwc-disconnect');
    const torCheck = document.getElementById('nwc-route-tor');

    if (torCheck && typeof routeViaTor === 'boolean') {
        torCheck.checked = routeViaTor;
    }

    if (connected) {
        if (healthy) {
            if (dashDot) dashDot.className = 'w-2 h-2 rounded-full bg-tsgreen';
            if (dashPillText) dashPillText.textContent = `Auto-Renew: On (${shortDur})`;
            if (renewBadge) {
                renewBadge.textContent = 'Active';
                renewBadge.className = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-tsgreen/60 bg-green-950/50 text-tsgreen';
            }
            if (renewActiveDur) {
                renewActiveDur.textContent = `Plan: ${shortDur}`;
                renewActiveDur.classList.remove('hidden');
            }
        } else {
            if (dashDot) dashDot.className = 'w-2 h-2 rounded-full bg-red-500';
            if (dashPillText) dashPillText.textContent = 'Auto-Renew: Error';
            if (renewBadge) {
                renewBadge.textContent = 'Error';
                renewBadge.className = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-red-500/70 bg-red-950/50 text-red-300';
                if (lastError) renewBadge.title = String(lastError);
            }
            if (renewActiveDur) renewActiveDur.classList.add('hidden');
        }
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
    } else {
        if (dashDot) dashDot.className = 'w-2 h-2 rounded-full bg-gray-500';
        if (dashPillText) dashPillText.textContent = 'Auto-Renew: Off';
        if (renewBadge) {
            renewBadge.textContent = 'Disabled';
            renewBadge.className = 'text-[10px] uppercase font-bold px-2.5 py-0.5 rounded-full border border-gray-700 bg-gray-900/70 text-gray-300';
        }
        if (renewActiveDur) renewActiveDur.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.add('hidden');
    }

    ['buy', 'renew', 'reset'].forEach((m) => updateInvoiceWalletButtons(m));
}
window.renderNwcStatusUI = renderNwcStatusUI;

function renderNotices(data) {
    const region = document.getElementById('notice-region');
    if (!region) return;
    region.replaceChildren();

    const status = data && typeof data === 'object' ? data : {};
    const { connected: nwcConnected, healthy: nwcHealthy, lastError: nwcError } = extractNwcState(status);
    const telemetry = (status.subscription_telemetry && typeof status.subscription_telemetry === 'object')
        ? status.subscription_telemetry
        : {};
    const { usedPct, usedGb, quotaGb } = extractBandwidthData(status);

    const banners = [];
    const rawBackendBanners = status.notices && Array.isArray(status.notices.banners)
        ? status.notices.banners
        : null;

    if (rawBackendBanners) {
        rawBackendBanners.forEach((b) => {
            if (!b || typeof b !== 'object') return;
            const id = String(b.id || b.code || b.type || '');
            // Suppress manual 7d and 3d expiry notices when NWC auto-renewal is active and healthy
            if (nwcHealthy && (id === 'expiring_7d' || id === 'expiring_3d' || id === 'expiry_7d' || id === 'expiry_3d' || id === 'renew_soon' || id === 'renew_now')) {
                return;
            }
            const rawSev = String(b.severity || 'warning').toLowerCase();
            const normalizedSeverity = (rawSev === 'red' || rawSev === 'critical' || rawSev === 'error')
                ? 'critical'
                : (rawSev === 'info' ? 'info' : 'warning');
            banners.push({
                id: id || 'notice',
                severity: normalizedSeverity,
                title: b.title || 'Subscription Notice',
                message: b.message || b.text || '',
                actionType: b.action_type || b.actionType || (b.kind === 'nwc' ? 'nwc' : (b.kind === 'bandwidth' ? 'bandwidth_reset' : null))
            });
        });
    } else {
        const rawExpires = telemetry.expires_at || telemetry.expiresAt || status.expires_at || null;
        const expMs = rawExpires ? Date.parse(rawExpires) : NaN;
        if (!Number.isNaN(expMs)) {
            const remMs = expMs - Date.now();
            const remDays = remMs / (24 * 60 * 60 * 1000);
            const dateStr = String(rawExpires).split('T')[0];
            if (remMs <= 0) {
                banners.push({
                    id: 'expired',
                    severity: 'critical',
                    title: 'Subscription Expired',
                    message: `Your TunnelSats subscription expired on ${dateStr}. Renew now to restore VPN routing.`,
                    actionType: 'renew'
                });
            } else if (remDays <= 1) {
                banners.push({
                    id: 'expiring_24h',
                    severity: 'critical',
                    title: 'Subscription Expires in Less Than 24 Hours',
                    message: `Your subscription expires on ${dateStr}. Renew immediately to prevent Lightning downtime.`,
                    actionType: 'renew'
                });
            } else if (remDays <= 3 && !nwcHealthy) {
                banners.push({
                    id: 'expiring_3d',
                    severity: 'critical',
                    title: 'Subscription Expiring Within 3 Days',
                    message: `Your subscription expires on ${dateStr}. Renew now or configure NWC Auto-Renew.`,
                    actionType: 'renew'
                });
            } else if (remDays <= 7 && !nwcHealthy) {
                banners.push({
                    id: 'expiring_7d',
                    severity: 'warning',
                    title: 'Subscription Expiring Soon',
                    message: `Less than 7 days remaining (expires ${dateStr}). Consider renewing or enabling NWC Auto-Renew.`,
                    actionType: 'renew'
                });
            }
        }

        if (nwcConnected && !nwcHealthy) {
            banners.push({
                id: 'nwc_failed',
                severity: 'critical',
                title: 'NWC Auto-Renewal Failed',
                message: nwcError
                    ? `Automatic renewal encountered an error: ${nwcError}`
                    : 'Your connected NWC wallet could not complete auto-renewal. Check wallet budget or renew manually.',
                actionType: 'nwc'
            });
        }

        if (usedPct >= 90) {
            banners.push({
                id: 'bandwidth_90',
                severity: 'critical',
                title: 'Critical Monthly Bandwidth Usage (>= 90%)',
                message: `You have used ${usedGb.toFixed(2)} of ${quotaGb} GB (${Math.round(usedPct)}%). Reset your bandwidth quota for $1 to avoid throttling.`,
                actionType: 'bandwidth_reset'
            });
        } else if (usedPct >= 70) {
            banners.push({
                id: 'bandwidth_70',
                severity: 'warning',
                title: 'High Monthly Bandwidth Usage (>= 70%)',
                message: `You have used ${usedGb.toFixed(2)} of ${quotaGb} GB (${Math.round(usedPct)}%). You can reset your monthly quota anytime for $1.`,
                actionType: 'bandwidth_reset'
            });
        }

        if (telemetry.stale === true) {
            banners.push({
                id: 'telemetry_stale',
                severity: 'info',
                title: 'Subscription Telemetry Cached',
                message: 'Showing last cached subscription telemetry while the remote API is unreachable.',
                actionType: 'refresh'
            });
        }
    }

    if (banners.length === 0) {
        region.classList.add('hidden');
        return;
    }

    banners.forEach((item) => {
        const isCritical = item.severity === 'critical' || item.severity === 'error';
        const isInfo = item.severity === 'info';
        const boxClass = isCritical
            ? 'rounded-xl border border-red-600/60 bg-red-950/40 p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3'
            : (isInfo
                ? 'rounded-xl border border-blue-500/50 bg-blue-950/30 p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3'
                : 'rounded-xl border border-tsyellow/50 bg-yellow-950/30 p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3');

        const bannerEl = document.createElement('div');
        bannerEl.className = boxClass;
        bannerEl.dataset.noticeId = item.id;
        bannerEl.dataset.severity = item.severity;

        const textWrap = document.createElement('div');
        textWrap.className = 'space-y-0.5';
        const titleEl = document.createElement('h4');
        titleEl.className = isCritical ? 'text-xs font-bold text-red-300' : (isInfo ? 'text-xs font-bold text-blue-300' : 'text-xs font-bold text-tsyellow');
        titleEl.textContent = item.title;
        const msgEl = document.createElement('p');
        msgEl.className = 'text-xs text-gray-300';
        msgEl.textContent = item.message;
        textWrap.append(titleEl, msgEl);

        const actionsWrap = document.createElement('div');
        actionsWrap.className = 'flex flex-wrap items-center gap-2 shrink-0';

        if (item.actionType === 'bandwidth_reset' || item.id.startsWith('bandwidth_')) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'bg-tsyellow hover:bg-yellow-400 text-black font-bold text-xs px-3 py-1.5 rounded-lg transition';
            btn.textContent = 'Reset Bandwidth ($1)';
            btn.addEventListener('click', () => handleBandwidthResetClick());
            actionsWrap.appendChild(btn);
        } else if (item.actionType === 'nwc' || item.id === 'nwc_failed') {
            const btnNwc = document.createElement('button');
            btnNwc.type = 'button';
            btnNwc.className = 'bg-tsgreen hover:bg-green-400 text-gray-900 font-bold text-xs px-3 py-1.5 rounded-lg transition';
            btnNwc.textContent = 'Fix NWC Auto-Renew';
            btnNwc.addEventListener('click', () => openNwcModal());
            const btnRenew = document.createElement('button');
            btnRenew.type = 'button';
            btnRenew.className = 'bg-gray-800 hover:bg-gray-700 border border-gray-700 text-white font-semibold text-xs px-3 py-1.5 rounded-lg transition';
            btnRenew.textContent = 'Renew Manually';
            btnRenew.addEventListener('click', () => switchTab('renew'));
            actionsWrap.append(btnNwc, btnRenew);
        } else if (item.actionType === 'refresh' || item.id === 'telemetry_stale') {
            const btnRefresh = document.createElement('button');
            btnRefresh.type = 'button';
            btnRefresh.className = 'bg-gray-800 hover:bg-gray-700 border border-gray-700 text-white font-semibold text-xs px-3 py-1.5 rounded-lg transition';
            btnRefresh.textContent = 'Refresh Status';
            btnRefresh.addEventListener('click', () => fetchStatus());
            actionsWrap.appendChild(btnRefresh);
        } else {
            const btnRenew = document.createElement('button');
            btnRenew.type = 'button';
            btnRenew.className = 'bg-tsyellow hover:bg-yellow-400 text-black font-bold text-xs px-3 py-1.5 rounded-lg transition';
            btnRenew.textContent = 'Renew Now';
            btnRenew.addEventListener('click', () => switchTab('renew'));
            const btnNwc = document.createElement('button');
            btnNwc.type = 'button';
            btnNwc.className = 'bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-200 font-semibold text-xs px-3 py-1.5 rounded-lg transition';
            btnNwc.textContent = 'Auto-Renew (NWC)';
            btnNwc.addEventListener('click', () => openNwcModal());
            actionsWrap.append(btnRenew, btnNwc);
        }

        bannerEl.append(textWrap, actionsWrap);
        region.appendChild(bannerEl);
    });

    region.classList.remove('hidden');
}
window.renderNotices = renderNotices;

// --- NWC Auto-Renew Modal & Payment Helpers ---

function normalizeNwcDurationCode(durationCode) {
    const raw = String(durationCode !== undefined && durationCode !== null ? durationCode : 'match').trim().toLowerCase();
    if (raw === '1' || raw === '1m') return '1m';
    if (raw === '3' || raw === '3m') return '3m';
    if (raw === '6' || raw === '6m') return '6m';
    if (raw === '12' || raw === '12m') return '12m';
    return 'match';
}
window.normalizeNwcDurationCode = normalizeNwcDurationCode;

function updateNwcRecommendedBudget(durationCode = 'match') {
    const code = normalizeNwcDurationCode(durationCode);
    const baseUsd = NWC_DURATION_USD[code] !== undefined ? NWC_DURATION_USD[code] : 2.89;
    const bufferedUsd = Number((baseUsd * 1.20).toFixed(2));
    const el = document.getElementById('nwc-recommended-budget');
    if (!el) return;
    let satsPart = '';
    if (currentSatsPerDollar) {
        const sats = Math.ceil(bufferedUsd * currentSatsPerDollar);
        satsPart = ` (~${sats.toLocaleString()} sats)`;
    }
    el.textContent = `Recommended wallet budget: $${bufferedUsd.toFixed(2)} (+20% buffer)${satsPart}`;
}
window.updateNwcRecommendedBudget = updateNwcRecommendedBudget;

function selectNwcDuration(durationCode) {
    const code = normalizeNwcDurationCode(durationCode);
    const selectEl = document.getElementById('nwc-duration-select');
    if (selectEl) selectEl.value = code;

    const container = document.getElementById('nwc-duration-pills');
    if (container) {
        container.querySelectorAll('button[data-nwc-duration]').forEach((btn) => {
            const isSelected = btn.getAttribute('data-nwc-duration') === code;
            btn.setAttribute('aria-checked', isSelected ? 'true' : 'false');
            btn.tabIndex = isSelected ? 0 : -1;
            btn.classList.toggle('border-tsgreen', isSelected);
            btn.classList.toggle('bg-tsgreen/10', isSelected);
            btn.classList.toggle('text-white', isSelected);
            btn.classList.toggle('border-gray-700', !isSelected);
            btn.classList.toggle('bg-gray-950', !isSelected);
        });
    }
    updateNwcRecommendedBudget(code);
}
window.selectNwcDuration = selectNwcDuration;

function openNwcModal() {
    const modal = document.getElementById('nwc-modal');
    if (!modal) return;
    const feedback = document.getElementById('nwc-modal-feedback');
    if (feedback) {
        feedback.textContent = '';
        feedback.classList.add('hidden');
    }
    if (latestStatusData) {
        const { renewalDuration } = extractNwcState(latestStatusData);
        selectNwcDuration(renewalDuration || 'match');
    } else {
        updateNwcRecommendedBudget('match');
    }
    modal.classList.remove('hidden');
}
window.openNwcModal = openNwcModal;

function closeNwcModal() {
    const modal = document.getElementById('nwc-modal');
    if (modal) modal.classList.add('hidden');
}
window.closeNwcModal = closeNwcModal;

function toggleNwcUriVisibility() {
    const input = document.getElementById('nwc-uri-input');
    const btn = document.getElementById('btn-toggle-nwc-uri');
    if (!input) return;
    const isPassword = input.type === 'password';
    input.type = isPassword ? 'text' : 'password';
    if (btn) btn.textContent = isPassword ? 'Hide' : 'Show';
}
window.toggleNwcUriVisibility = toggleNwcUriVisibility;

function setNwcFeedback(message, tone = 'info') {
    const el = document.getElementById('nwc-modal-feedback');
    if (!el) return;
    el.textContent = message;
    el.className = tone === 'error'
        ? 'text-xs font-semibold text-red-400'
        : (tone === 'success' ? 'text-xs font-semibold text-tsgreen' : 'text-xs font-semibold text-gray-300');
    el.classList.remove('hidden');
}

async function connectNwcWallet() {
    const uriInput = document.getElementById('nwc-uri-input');
    const durationSelect = document.getElementById('nwc-duration-select');
    const torCheck = document.getElementById('nwc-route-tor');
    const connectBtn = document.getElementById('btn-nwc-connect');

    const nwcUri = uriInput && typeof uriInput.value === 'string' ? uriInput.value.trim() : '';
    const renewalDuration = durationSelect && durationSelect.value ? durationSelect.value : 'match';
    const routeViaTor = Boolean(torCheck && torCheck.checked);

    if (!nwcUri || !nwcUri.startsWith('nostr+walletconnect://')) {
        setNwcFeedback('Please enter a valid nostr+walletconnect:// URI.', 'error');
        return;
    }

    if (connectBtn) {
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting...';
    }
    setNwcFeedback('Connecting NWC wallet...', 'info');

    try {
        const res = await managementFetch('/api/nwc/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nwcUri, renewalDuration, routeViaTor })
        });
        const data = await safeFetchJson(res);
        if (res.ok && data && data.success !== false) {
            setNwcFeedback(data.message || 'NWC Auto-Renew enabled!', 'success');
            showToast('NWC Auto-Renew connected!', 'success');
            await fetchStatus();
        } else {
            setNwcFeedback((data && (data.error || data.message)) || 'Failed to connect NWC wallet.', 'error');
        }
    } catch (e) {
        setNwcFeedback(`Failed to connect NWC wallet: ${e.message}`, 'error');
    } finally {
        if (connectBtn) {
            connectBtn.disabled = false;
            connectBtn.textContent = 'Connect & Enable Auto-Renew';
        }
    }
}
window.connectNwcWallet = connectNwcWallet;

async function disconnectNwcWallet() {
    const disconnectBtn = document.getElementById('btn-nwc-disconnect');
    if (disconnectBtn) {
        disconnectBtn.disabled = true;
        disconnectBtn.textContent = 'Disconnecting...';
    }
    try {
        const res = await managementFetch('/api/nwc/disconnect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await safeFetchJson(res);
        if (res.ok && (!data || data.success !== false)) {
            const uriInput = document.getElementById('nwc-uri-input');
            if (uriInput) uriInput.value = '';
            setNwcFeedback('NWC wallet disconnected.', 'info');
            showToast('NWC wallet disconnected.', 'info');
            await fetchStatus();
        } else {
            setNwcFeedback((data && (data.error || data.message)) || 'Failed to disconnect NWC wallet.', 'error');
        }
    } catch (e) {
        setNwcFeedback(`Failed to disconnect NWC wallet: ${e.message}`, 'error');
    } finally {
        if (disconnectBtn) {
            disconnectBtn.disabled = false;
            disconnectBtn.textContent = 'Disconnect Wallet';
        }
    }
}
window.disconnectNwcWallet = disconnectNwcWallet;

function updateInvoiceWalletButtons(mode) {
    const nwcBtn = document.getElementById(`btn-pay-nwc-${mode}`);
    const weblnBtn = document.getElementById(`btn-pay-webln-${mode}`);
    const { connected } = extractNwcState(latestStatusData);
    if (nwcBtn) {
        nwcBtn.classList.toggle('hidden', !connected);
    }
    if (weblnBtn) {
        const hasWebln = typeof window.webln !== 'undefined' && window.webln !== null;
        weblnBtn.classList.toggle('hidden', !hasWebln);
    }
}
window.updateInvoiceWalletButtons = updateInvoiceWalletButtons;

async function payWithNwc(mode) {
    const input = document.getElementById(`invoice-bolt11-${mode}`);
    const btn = document.getElementById(`btn-pay-nwc-${mode}`);
    const invoice = input && input.value ? input.value.trim() : '';
    if (!invoice) return;

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Paying via NWC...';
    }
    try {
        const res = await managementFetch('/api/nwc/pay', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ invoice, paymentHash: activePaymentHash, mode })
        });
        const data = await safeFetchJson(res);
        if (res.ok && data && data.success !== false) {
            showToast('Payment sent via NWC wallet!', 'success');
            await pollPayment();
        } else {
            showToast((data && (data.error || data.message)) || 'NWC payment failed.', 'error');
        }
    } catch (e) {
        showToast(`NWC payment failed: ${e.message}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Pay with Connected NWC Wallet';
        }
    }
}
window.payWithNwc = payWithNwc;

async function payWithWebln(mode) {
    const input = document.getElementById(`invoice-bolt11-${mode}`);
    const invoice = input && input.value ? input.value.trim() : '';
    if (!invoice || !window.webln) return;
    try {
        if (typeof window.webln.enable === 'function') {
            await window.webln.enable();
        }
        await window.webln.sendPayment(invoice);
        showToast('WebLN payment submitted!', 'success');
        await pollPayment();
    } catch (e) {
        showToast(`WebLN payment failed: ${e.message}`, 'error');
    }
}
window.payWithWebln = payWithWebln;

function startInvoiceExpiryTimer(mode, expiresAt) {
    if (invoiceExpiryInterval) {
        clearInterval(invoiceExpiryInterval);
        invoiceExpiryInterval = null;
    }
    const el = document.getElementById(`invoice-expiry-${mode}`);
    if (!el) return;

    const targetMs = expiresAt
        ? (typeof expiresAt === 'number' ? (expiresAt < 1e12 ? expiresAt * 1000 : expiresAt) : Date.parse(expiresAt))
        : (Date.now() + 15 * 60 * 1000);

    if (Number.isNaN(targetMs)) {
        el.textContent = '';
        return;
    }

    const tick = () => {
        const remSec = Math.max(0, Math.floor((targetMs - Date.now()) / 1000));
        if (remSec <= 0) {
            el.textContent = 'Invoice expired - generate a new invoice';
            el.className = 'text-xs font-mono text-red-400 mb-1';
            if (invoiceExpiryInterval) {
                clearInterval(invoiceExpiryInterval);
                invoiceExpiryInterval = null;
            }
            const createBtn = document.getElementById(`btn-create-${mode}`);
            if (createBtn) {
                createBtn.disabled = false;
                createBtn.textContent = mode === 'renew' ? 'Generate Renewal Invoice' : 'Generate Lightning Invoice';
            }
            return;
        }
        const mins = String(Math.floor(remSec / 60)).padStart(2, '0');
        const secs = String(remSec % 60).padStart(2, '0');
        el.textContent = `Invoice expires in ${mins}:${secs}`;
    };

    tick();
    if (!isJest) {
        invoiceExpiryInterval = setInterval(tick, 1000);
    }
}
window.startInvoiceExpiryTimer = startInvoiceExpiryTimer;

// --- Paid Bandwidth Reset ($1) Flow ---
function handleBandwidthResetClick() {
    const { usedPct, nextResetAt } = extractBandwidthData(latestStatusData);
    if (usedPct < 70) {
        const modal = document.getElementById('reset-confirm-modal');
        const textEl = document.getElementById('reset-confirm-text');
        if (textEl) {
            const resetDateStr = nextResetAt ? ` Your next free reset is on ${String(nextResetAt).split('T')[0]}.` : '';
            textEl.textContent = `Your monthly bandwidth usage is currently ${Math.round(usedPct)}% (below 70%).${resetDateStr} Do you still want to pay $1 to reset your cycle counter now?`;
        }
        if (modal) modal.classList.remove('hidden');
        return Promise.resolve(false);
    }
    return requestBandwidthReset();
}
window.handleBandwidthResetClick = handleBandwidthResetClick;

function closeResetConfirmModal() {
    const modal = document.getElementById('reset-confirm-modal');
    if (modal) modal.classList.add('hidden');
}
window.closeResetConfirmModal = closeResetConfirmModal;

function confirmBandwidthReset() {
    closeResetConfirmModal();
    return requestBandwidthReset();
}
window.confirmBandwidthReset = confirmBandwidthReset;

function closeResetInvoiceBox() {
    const box = document.getElementById('bandwidth-reset-invoice-box');
    if (box) box.classList.add('hidden');
}
window.closeResetInvoiceBox = closeResetInvoiceBox;

async function requestBandwidthReset() {
    const btn = document.getElementById('btn-bandwidth-reset');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Generating Invoice...';
    }
    try {
        const res = await managementFetch('/api/subscription/bandwidth-reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await safeFetchJson(res);
        const paymentHash = data && (data.paymentHash || data.payment_hash);
        const invoice = data && (data.invoice || data.payment_request);
        if (res.ok && paymentHash && invoice) {
            activePaymentHash = paymentHash;
            purchaseMode = 'reset';
            const controlsEl = document.getElementById('bandwidth-reset-invoice-controls');
            if (controlsEl) controlsEl.classList.remove('hidden');
            const successEl = document.getElementById('bandwidth-reset-success-msg');
            if (successEl) {
                successEl.classList.add('hidden');
                successEl.replaceChildren();
            }
            const input = document.getElementById('invoice-bolt11-reset');
            const link = document.getElementById('pay-link-reset');
            const box = document.getElementById('bandwidth-reset-invoice-box');
            if (input) input.value = invoice;
            if (link) link.href = `lightning:${invoice}`;
            renderQR('reset', invoice);
            if (box) box.classList.remove('hidden');
            updateInvoiceWalletButtons('reset');
            startInvoiceExpiryTimer('reset', data.expiresAt || data.expires_at);
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(pollPayment, POLL_INTERVAL_MS);
        } else {
            showToast((data && (data.error || data.message)) || 'Unable to create bandwidth reset invoice.', 'error');
        }
    } catch (e) {
        showToast(`Bandwidth reset failed: ${e.message}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Reset Bandwidth ($1)';
        }
    }
}
window.requestBandwidthReset = requestBandwidthReset;

// --- Inbound Reachability Probe ---
async function testReachability() {
    const btn = document.getElementById('btn-test-reachability');
    const resultEl = document.getElementById('reachability-result');
    const pubkeyWrap = document.getElementById('reachability-pubkey-wrap');
    const pubkeyInput = document.getElementById('reachability-pubkey-input');
    const customInputVal = pubkeyInput && typeof pubkeyInput.value === 'string' ? pubkeyInput.value.trim() : '';
    const requestPayload = customInputVal
        ? (customInputVal.includes('@') ? { socket: customInputVal } : { pubkey: customInputVal })
        : {};

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Testing...';
    }
    if (resultEl) {
        resultEl.className = 'text-xs text-gray-300 mt-0.5';
        resultEl.textContent = 'Testing inbound TCP reachability...';
    }

    try {
        const res = await managementFetch('/api/reachability', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestPayload)
        });
        const data = await safeFetchJson(res);
        if (!resultEl) return;

        if (data && data.needsPubkey && pubkeyWrap) {
            pubkeyWrap.classList.remove('hidden');
            if (pubkeyInput && typeof pubkeyInput.focus === 'function') {
                pubkeyInput.focus();
            }
        }

        if (data && (data.status === 'skipped' || data.skipped === true)) {
            resultEl.className = 'text-xs font-semibold text-tsyellow mt-0.5';
            resultEl.textContent = `Skipped: ${data.reason || data.message || 'VPN tunnel is not active.'}`;
        } else if (res.ok && data && (data.reachable === true || data.status === 'reachable')) {
            const endpoint = data.endpoint || data.socket || (data.host && data.port ? `${data.host}:${data.port}` : 'forwarded port');
            const rttVal = data.rtt_ms !== undefined && data.rtt_ms !== null ? data.rtt_ms : data.latency_ms;
            const rtt = rttVal !== undefined && rttVal !== null ? ` (${rttVal} ms)` : '';
            resultEl.className = 'text-xs font-semibold text-tsgreen mt-0.5';
            resultEl.textContent = `Reachable: ${endpoint}${rtt}`;
        } else if (data && (data.reachable === false || data.status === 'unreachable' || data.status === 'timeout')) {
            resultEl.className = 'text-xs font-semibold text-red-400 mt-0.5';
            resultEl.textContent = `Unreachable: ${data.error || data.reason || data.message || 'External TCP probe timed out.'}`;
        } else {
            resultEl.className = 'text-xs font-semibold text-red-400 mt-0.5';
            resultEl.textContent = `Reachability check unavailable: ${(data && (data.error || data.message)) || 'Unexpected response.'}`;
        }
    } catch (e) {
        if (resultEl) {
            resultEl.className = 'text-xs font-semibold text-red-400 mt-0.5';
            resultEl.textContent = `Reachability check unavailable: ${e.message}`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Test Reachability';
        }
    }
}
window.testReachability = testReachability;

function handleDocumentClick(e) {
    if (openDropdown) {
        const container = document.getElementById(`${openDropdown}-dropdown-container`);
        if (container && !container.contains(e.target)) {
            closeDropdown(openDropdown);
        }
    }

    const gotoBtn = e.target.closest('#btn-goto-instructions');
    if (gotoBtn) {
        e.preventDefault();
        const impl = gotoBtn.dataset ? gotoBtn.dataset.impl : '';
        if (impl === 'lnd' || impl === 'cln') setNodeType(impl, false);
        switchTab('import');
        setTimeout(() => {
            configureNode();
        }, 100);
        return;
    }

    const recheckBtn = e.target.closest('#btn-recheck-status');
    if (recheckBtn) {
        e.preventDefault();
        if (!recheckBtn.disabled) recheckStatus(recheckBtn);
        return;
    }

    if (e.target.closest('#btn-open-subscribe')) {
        e.preventDefault();
        switchTab('buy');
        return;
    }

    const switchNodeBtn = e.target.closest('#btn-switch-node-type');
    if (switchNodeBtn) {
        e.preventDefault();
        const impl = switchNodeBtn.dataset ? switchNodeBtn.dataset.impl : '';
        if (impl === 'lnd' || impl === 'cln') setNodeType(impl, false);
        switchTab('import');
        return;
    }

    if (e.target.closest('#btn-reload-page')) {
        e.preventDefault();
        window.location.reload();
    }
}

// Register once, even if this script is evaluated again, so no action runs twice.
if (window.__tsDocumentClickHandler) {
    document.removeEventListener('click', window.__tsDocumentClickHandler);
}
window.__tsDocumentClickHandler = handleDocumentClick;
document.addEventListener('click', window.__tsDocumentClickHandler);

async function downloadWireGuardConfig() {
    try {
        const downloadUrl = '/api/local/export-config';

        // 1. Verify config availability first
        const check = await managementFetch(downloadUrl);
        if (!check.ok) {
            const errData = await check.json().catch(() => ({}));
            alert(errData.error || 'No active WireGuard configuration profile found.');
            return;
        }

        // 2. Trigger native download via direct anchor link (bypasses HTTP 'Not secure' Blob restrictions in Chrome)
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = downloadUrl;
        a.download = 'tunnelsats.conf';
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        document.body.appendChild(a);
        a.click();

        setTimeout(() => {
            if (a.parentNode) {
                a.parentNode.removeChild(a);
            }
        }, 500);
    } catch (err) {
        console.error('Export error:', err);
        // Fallback for restricted webview/browser environments
        window.location.assign('/api/local/export-config');
    }
}
window.downloadWireGuardConfig = downloadWireGuardConfig;
