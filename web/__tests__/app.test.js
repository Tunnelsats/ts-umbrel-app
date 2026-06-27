const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.resolve(__dirname, '../index.html'), 'utf8');
const script = fs.readFileSync(path.resolve(__dirname, '../js/app.js'), 'utf8');

// --- Helpers ---

function createMockGlobeInstance() {
    const mockGlobe = {};
    [
        'backgroundColor', 'showAtmosphere', 'atmosphereColor', 'atmosphereAltitude',
        'globeImageUrl', 'bumpImageUrl', 'pointsData', 'pointAltitude', 'pointColor',
        'pointRadius', 'pointsTransitionDuration', 'ringsData', 'ringColor',
        'ringMaxRadius', 'ringPropagationSpeed', 'ringRepeatPeriod', 'labelsData',
        'labelLat', 'labelLng', 'labelText', 'labelSize', 'labelDotRadius',
        'labelColor', 'labelResolution', 'controls', 'autoRotate', 'autoRotateSpeed',
        'enableZoom', 'width', 'height', 'pointOfView'
    ].forEach(method => {
        mockGlobe[method] = jest.fn().mockReturnValue(mockGlobe);
    });
    mockGlobe.controls = jest.fn().mockReturnValue({
        autoRotate: true,
        autoRotateSpeed: 0.5,
        enableZoom: false
    });
    return mockGlobe;
}

function setupDOM() {
    document.documentElement.innerHTML = html.toString();
    // Mock QRCode globally (loaded via CDN in real app)
    global.QRCode = jest.fn().mockImplementation(() => ({
        makeCode: jest.fn()
    }));
    // Mock Globe.gl globally
    const mockGlobe = createMockGlobeInstance();
    // Globe() returns a function that returns mockGlobe
    global.Globe = jest.fn().mockReturnValue(jest.fn().mockReturnValue(mockGlobe));
    Object.defineProperty(document, 'hidden', { value: false, configurable: true });
}

function evalScript() {
    // In jsdom, document.readyState is already "complete", so app.js eagerly runs initApp().
    window.eval(script);
}

describe('Globe initialization resilience', () => {
    afterEach(() => {
        jest.useRealTimers();
        jest.restoreAllMocks();
    });

    test('retries globe initialization after a transient failure', () => {
        jest.useFakeTimers();
        setupDOM();

        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({ servers: [], wg_status: 'Disconnected' }),
                ok: true
            })
        );

        const mockGlobe = createMockGlobeInstance();
        let attempts = 0;
        global.Globe = jest.fn().mockReturnValue(jest.fn().mockImplementation(() => {
            attempts += 1;
            if (attempts === 1) throw new Error('transient globe init error');
            return mockGlobe;
        }));
        jest.spyOn(console, 'error').mockImplementation(() => {});

        evalScript();
        expect(attempts).toBe(1);

        jest.advanceTimersByTime(1000);
        expect(attempts).toBe(2);
    });
});

// --- Test Suites ---

describe('UI Routing and Initialization', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    vpn_active: true,
                    lnd_detected: true,
                    cln_detected: false,
                    lnd_routing_active: true,
                    cln_routing_active: false,
                    wg_status: 'Connected',
                    wg_pubkey: 'testpubkey123',
                    server_domain: 'au1.tunnelsats.com',
                    lat: -33.8688,
                    lng: 151.2093,
                    label: 'SYDNEY, AU',
                    vpn_port: 39486,
                    expires_at: '2027-03-10T12:00:00Z',
                    target_impl: 'lnd',
                    target_container: 'lightning_lnd_1',
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

    test('footer FAQ link switches to FAQ view without requiring a nav button', () => {
        const faqView = document.getElementById('view-faq');
        const footerFaq = document.getElementById('btn-footer-faq');

        expect(faqView.classList.contains('hidden')).toBe(true);

        footerFaq.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));

        expect(faqView.classList.contains('hidden')).toBe(false);
        expect(document.getElementById('view-dashboard').classList.contains('hidden')).toBe(true);
        expect(footerFaq.classList.contains('text-blue-400')).toBe(true);

        window.switchTab('buy');
        expect(footerFaq.classList.contains('text-blue-400')).toBe(false);
        expect(footerFaq.classList.contains('text-gray-500')).toBe(true);
    });

    test('delegated data-scroll-to click prevents default and smooth-scrolls when target exists', () => {
        const faq3 = document.getElementById('faq-3');
        const tocLink = document.querySelector('[data-scroll-to="faq-3"]');
        faq3.scrollIntoView = jest.fn();

        const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
        tocLink.dispatchEvent(clickEvent);

        expect(clickEvent.defaultPrevented).toBe(true);
        expect(faq3.scrollIntoView).toHaveBeenCalledTimes(1);
        expect(faq3.scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth' });
    });

    test('delegated data-scroll-to click does not prevent default when target does not exist', () => {
        const missingTargetLink = document.createElement('a');
        missingTargetLink.setAttribute('href', '#missing-faq-item');
        missingTargetLink.setAttribute('data-scroll-to', 'missing-faq-item');
        document.body.appendChild(missingTargetLink);

        try {
            const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
            expect(() => missingTargetLink.dispatchEvent(clickEvent)).not.toThrow();
            expect(clickEvent.defaultPrevented).toBe(false);
        } finally {
            missingTargetLink.remove();
        }
    });

    test('delegated data-scroll-to click switches tab when target is inside a hidden section', () => {
        const faq3 = document.getElementById('faq-3');
        const tocLink = document.querySelector('[data-scroll-to="faq-3"]');
        faq3.scrollIntoView = jest.fn();

        const faqView = document.getElementById('view-faq');
        faqView.classList.add('hidden');
        window.switchTab = jest.fn();

        const clickEvent = new MouseEvent('click', { bubbles: true, cancelable: true });
        tocLink.dispatchEvent(clickEvent);

        expect(clickEvent.defaultPrevented).toBe(true);
        expect(window.switchTab).toHaveBeenCalledWith('faq');
        expect(faq3.scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth' });

        faqView.classList.remove('hidden');
        delete window.switchTab;
    });

    test('hash routing switches to FAQ tab on load/hashchange', () => {
        jest.useFakeTimers();
        const faq6 = document.getElementById('faq-6');
        faq6.scrollIntoView = jest.fn();
        window.switchTab = jest.fn();

        window.location.hash = '#faq-6';
        window.__tsHashHandler();

        expect(window.switchTab).toHaveBeenCalledWith('faq');
        jest.advanceTimersByTime(150);
        expect(faq6.scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth' });

        window.location.hash = '';
        delete window.switchTab;
        jest.useRealTimers();
    });

    test('all target=_blank links include noopener and noreferrer', () => {
        const links = Array.from(document.querySelectorAll('a[target="_blank"]'));
        expect(links.length).toBeGreaterThan(0);

        links.forEach((link) => {
            const rel = link.getAttribute('rel') || '';
            expect(rel).toContain('noopener');
            expect(rel).toContain('noreferrer');
        });
    });

    test('fetchStatus updates DOM elements', async () => {
        await window.fetchStatus();
        expect(document.getElementById('txt-wg-status').textContent).toBe('Connected');
        expect(document.getElementById('txt-routing-status').textContent).toBe('Node Routing Secured');
        expect(document.getElementById('badge-routing').textContent).toBe('Active');
        expect(document.getElementById('btn-dash-disable-routing').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('btn-dash-enable-routing').classList.contains('hidden')).toBe(true);
        expect(document.getElementById('box-pubkey').textContent).toBe('testpubkey123');
        expect(document.getElementById('box-node').textContent).toBe('LND');
        expect(document.getElementById('box-server').textContent).toBe('au1.tunnelsats.com');
        expect(document.getElementById('box-port').textContent).toBe('39486');
        expect(document.getElementById('box-expiration').textContent).toBe('2027-03-10');
    });

    test('fetchStatus does not show Protected when no node is detected', async () => {
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    vpn_active: true,
                    lnd_detected: false,
                    cln_detected: false,
                    lnd_routing_active: true,
                    cln_routing_active: false,
                    wg_status: 'Connected',
                    wg_pubkey: 'testpubkey123',
                    server_domain: 'au1.tunnelsats.com',
                    vpn_port: 39486,
                    expires_at: '2027-03-10T12:00:00Z',
                    target_impl: '',
                    configs_found: [],
                    version: 'v3.0.0'
                }),
                ok: true
            })
        );

        await window.fetchStatus();

        expect(document.getElementById('statusBadge').textContent).toBe('Connected');
        expect(document.getElementById('txt-routing-status').textContent).toBe('No Nodes Detected');
    });

    test('fetchStatus adjusts UI for secure_mode: true', async () => {
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    vpn_active: true,
                    lnd_detected: true,
                    cln_detected: false,
                    lnd_routing_active: true,
                    cln_routing_active: false,
                    wg_status: 'Connected',
                    wg_pubkey: 'testpubkey123',
                    server_domain: 'au1.tunnelsats.com',
                    vpn_port: 39486,
                    expires_at: '2027-03-10T12:00:00Z',
                    target_impl: 'lnd',
                    configs_found: [],
                    version: 'v3.0.0',
                    secure_mode: true
                }),
                ok: true
            })
        );

        await window.fetchStatus();

        expect(document.getElementById('install-desc').textContent).toContain('Since TunnelSats is running in Secure Mode, the configuration cannot be applied automatically');
        expect(document.getElementById('uninstall-desc').textContent).toContain('Since TunnelSats is running in Secure Mode, the configuration cannot be modified automatically');
        expect(document.getElementById('faq-uninstall-steps').innerHTML).toContain('Follow the modal instructions to manually comment out or delete');
        
        const warningBox = document.getElementById('secure-mode-warning-box');
        const uninstallWarningBox = document.getElementById('secure-mode-uninstall-warning-box');
        expect(warningBox.classList.contains('hidden')).toBe(false);
        expect(warningBox.textContent).toContain('Files app');
        expect(uninstallWarningBox.classList.contains('hidden')).toBe(false);
        expect(uninstallWarningBox.textContent).toContain('manually revert configuration changes');
        
        expect(document.getElementById('faq-6')).toBeTruthy();
        expect(document.getElementById('faq-6').textContent).toContain('In Secure Mode, why must I edit my config manually');
        
        expect(window.secureModeActive).toBe(true);
    });

    test('fetchStatus adjusts UI for secure_mode: false', async () => {
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    vpn_active: true,
                    lnd_detected: true,
                    cln_detected: false,
                    lnd_routing_active: true,
                    cln_routing_active: false,
                    wg_status: 'Connected',
                    wg_pubkey: 'testpubkey123',
                    server_domain: 'au1.tunnelsats.com',
                    vpn_port: 39486,
                    expires_at: '2027-03-10T12:00:00Z',
                    target_impl: 'lnd',
                    configs_found: [],
                    version: 'v3.0.0',
                    secure_mode: false
                }),
                ok: true
            })
        );

        await window.fetchStatus();

        expect(document.getElementById('install-desc').textContent).toContain('Choose your node implementation and inject the TunnelSats forwarding host');
        expect(document.getElementById('uninstall-desc').textContent).toContain('Revert TunnelSats node configuration changes before uninstalling');
        expect(document.getElementById('faq-uninstall-steps').innerHTML).toContain('Wait for the UI to confirm that your node configuration has been restored');
        
        expect(document.getElementById('secure-mode-warning-box').classList.contains('hidden')).toBe(true);
        expect(document.getElementById('secure-mode-uninstall-warning-box').classList.contains('hidden')).toBe(true);
        
        expect(window.secureModeActive).toBe(false);
    });

    test('configureNode bypasses confirmRestartModal in secure mode', async () => {
        window.secureModeActive = true;
        window.confirmRestartModal = jest.fn().mockResolvedValue(true);
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    success: true,
                    manual_mode: true,
                    node_type: 'lnd',
                    config_path: '/path/to/lnd.conf',
                    config_lines: ['line1', 'line2']
                }),
                ok: true
            })
        );

        await window.configureNode();

        expect(window.confirmRestartModal).not.toHaveBeenCalled();
        expect(document.getElementById('manual-config-modal')).toBeTruthy();
        
        document.getElementById('manual-config-modal').remove();
    });

    test('restoreNode bypasses confirmRestartModal in secure mode', async () => {
        window.secureModeActive = true;
        window.confirmRestartModal = jest.fn().mockResolvedValue(true);
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    success: true,
                    manual_mode: true,
                    restore: true,
                    targets: [
                        {
                            node_type: 'lnd',
                            config_path: '/path/to/lnd.conf',
                            config_lines: ['remove1']
                        }
                    ]
                }),
                ok: true
            })
        );

        await window.restoreNode();

        expect(window.confirmRestartModal).not.toHaveBeenCalled();
        expect(document.getElementById('manual-restore-modal')).toBeTruthy();

        document.getElementById('manual-restore-modal').remove();
    });

    test('FAQ layout ordering matches Q5 -> Q6 (Secure Mode) -> Q7 (Troubleshooting)', () => {
        const anchors = Array.from(document.querySelectorAll('#view-faq a[data-scroll-to]'));
        const index5 = anchors.findIndex(a => a.getAttribute('data-scroll-to') === 'faq-5');
        const index6 = anchors.findIndex(a => a.getAttribute('data-scroll-to') === 'faq-6');
        const index7 = anchors.findIndex(a => a.getAttribute('data-scroll-to') === 'faq-7');
        
        expect(index5).toBeLessThan(index6);
        expect(index6).toBeLessThan(index7);

        const faq5 = document.getElementById('faq-5');
        const faq6 = document.getElementById('faq-6');
        const faq7 = document.getElementById('faq-7');

        expect(faq5.compareDocumentPosition(faq6)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
        expect(faq6.compareDocumentPosition(faq7)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    });

    test('Install/Uninstall tab descriptions and warning boxes have increased font sizes', () => {
        const installDesc = document.getElementById('install-desc');
        const uninstallDesc = document.getElementById('uninstall-desc');
        const warningBox = document.getElementById('secure-mode-warning-box');
        const uninstallWarningBox = document.getElementById('secure-mode-uninstall-warning-box');

        expect(installDesc.className).toContain('text-base');
        expect(uninstallDesc.className).toContain('text-base');
        expect(warningBox.className).toContain('text-sm');
        expect(uninstallWarningBox.className).toContain('text-sm');
    });

    test('Manual configuration and restore modals have increased font sizes and correct typography styling', async () => {
        // Test manual config modal font sizes
        window.showManualConfigModal('lnd', '/path/to/lnd.conf', ['line1']);
        const configModal = document.getElementById('manual-config-modal');
        expect(configModal).toBeTruthy();
        expect(configModal.querySelector('p').className).toContain('text-sm'); // desc has text-sm
        expect(configModal.querySelector('ol').className).toContain('text-sm'); // instructions have text-sm
        expect(configModal.querySelector('pre').className).toContain('text-sm'); // pre code block has text-sm
        const configBtns = configModal.querySelectorAll('button');
        const configDoneBtn = Array.from(configBtns).find(btn => btn.textContent === 'Done');
        expect(configDoneBtn.className).toContain('text-base'); // Done btn has text-base
        configDoneBtn.click();
        await new Promise(resolve => setTimeout(resolve, 350));

        // Test manual restore modal font sizes and formatting
        window.showManualRestoreModal([{
            node_type: 'lnd',
            config_path: '/path/to/lnd.conf',
            config_lines: ['remove1']
        }]);
        const restoreModal = document.getElementById('manual-restore-modal');
        expect(restoreModal).toBeTruthy();
        expect(restoreModal.querySelector('p').className).toContain('text-sm'); // desc has text-sm
        const restoreDoneBtn = Array.from(restoreModal.querySelectorAll('button')).find(btn => btn.textContent === 'Done');
        expect(restoreDoneBtn.className).toContain('text-base'); // Done btn has text-base
        
        // Check formatting of the configuration lines list
        const ul = restoreModal.querySelector('ul');
        expect(ul.className).toContain('list-disc');
        expect(ul.className).toContain('text-sm');
        expect(ul.className).toContain('text-gray-300'); // normal sans-serif gray text for list item
        expect(ul.className).not.toContain('font-mono'); // ul shouldn't force monospace globally
        expect(ul.className).not.toContain('text-tsyellow'); // ul shouldn't force yellow globally

        const li = ul.querySelector('li');
        expect(li.className).toContain('text-sm');
        expect(li.innerHTML).toContain('Remove/comment out:');
        const code = li.querySelector('code');
        expect(code).toBeTruthy();
        expect(code.className).toContain('font-mono');
        expect(code.className).toContain('text-tsyellow');
        expect(code.textContent).toBe('remove1');

        restoreDoneBtn.click();
        await new Promise(resolve => setTimeout(resolve, 350));
    });

    test('manual configuration modal closes on clicking the overlay outside the panel but not inside', async () => {
        window.secureModeActive = true;
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    success: true,
                    manual_mode: true,
                    node_type: 'lnd',
                    config_path: '/path/to/lnd.conf',
                    config_lines: ['line1']
                }),
                ok: true
            })
        );

        await window.configureNode();

        const overlay = document.getElementById('manual-config-modal');
        expect(overlay).toBeTruthy();

        // Click inside panel
        const panel = overlay.querySelector('div');
        panel.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 50));
        expect(document.getElementById('manual-config-modal')).toBeTruthy();

        // Click outside panel (on overlay itself)
        overlay.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 350));
        expect(document.getElementById('manual-config-modal')).toBeNull();
    });

    test('manual restore modal closes on clicking the overlay outside the panel but not inside', async () => {
        window.secureModeActive = true;
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    success: true,
                    manual_mode: true,
                    restore: true,
                    targets: [{
                        node_type: 'lnd',
                        config_path: '/path/to/lnd.conf',
                        config_lines: ['remove1']
                    }]
                }),
                ok: true
            })
        );

        await window.restoreNode();

        const overlay = document.getElementById('manual-restore-modal');
        expect(overlay).toBeTruthy();

        // Click inside panel
        const panel = overlay.querySelector('div');
        panel.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 50));
        expect(document.getElementById('manual-restore-modal')).toBeTruthy();

        // Click outside panel (on overlay itself)
        overlay.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 350));
        expect(document.getElementById('manual-restore-modal')).toBeNull();
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
                            { id: 'eu-de', country: 'Germany', city: 'Nuremberg', flag: '🇩🇪', status: 'online', lat: 49.4521, lng: 11.0767, label: 'NUREMBERG, DE' },
                            { id: 'us-east', country: 'USA', city: 'Ashburn', flag: '🇺🇸', status: 'online', lat: 40.7128, lng: -74.0060, label: 'NEW YORK, US' }
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
        expect(buttons[0].textContent).toContain('Germany');
        expect(buttons[0].textContent).toContain('Nuremberg');
        expect(buttons[0].textContent).toContain('🇩🇪');
        expect(buttons[0].textContent).not.toContain('Port');
        expect(buttons[0].textContent).not.toContain('undefined');
    });

    test('first server is auto-selected', async () => {
        await window.fetchServers();
        const selectEl = document.getElementById('buy-server-select');
        expect(selectEl.value).toBe('eu-de');

        const labelEl = document.getElementById('buy-server-label');
        expect(labelEl.textContent).toContain('Germany');
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
                body: JSON.stringify({ paymentHash: 'buy-hash-123', wgPublicKey: '', wgPresharedKey: '', referralCode: null })
            })
        );
    });

    test('claimSubscription treats success payload with informational error field as success', async () => {
        window.activePaymentHash = 'buy-hash-123';

        global.fetch = jest.fn((url) => {
            if (url === '/api/subscription/claim') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        error: 'Deprecation notice: this field is informational only'
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.claimSubscription('import');

        const invoiceBox = document.getElementById('invoice-box-import');
        expect(invoiceBox.textContent).toContain('Installation Complete!');
        expect(invoiceBox.textContent).not.toContain('Provisioning Error');
    });

    test('restart failure keeps install section visible for retry and preserves payment hash', async () => {
        window.activePaymentHash = 'buy-hash-123';
        document.getElementById('pending-install-section').classList.remove('hidden');

        global.fetch = jest.fn((url) => {
            if (url === '/api/subscription/claim') {
                return Promise.resolve({
                    json: () => Promise.resolve({ success: true }),
                    ok: true
                });
            }
            if (url === '/api/local/restart') {
                return Promise.resolve({
                    json: () => Promise.resolve({ error: 'restart failed' }),
                    ok: false
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.claimSubscription('import');

        const invoiceBox = document.getElementById('invoice-box-import');
        const restartBtn = Array.from(invoiceBox.querySelectorAll('button'))
            .find((btn) => btn.textContent.includes('Restart Apps & Tunnel'));
        expect(restartBtn).toBeTruthy();

        restartBtn.click();
        await new Promise(process.nextTick);

        expect(document.getElementById('pending-install-section').classList.contains('hidden')).toBe(false);
        expect(window.activePaymentHash).toBe('buy-hash-123');
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

        // Button remains disabled while invoice is active to prevent duplicate invoices
        const createBtn = document.getElementById('btn-create-buy');
        expect(createBtn.disabled).toBe(true);
        expect(createBtn.textContent).toBe('Invoice Active...');
    });

    test('surfaces backend errors for createSub', async () => {
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
                    json: () => Promise.resolve({ error: 'Upstream unavailable' }),
                    ok: false
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.fetchServers();
        await window.createSub('buy');

        const errEl = document.getElementById('purchase-error-buy');
        expect(errEl).toBeTruthy();
        expect(errEl.textContent).toContain('Upstream unavailable');

        const createBtn = document.getElementById('btn-create-buy');
        expect(createBtn.disabled).toBe(false);
        expect(createBtn.textContent).toBe('Generate Lightning Invoice');
    });

    test('resets active invoice state if post-fetch UI setup throws', async () => {
        if (window.pollInterval) clearInterval(window.pollInterval);
        window.pollInterval = null;
        window.activePaymentHash = null;
        await window.fetchServers();
        jest.spyOn(window, 'renderQR').mockImplementation(() => {
            throw new Error('QR render failed');
        });

        await window.createSub('buy');

        expect(window.activePaymentHash).toBeNull();
        expect(window.pollInterval).toBeFalsy();

        const errEl = document.getElementById('purchase-error-buy');
        expect(errEl).toBeTruthy();
        expect(errEl.textContent).toContain('QR render failed');

        const createBtn = document.getElementById('btn-create-buy');
        expect(createBtn.disabled).toBe(false);
        expect(createBtn.textContent).toBe('Generate Lightning Invoice');
    });

    test('does not clear pre-existing poll interval if setup fails before creating new interval', async () => {
        await window.fetchServers();
        const clearSpy = jest.spyOn(window, 'clearInterval');

        window.activePaymentHash = 'existing-hash';
        window.purchaseMode = 'renew';
        window.pollInterval = 98765;

        jest.spyOn(window, 'renderQR').mockImplementation(() => {
            throw new Error('QR render failed');
        });

        await window.createSub('buy');

        expect(clearSpy).not.toHaveBeenCalledWith(98765);
        expect(window.pollInterval).toBe(98765);
        expect(window.activePaymentHash).toBe('existing-hash');
        expect(window.purchaseMode).toBe('renew');
    });

    test('rolls back mode state when setup fails even if payment hash is reused', async () => {
        await window.fetchServers();

        // Existing renew invoice is active before creating a buy invoice.
        window.activePaymentHash = 'hash-xyz-123';
        window.purchaseMode = 'renew';

        jest.spyOn(window, 'renderQR').mockImplementation(() => {
            throw new Error('QR render failed');
        });

        await window.createSub('buy');

        // Existing active invoice context must remain unchanged.
        expect(window.activePaymentHash).toBe('hash-xyz-123');
        expect(window.purchaseMode).toBe('renew');

        // Buy button must stay usable because buy has no active invoice.
        const buyBtn = document.getElementById('btn-create-buy');
        expect(buyBtn.disabled).toBe(false);
        expect(buyBtn.textContent).toBe('Generate Lightning Invoice');
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

    test('renew failure does not lock button when only buy invoice is active', async () => {
        // Existing buy invoice is active in global state.
        window.activePaymentHash = 'buy-hash-123';
        window.purchaseMode = 'buy';

        // Load renew metadata so createSub('renew') can proceed.
        window.switchTab('renew');
        await new Promise(process.nextTick);

        // Force renew API failure response.
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/meta') {
                return Promise.resolve({
                    json: () => Promise.resolve({ serverId: 'ch-zrh', wgPublicKey: 'pubkey789' }),
                    ok: true
                });
            }
            if (url === '/api/subscription/renew') {
                return Promise.resolve({
                    json: () => Promise.resolve({ error: 'Renew endpoint unavailable' }),
                    ok: false
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.createSub('renew');

        // Renew button must remain usable after failed renew attempt.
        const renewBtn = document.getElementById('btn-create-renew');
        expect(renewBtn.disabled).toBe(false);
        expect(renewBtn.textContent).toBe('Generate Renewal Invoice');

        // Existing buy invoice state remains intact and uncorrupted.
        expect(window.activePaymentHash).toBe('buy-hash-123');
        expect(window.purchaseMode).toBe('buy');

        const errEl = document.getElementById('purchase-error-renew');
        expect(errEl).toBeTruthy();
        expect(errEl.textContent).toContain('Renew endpoint unavailable');
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
        document.getElementById('txt-configs').textContent = 'None Detected';
        global.fetch.mockClear();

        await window.importConfig();

        const msg = document.getElementById('import-msg').textContent;
        expect(msg).toContain('Please paste a WireGuard config');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('pre-validation rejects config missing [Peer] block', async () => {
        document.getElementById('config-text').value = '[Interface]\nPrivateKey = abc\n';
        document.getElementById('txt-configs').textContent = 'None Detected';
        global.fetch.mockClear();

        await window.importConfig();

        const msg = document.getElementById('import-msg').textContent;
        expect(msg).toContain('Missing [Interface] or [Peer] block');
        expect(global.fetch).not.toHaveBeenCalled();
    });

    test('import sends JSON payload and renders success message', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\nEndpoint = de2.tunnelsats.com:51820\n';
        const expectedConfig = config.trim();
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').textContent = 'None Detected';
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
        const msg = document.getElementById('import-msg').textContent;
        expect(msg).toContain('Configuration saved and parsed.');
    });

    test('existing config cancel keeps import local and does not call backend', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\n';
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').textContent = 'tunnelsats.conf';
        global.fetch.mockClear();

        const importPromise = window.importConfig();
        const modal = document.getElementById('import-overwrite-modal');
        expect(modal).toBeTruthy();

        const cancelBtn = Array.from(modal.querySelectorAll('button')).find((btn) => btn.textContent === 'Cancel');
        cancelBtn.click();
        await importPromise;

        expect(document.getElementById('import-overwrite-modal')).toBeNull();
        expect(global.fetch).not.toHaveBeenCalled();
        expect(document.getElementById('import-msg').textContent).toContain('Import cancelled.');
    });

    test('existing config confirm proceeds with upload request', async () => {
        const config = '[Interface]\nPrivateKey = abc\n\n[Peer]\nPublicKey = def\nEndpoint = de2.tunnelsats.com:51820\n';
        const expectedConfig = config.trim();
        document.getElementById('config-text').value = config;
        document.getElementById('txt-configs').textContent = 'tunnelsats.conf';
        global.fetch.mockClear();

        const importPromise = window.importConfig();
        const modal = document.getElementById('import-overwrite-modal');
        expect(modal).toBeTruthy();

        const confirmBtn = Array.from(modal.querySelectorAll('button')).find((btn) => btn.textContent === 'Import Anyway');
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
        document.getElementById('txt-configs').textContent = 'None Detected';

        await window.importConfig();

        const msg = document.getElementById('import-msg').textContent;
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
        // Mock the restart confirmation modal to always confirm by default
        window.confirmRestartModal = jest.fn().mockResolvedValue(true);
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

        expect(window.confirmRestartModal).toHaveBeenCalledWith('lnd');
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/configure-node',
            expect.objectContaining({
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ nodeType: 'lnd' })
            })
        );
        expect(document.getElementById('configure-node-msg').textContent).toContain('de2.tunnelsats.com:35825');
    });

    test('configureNode supports cln nodeType payload', async () => {
        window.setNodeType('cln');
        await window.configureNode();

        expect(window.confirmRestartModal).toHaveBeenCalledWith('cln');
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

        expect(window.confirmRestartModal).toHaveBeenCalledWith('Lightning');
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/local/restore-node',
            expect.objectContaining({ method: 'POST' })
        );
        const msg = document.getElementById('restore-node-msg').textContent;
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

        const msg = document.getElementById('restore-node-msg').textContent;
        expect(msg).toContain('LND: config not found');
        expect(msg).toContain('CLN: config not found');
    });

    test('configureNode handles manual_mode and renders instructions modal', async () => {
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/configure-node') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        manual_mode: true,
                        node_type: 'lnd',
                        config_path: '/path/to/lnd.conf',
                        config_lines: ['line1', 'line2']
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.configureNode();

        const modal = document.getElementById('manual-config-modal');
        expect(modal).toBeTruthy();
        expect(modal.textContent).toContain('Manual Setup Required');
        expect(modal.textContent).toContain('/path/to/lnd.conf');
        expect(modal.textContent).toContain('line1\nline2');

        // Close modal
        const doneBtn = Array.from(modal.querySelectorAll('button')).find(btn => btn.textContent === 'Done');
        doneBtn.click();
        await new Promise(resolve => setTimeout(resolve, 350));
        expect(document.getElementById('manual-config-modal')).toBeNull();
    });

    test('restoreNode handles manual_mode and renders manual restore modal', async () => {
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/restore-node') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        manual_mode: true,
                        restore: true,
                        targets: [
                            {
                                node_type: 'lnd',
                                config_path: '/path/to/lnd.conf',
                                config_lines: ['remove1', 'remove2']
                            }
                        ]
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.restoreNode();

        const modal = document.getElementById('manual-restore-modal');
        expect(modal).toBeTruthy();
        expect(modal.textContent).toContain('Manual Restore Required');
        expect(modal.textContent).toContain('/path/to/lnd.conf');
        expect(modal.textContent).toContain('remove1');

        // Close modal
        const doneBtn = Array.from(modal.querySelectorAll('button')).find(btn => btn.textContent === 'Done');
        doneBtn.click();
        await new Promise(resolve => setTimeout(resolve, 350));
        expect(document.getElementById('manual-restore-modal')).toBeNull();
    });

    test('restoreNode handles manual_mode with empty targets gracefully', async () => {
        global.fetch = jest.fn((url) => {
            if (url === '/api/local/restore-node') {
                return Promise.resolve({
                    json: () => Promise.resolve({
                        success: true,
                        manual_mode: true,
                        restore: true,
                        targets: []
                    }),
                    ok: true
                });
            }
            return Promise.resolve({ json: () => Promise.resolve({}), ok: true });
        });

        await window.restoreNode();

        const modal = document.getElementById('manual-restore-modal');
        expect(modal).toBeNull();
        const msg = document.getElementById('restore-node-msg');
        expect(msg.textContent).toContain('No active Lightning nodes detected for manual restore.');
    });

// Removed pollReconcileStatus tests
});

describe('NWC Auto-Renew Features', () => {
    beforeEach(() => {
        setupDOM();
        evalScript();
        // Mock clipboard API
        Object.defineProperty(navigator, 'clipboard', {
            value: {
                writeText: jest.fn().mockImplementation(() => Promise.resolve()),
            },
            configurable: true
        });
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('fetchStatus updates NWC IP suffix element', async () => {
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({
                    wg_status: 'Connected',
                    vpn_internal_ip: '10.9.0.100',
                    configs_found: [],
                    target_container: 'lnd'
                }),
                ok: true
            })
        );

        await window.fetchStatus();
        expect(document.getElementById('renew-ip-suffix').textContent).toBe('.100');
    });

    test('copy buttons trigger clipboard API with correct values', async () => {
        const writeTextSpy = jest.spyOn(navigator.clipboard, 'writeText');
        
        // Mock public key
        document.getElementById('renew-pubkey').value = 'test-pubkey-abc';
        document.getElementById('btn-copy-pubkey').click();
        expect(writeTextSpy).toHaveBeenCalledWith('test-pubkey-abc');

        // Mock IP suffix
        document.getElementById('renew-ip-suffix').textContent = '.100';
        document.getElementById('btn-copy-ip').click();
        expect(writeTextSpy).toHaveBeenCalledWith('100');
    });

    test('NWC promo card links to correct FAQ', () => {
        const promoLink = document.querySelector('a[href*="nwc-renewals-work"]');
        expect(promoLink).toBeTruthy();
        expect(promoLink.getAttribute('target')).toBe('_blank');
    });
});

describe('Subscription Renewal Fixes', () => {
    beforeEach(() => {
        setupDOM();
        global.fetch = jest.fn(() =>
            Promise.resolve({
                json: () => Promise.resolve({ 
                    status: 'paid', 
                    subscription: { expiresAt: '2027-04-10T12:00:00Z' },
                    wg_status: 'Connected',
                    configs_found: [],
                    version: 'v3.0.0'
                }),
                ok: true
            })
        );
        evalScript();
    });

    afterEach(() => { jest.restoreAllMocks(); });

    test('switchTab("dashboard") triggers fetchStatus', () => {
        const fetchStatusSpy = jest.spyOn(window, 'fetchStatus');
        window.switchTab('dashboard');
        expect(fetchStatusSpy).toHaveBeenCalled();
    });

    test('switchTab("dashboard") clears polling before fetchStatus', () => {
        const clearSpy = jest.spyOn(window, 'clearInterval').mockImplementation(() => {});
        const fetchStatusSpy = jest.spyOn(window, 'fetchStatus').mockImplementation(() => Promise.resolve());
        window.pollInterval = 12345;

        window.switchTab('dashboard');

        expect(clearSpy).toHaveBeenCalledWith(12345);
        expect(window.pollInterval).toBeNull();
        expect(clearSpy.mock.invocationCallOrder[0]).toBeLessThan(fetchStatusSpy.mock.invocationCallOrder[0]);
    });

    test('pollPayment for renew triggers fetchStatus on success', async () => {
        const fetchStatusSpy = jest.spyOn(window, 'fetchStatus');
        window.activePaymentHash = 'renew-hash-123';
        window.purchaseMode = 'renew';
        
        const invoiceBox = document.getElementById('invoice-box-renew');
        invoiceBox.classList.remove('hidden');
        
        await window.pollPayment();
        
        expect(fetchStatusSpy).toHaveBeenCalled();
        expect(invoiceBox.textContent).toContain('Renewal Successful');
    });
});
