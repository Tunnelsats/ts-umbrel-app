const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.resolve(__dirname, '../index.html'), 'utf8');
const script = fs.readFileSync(path.resolve(__dirname, '../js/app.js'), 'utf8');

describe('UI Routing and Initialization', () => {
    beforeEach(() => {
        // Setup simple DOM
        document.documentElement.innerHTML = html.toString();
        // Mock fetch globally
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
        // Execute the script
        window.eval(script);
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('switchTab changes tab visibility correctly', () => {
        // Assert initial state (dashboard visible by default from HTML, but let's test switching)
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
