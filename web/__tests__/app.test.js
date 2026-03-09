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
    Object.defineProperty(document, 'hidden', { value: false, configurable: true });
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
                    version: 'v3.0.0',
                    target_container: 'lightning_lnd_1',
                    target_ip: '10.21.21.6',
                    forwarding_port: 9735,
                    rules_synced: true,
                    last_reconcile_at: '2023-10-27T10:00:00Z',
                    last_error: 'Connection timeout'
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
        expect(document.getElementById('txt-target').innerText).toBe('lightning_lnd_1 (10.21.21.6)');
        expect(String(document.getElementById('txt-forwarding').querySelector('span').innerText)).toBe('9735');
        expect(document.getElementById('badge-rules').innerText).toBe('Synced');
        expect(document.getElementById('btn-reconcile').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('txt-reconcile').innerText).not.toBe('Never');
        expect(document.getElementById('txt-error').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('txt-error').querySelector('span').innerText).toBe('Connection timeout');
    });

    test('switchTab resumes polling and restores UI if activePaymentHash exists', () => {
        window.activePaymentHash = 'test-hash-123';
        window.purchaseMode = 'buy';
        
        // Navigate away, should clear polling
        window.switchTab('dashboard'); 
        expect(window.pollInterval).toBeFalsy();
        expect(document.getElementById('invoice-box-buy').classList.contains('hidden')).toBe(true);
        
        // Navigate back to buy tab
        window.switchTab('buy');
        
        // Polling should resume, UI should restore
        expect(window.pollInterval).not.toBeNull();
        expect(document.getElementById('invoice-box-buy').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('btn-create-buy').disabled).toBe(true);
        
        // Cleanup
        clearInterval(window.pollInterval);
        window.activePaymentHash = null;
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
        // Ensure the purchase mode view is visible so polling doesn't abort
        window.switchTab('buy');

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

    test('buy paid flow preserves paymentHash into import claim', async () => {
        window.switchTab('buy');
        window.activePaymentHash = 'buy-hash-123';
        window.purchaseMode = 'buy';
        if (window.pollInterval) clearInterval(window.pollInterval);

        const invoiceBox = document.getElementById('invoice-box-buy');
        invoiceBox.classList.remove('hidden');
        invoiceBox.innerHTML = '<p>Waiting...</p>';

        global.fetch = jest.fn((url) => {
            if (url === '/api/subscription/buy-hash-123') {
                return Promise.resolve({
                    json: () => Promise.resolve({ status: 'paid' }),
                    ok: true
                });
            }
            if (url === '/api/subscription/claim') {
                return Promise.resolve({
                    json: () => Promise.resolve({ success: true }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.pollPayment();

        const proceedBtn = Array.from(invoiceBox.querySelectorAll('button'))
            .find((btn) => btn.textContent.includes('Proceed to Installation'));
        expect(proceedBtn).toBeTruthy();
        proceedBtn.click();

        expect(window.activePaymentHash).toBe('buy-hash-123');
        expect(document.getElementById('view-import').classList.contains('hidden')).toBe(false);

        global.fetch.mockClear();
        await window.claimSubscription('import');

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/subscription/claim',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paymentHash: 'buy-hash-123', referralCode: null })
            })
        );
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

describe('Phase 2: Renew Flow', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn((url, options) => {
            if (url === '/api/local/meta') {
                return Promise.resolve({
                    json: () => Promise.resolve({ serverId: 'ch-zrh', wgPublicKey: 'pubkey789' }),
                    ok: true
                });
            }
            if (url === '/api/subscription/renew') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        paymentHash: 'renew-hash-123',
                        invoice: 'lnbcrenewtest',
                        amount_sats: 500
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('switchTab to renew auto-fills pubkey and server', async () => {
        window.switchTab('renew');
        // Wait for microtasks to finish so fetch callback resolves
        await new Promise(process.nextTick); 
        
        expect(document.getElementById('renew-server').value).toBe('ch-zrh');
        expect(document.getElementById('renew-pubkey').value).toBe('pubkey789');
    });

    test('createSub renew displays invoice and qr', async () => {
        window.activePaymentHash = null;
        if (window.pollInterval) clearInterval(window.pollInterval);

        // Pre-fill the form by simulating a tab switch so validation passes
        window.switchTab('renew');
        await new Promise(process.nextTick);

        // trigger the renew payload
        await window.createSub('renew');

        const invoiceBox = document.getElementById('invoice-box-renew');
        expect(invoiceBox.classList.contains('hidden')).toBe(false);
        const bolt11 = document.getElementById('invoice-bolt11-renew');
        expect(bolt11.value).toBe('lnbcrenewtest');
        expect(window.activePaymentHash).toBe('renew-hash-123');
    });
});

describe('Phase 3a: Import Config', () => {
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
            if (url === '/api/local/upload-config') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        message: 'Configuration saved and parsed.',
                        meta: { serverId: 'de2' }
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('pre-validation rejects empty import payload', async () => {
        document.getElementById('config-text').value = '   ';
        document.getElementById('txt-configs').innerText = 'None Detected';
        global.fetch.mockClear();

        await window.importConfig();

        const msg = document.getElementById('import-msg').innerText;
        expect(msg).toContain('Please paste a WireGuard config');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('pre-validation rejects config missing [Peer] block', async () => {
        document.getElementById('config-text').value = '[Interface]\nPrivateKey = abc\n';
        document.getElementById('txt-configs').innerText = 'None Detected';
        global.fetch.mockClear();

        await window.importConfig();

        const msg = document.getElementById('import-msg').innerText;
        expect(msg).toContain('Missing [Interface] or [Peer] block');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('import sends JSON payload and renders success message', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\nEndpoint = de2.tunnelsats.com:51820\n';
        const expectedConfig = config.trim();
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').innerText = 'None Detected';
        global.fetch.mockClear();

        await window.importConfig();

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/upload-config',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config: expectedConfig })
            })
        );
        const msg = document.getElementById('import-msg').innerText;
        expect(msg).toContain('Configuration saved and parsed.');
    });

    test('existing config cancel keeps import local and does not call backend', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\n';
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').innerText = 'tunnelsats.conf';
        global.fetch.mockClear();

        const importPromise = window.importConfig();
        const modal = document.getElementById('import-overwrite-modal');
        expect(modal).toBeTruthy();

        const cancelBtn = Array.from(modal.querySelectorAll('button')).find((btn) => btn.innerText === 'Cancel');
        cancelBtn.click();
        await importPromise;

        expect(document.getElementById('import-overwrite-modal')).toBeNull();
        expect(global.fetch).not.toHaveBeenCalled();
        expect(document.getElementById('import-msg').innerText).toContain('Import cancelled.');
    });

    test('existing config confirm proceeds with upload request', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\nEndpoint = de2.tunnelsats.com:51820\n';
        const expectedConfig = config.trim();
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').innerText = 'tunnelsats.conf';
        global.fetch.mockClear();

        const importPromise = window.importConfig();
        const modal = document.getElementById('import-overwrite-modal');
        expect(modal).toBeTruthy();

        const confirmBtn = Array.from(modal.querySelectorAll('button')).find((btn) => btn.innerText === 'Import Anyway');
        confirmBtn.click();
        await importPromise;

        expect(document.getElementById('import-overwrite-modal')).toBeNull();
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/upload-config',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config: expectedConfig })
            })
        );
    });

    test('import renders backend error message', async () => {
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/upload-config') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: false,
                        error: 'Invalid WireGuard configuration format. Missing [Interface] or [Peer] block.'
                    }),
                    ok: false
                });
            }
            return Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Disconnected', wg_pubkey: '', configs_found: [], version: 'v3.0.0', servers: []
                }),
                ok: true
            });
        });

        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\n';
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').innerText = 'None Detected';

        await window.importConfig();

        const msg = document.getElementById('import-msg').innerText;
        expect(msg).toContain('Invalid WireGuard configuration format');
    });
});

describe('Phase 3b: Install Config', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn((url, opts) => {
            if (url === '/api/local/configure-node') {
                const payload = opts && opts.body ? JSON.parse(opts.body) : {};
                if (payload.nodeType === 'cln') {
                    return Promise.resolve({
                        json: () => Promise.resolve({
                            success: true,
                            lnd: false,
                            cln: true,
                            port: 35825,
                            dns: 'de2.tunnelsats.com'
                        }),
                        ok: true
                    });
                }
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        lnd: true,
                        cln: false,
                        port: 35825,
                        dns: 'de2.tunnelsats.com'
                    }),
                    ok: true
                });
            }
            if (url === '/api/local/restore-node') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        lnd: true,
                        cln: true,
                        lnd_changed: true,
                        cln_changed: false
                    }),
                    ok: true
                });
            }
            return Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Disconnected',
                    wg_pubkey: '',
                    configs_found: [],
                    version: 'v3.0.0',
                    target_impl: 'lnd',
                    servers: []
                }),
                ok: true
            });
        });
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('setNodeType updates selected node type state', () => {
        window.setNodeType('cln');
        expect(document.getElementById('node-type-selected').value).toBe('cln');
        expect(document.getElementById('node-type-cln').className).toContain('bg-tsyellow');
        expect(document.getElementById('node-type-lnd').className).not.toContain('bg-tsyellow');
    });

    test('configureNode posts selected nodeType and renders success', async () => {
        window.setNodeType('lnd');
        await window.configureNode();

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/configure-node',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodeType: 'lnd' })
            })
        );
        expect(document.getElementById('configure-node-msg').innerText).toContain('de2.tunnelsats.com:35825');
    });

    test('configureNode supports cln nodeType payload', async () => {
        window.setNodeType('cln');
        await window.configureNode();

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/configure-node',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodeType: 'cln' })
            })
        );
    });

    test('restoreNode calls backend and renders result summary', async () => {
        await window.restoreNode();

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/restore-node',
            expect.objectContaining({ method: 'POST' })
        );
        const msg = document.getElementById('restore-node-msg').innerText;
        expect(msg).toContain('LND');
        expect(msg).toContain('CLN');
    });

    test('restoreNode reports missing configs clearly', async () => {
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/restore-node') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        lnd: false,
                        cln: false,
                        lnd_changed: false,
                        cln_changed: false
                    }),
                    ok: true
                });
            }
            return Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Disconnected',
                    wg_pubkey: '',
                    configs_found: [],
                    version: 'v3.0.0',
                    target_impl: 'lnd',
                    servers: []
                }),
                ok: true
            });
        });

        await window.restoreNode();

        const msg = document.getElementById('restore-node-msg').innerText;
        expect(msg).toContain('LND: config not found');
        expect(msg).toContain('CLN: config not found');
    });

    test('pollReconcileStatus fails fast on non-2xx responses', async () => {
        const timeoutSpy = jest.spyOn(window, 'setTimeout');
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/reconcile/failure-case') {
                return Promise.resolve({
                    json: () => Promise.resolve({ error: 'temporary failure' }),
                    ok: false
                });
            }
            return Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Disconnected',
                    wg_pubkey: '',
                    configs_found: [],
                    version: 'v3.0.0',
                    target_impl: 'lnd',
                    servers: []
                }),
                ok: true
            });
        });

        await window.pollReconcileStatus('/api/local/reconcile/failure-case');

        expect(document.getElementById('reconcile-text').innerText).toBe('Failed');
        expect(timeoutSpy).toHaveBeenCalledWith(window.resetReconcileBtn, 3000);
        expect(timeoutSpy.mock.calls.some(([, delay]) => delay === 2000)).toBe(false);
    });

    test('pollReconcileStatus surfaces network issues after repeated fetch failures', async () => {
        const timeoutSpy = jest.spyOn(window, 'setTimeout');
        global.fetch = jest.fn(() => Promise.reject(new Error('network down')));
        document.getElementById('reconcile-text').innerText = 'Reconciling...';

        await window.pollReconcileStatus('/api/local/reconcile/net-err');
        await window.pollReconcileStatus('/api/local/reconcile/net-err');
        expect(document.getElementById('reconcile-text').innerText).not.toContain('network issues');

        await window.pollReconcileStatus('/api/local/reconcile/net-err');
        expect(document.getElementById('reconcile-text').innerText).toBe('Reconciling (network issues)...');
        expect(timeoutSpy.mock.calls.filter(([, delay]) => delay === 2000).length).toBeGreaterThanOrEqual(3);
    });
});
