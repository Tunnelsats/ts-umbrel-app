const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.resolve(__dirname, '../index.html'), 'utf8');
const script = fs.readFileSync(path.resolve(__dirname, '../js/app.js'), 'utf8');

// --- Helpers ---

function setupDOM() {
    document.documentElement.innerHTML = html.toString();
    // Mock QRCode globally (loaded via CDN in real app)
    global.QRCode = jest.fn().mockImplementation(() => ({
        makeCode: jest.fn()
    }));
}

function evalScript() {
    window.eval(script);
}

// --- Test Suites ---

describe('UI Routing and Initialization', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Connected',
                    wg_pubkey: 'testpubkey123',
                    configs_found: [],
                    version: 'v3.0.0'
                }),
                ok: true
            })
        );
        evalScript();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('switchTab changes tab visibility correctly', () => {
        window.switchTab('buy');
        expect(document.getElementById('view-buy').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('view-dashboard').classList.contains('hidden')).toBe(true);
        expect(document.getElementById('nav-buy').classList.contains('nav-active')).toBe(true);
        expect(document.getElementById('nav-dashboard').classList.contains('nav-active')).toBe(false);
    });

    test('fetchStatus updates DOM elements', async () => {
        await window.fetchStatus();
        expect(document.getElementById('txt-wg-status').innerText).toBe('Connected');
        expect(document.getElementById('txt-pubkey').innerText).toBe('testpubkey123');
    });
});


describe('Phase 1: fetchServers', () => {
    beforeEach(() => {
        setupDOM();
        // Default fetch mock: status endpoint returns disconnected
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/status') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        wg_status: 'Disconnected', wg_pubkey: '', configs_found: [], version: 'v3.0.0'
                    }),
                    ok: true
                });
            }
            // /api/servers mock
            if (url === '/api/servers') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        servers: [
                            { id: 'eu-de', country: 'Germany', city: 'Nuremberg', flag: '🇩🇪', status: 'online' },
                            { id: 'us-east', country: 'USA', city: 'Ashburn', flag: '🇺🇸', status: 'online' }
                        ]
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('renders server buttons with flag, country, and city (no wireguardPort)', async () => {
        await window.fetchServers();
        const list = document.getElementById('buy-server-list');
        const buttons = list.querySelectorAll('button');

        expect(buttons.length).toBe(2);
        // Should use flag + country + city, NOT wireguardPort
        expect(buttons[0].innerText).toContain('Germany');
        expect(buttons[0].innerText).toContain('Nuremberg');
        expect(buttons[0].innerText).toContain('🇩🇪');
        expect(buttons[0].innerText).not.toContain('Port');
        expect(buttons[0].innerText).not.toContain('undefined');
    });

    test('first server is auto-selected', async () => {
        await window.fetchServers();
        const selectEl = document.getElementById('buy-server-select');
        expect(selectEl.value).toBe('eu-de');

        const labelEl = document.getElementById('buy-server-label');
        expect(labelEl.innerText).toContain('Germany');
    });
});


describe('Phase 1: pollPayment detects lowercase paid', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/status') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        wg_status: 'Disconnected', wg_pubkey: '', configs_found: [], version: 'v3.0.0'
                    }),
                    ok: true
                });
            }
            if (url === '/api/servers') {
                return Promise.resolve({
                    json: () => Promise.resolve({ servers: [] }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('recognizes "paid" (lowercase) status and clears poll', async () => {
        // Reset state explicitly
        window.activePaymentHash = 'test-hash-abc';
        window.purchaseMode = 'buy';
        // Ensure no lingering pollInterval
        if (window.pollInterval) clearInterval(window.pollInterval);

        // Show the invoice box so pollPayment can modify it
        const invoiceBox = document.getElementById('invoice-box-buy');
        invoiceBox.classList.remove('hidden');
        invoiceBox.innerHTML = '<p>Waiting...</p>';

        // Mock the status check to return paid (lowercase)
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({ status: 'paid' }),
                ok: true
            })
        );

        await window.pollPayment();

        // After detecting 'paid', the invoice box content should be updated
        // Check that old content is replaced (payment-received UI renders)
        expect(invoiceBox.innerHTML).not.toContain('Waiting...');
        expect(invoiceBox.textContent).toContain('Payment Received');
    });
});


describe('Phase 1: createSub generates invoice', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn((url, opts) => {
            if (url === '/api/local/status') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        wg_status: 'Disconnected', wg_pubkey: '', configs_found: [], version: 'v3.0.0'
                    }),
                    ok: true
                });
            }
            if (url === '/api/servers') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        servers: [
                            { id: 'eu-de', country: 'Germany', city: 'Nuremberg', flag: '🇩🇪', status: 'online' }
                        ]
                    }),
                    ok: true
                });
            }
            if (url === '/api/subscription/create') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        paymentHash: 'hash-xyz-123',
                        invoice: 'lnbc10u1p3testinvoice',
                        amount_sats: 1000,
                        description: 'TunnelSats VPN - 1 month',
                        expiresAt: '2026-04-01T00:00:00.000Z'
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('displays invoice and QR after createSub', async () => {
        // Reset state to avoid contamination from other suites
        window.activePaymentHash = null;
        if (window.pollInterval) clearInterval(window.pollInterval);

        // Set server selection
        await window.fetchServers();

        await window.createSub('buy');

        // Invoice box should be visible
        const invoiceBox = document.getElementById('invoice-box-buy');
        expect(invoiceBox.classList.contains('hidden')).toBe(false);

        // Invoice bolt11 field should contain the invoice
        const bolt11 = document.getElementById('invoice-bolt11-buy');
        expect(bolt11.value).toBe('lnbc10u1p3testinvoice');

        // Payment hash should be stored
        expect(window.activePaymentHash).toBe('hash-xyz-123');
    });
});
