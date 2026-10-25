/**
 * @jest-environment jsdom
 */

const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.resolve(__dirname, '../index.html'), 'utf8');
const script = fs.readFileSync(path.resolve(__dirname, '../js/app.js'), 'utf8');

describe('TunnelSats v3.5.0 Frontend UI Suite', () => {
    let mockGlobeInstance;

    beforeEach(() => {
        jest.useFakeTimers();
        document.documentElement.innerHTML = html;

        global.fetch = jest.fn((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({
                        authenticated: true,
                        csrf_token: 'test-csrf-token-v350'
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: () => Promise.resolve({})
            });
        });

        global.QRCode = jest.fn().mockImplementation(() => ({
            clear: jest.fn(),
            makeCode: jest.fn()
        }));

        mockGlobeInstance = {
            backgroundColor: jest.fn().mockReturnThis(),
            showAtmosphere: jest.fn().mockReturnThis(),
            atmosphereColor: jest.fn().mockReturnThis(),
            atmosphereAltitude: jest.fn().mockReturnThis(),
            globeImageUrl: jest.fn().mockReturnThis(),
            bumpImageUrl: jest.fn().mockReturnThis(),
            pointsData: jest.fn().mockReturnThis(),
            pointAltitude: jest.fn().mockReturnThis(),
            pointColor: jest.fn().mockReturnThis(),
            pointRadius: jest.fn().mockReturnThis(),
            ringsData: jest.fn().mockReturnThis(),
            ringColor: jest.fn().mockReturnThis(),
            ringMaxRadius: jest.fn().mockReturnThis(),
            ringPropagationSpeed: jest.fn().mockReturnThis(),
            ringRepeatPeriod: jest.fn().mockReturnThis(),
            labelsData: jest.fn().mockReturnThis(),
            labelLat: jest.fn().mockReturnThis(),
            labelLng: jest.fn().mockReturnThis(),
            labelText: jest.fn().mockReturnThis(),
            labelSize: jest.fn().mockReturnThis(),
            labelDotRadius: jest.fn().mockReturnThis(),
            labelColor: jest.fn().mockReturnThis(),
            labelResolution: jest.fn().mockReturnThis(),
            controls: jest.fn().mockReturnValue({ autoRotate: false, autoRotateSpeed: 0, enableZoom: false }),
            pointOfView: jest.fn().mockReturnThis(),
            width: jest.fn().mockReturnThis(),
            height: jest.fn().mockReturnThis()
        };
        global.Globe = jest.fn(() => jest.fn(() => mockGlobeInstance));

        window.eval(script);
    });

    afterEach(() => {
        if (window.pollInterval) clearInterval(window.pollInterval);
        if (window.subscriptionCountdownInterval) clearInterval(window.subscriptionCountdownInterval);
        if (window.invoiceExpiryInterval) clearInterval(window.invoiceExpiryInterval);
        jest.clearAllTimers();
        jest.useRealTimers();
        jest.restoreAllMocks();
    });

    test('1. Compact nested 3D Globe widget lives inside #view-buy and updates #globe-selected-caption', () => {
        const globeContainer = document.getElementById('globe-container');
        const buySection = document.getElementById('view-buy');
        const widgetCard = document.getElementById('globe-widget-card');
        expect(globeContainer).not.toBeNull();
        expect(widgetCard).not.toBeNull();
        expect(buySection.contains(globeContainer)).toBe(true);
        expect(globeContainer.className).not.toContain('inset-0');

        window.updateGlobeMarker({ lat: 47.37, lng: 8.54, flag: '🇨🇭', country: 'Switzerland', city: 'Zurich' });
        const caption = document.getElementById('globe-selected-caption');
        expect(caption.textContent).toContain('Switzerland');
        expect(caption.textContent).toContain('Zurich');
    });

    test('2. formatSubscriptionCountdown formats >=24h as Xd Yh, <24h as HH:MM:SS, and <=0 as Expired', () => {
        expect(window.formatSubscriptionCountdown(0)).toBe('Expired');
        expect(window.formatSubscriptionCountdown(-5000)).toBe('Expired');
        expect(window.formatSubscriptionCountdown((5 * 24 + 6) * 3600 * 1000)).toBe('5d 6h');
        expect(window.formatSubscriptionCountdown((3 * 3600 + 7 * 60 + 9) * 1000)).toBe('03:07:09');
    });

    test('3. Subscription Timeline SVG ring updates phase badge, stroke-dashoffset, and countdown across phases', () => {
        const ring = document.getElementById('subscription-ring-progress');
        const badge = document.getElementById('subscription-phase-badge');
        const countdown = document.getElementById('subscription-countdown');

        // 15 days remaining -> On track
        const in15Days = new Date(Date.now() + 15 * 24 * 3600 * 1000).toISOString();
        window.renderSubscriptionTimeline({ expires_at: in15Days, subscription_telemetry: { plan_days: 30 } });
        expect(badge.textContent).toBe('On track');
        expect(Number(ring.getAttribute('stroke-dashoffset'))).toBeCloseTo(157.08, 0);
        expect(countdown.textContent).toMatch(/1[45]d/);

        // 5 days remaining -> Renew soon
        const in5Days = new Date(Date.now() + 5 * 24 * 3600 * 1000).toISOString();
        window.renderSubscriptionTimeline({ expires_at: in5Days, subscription_telemetry: { plan_days: 30 } });
        expect(badge.textContent).toBe('Renew soon');

        // 2 days remaining -> Renew now
        const in2Days = new Date(Date.now() + 2 * 24 * 3600 * 1000).toISOString();
        window.renderSubscriptionTimeline({ expires_at: in2Days, subscription_telemetry: { plan_days: 30 } });
        expect(badge.textContent).toBe('Renew now');

        // Expired -> Expired
        const pastDate = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
        window.renderSubscriptionTimeline({ expires_at: pastDate });
        expect(badge.textContent).toBe('Expired');
        expect(countdown.textContent).toBe('Expired');
        expect(ring.getAttribute('stroke-dashoffset')).toBe('314.16');
    });

    test('4. Monthly Bandwidth SVG arc gauge calculates stroke-dashoffset, status badge, and pace marker coordinates', () => {
        const arcFill = document.getElementById('bandwidth-arc-fill');
        const paceMarker = document.getElementById('bandwidth-pace-marker');
        const badge = document.getElementById('bandwidth-status-badge');
        const usedText = document.getElementById('bandwidth-used-text');
        const pctText = document.getElementById('bandwidth-pct-text');

        // 50% used, 50% cycle elapsed -> Normal, top of arc (cx=70, cy=14)
        window.renderBandwidthGauge({
            subscription_telemetry: {
                bandwidth: {
                    used_gb: 45,
                    quota_gb: 90,
                    used_pct: 50,
                    cycle_elapsed_pct: 50,
                    days_until_reset: 15,
                    next_reset_at: '2026-11-01T00:00:00Z'
                }
            }
        });
        expect(badge.textContent).toBe('Normal');
        expect(pctText.textContent).toBe('50%');
        expect(usedText.textContent).toBe('45.00 / 90 GB');
        expect(Number(arcFill.getAttribute('stroke-dashoffset'))).toBeCloseTo(91.11, 1);
        expect(paceMarker.classList.contains('hidden')).toBe(false);
        expect(Number(paceMarker.getAttribute('cx'))).toBeCloseTo(70, 1);
        expect(Number(paceMarker.getAttribute('cy'))).toBeCloseTo(14, 1);

        // 75% used -> High Usage
        window.renderBandwidthGauge({
            subscription_telemetry: { bandwidth: { used_gb: 67.5, quota_gb: 90, used_pct: 75 } }
        });
        expect(badge.textContent).toBe('High Usage');

        // 95% used -> Critical
        window.renderBandwidthGauge({
            subscription_telemetry: { bandwidth: { used_gb: 85.5, quota_gb: 90, used_pct: 95 } }
        });
        expect(badge.textContent).toBe('Critical');
    });

    test('5. Actionable notice banners render for expiring subscription and high bandwidth', () => {
        const in2Days = new Date(Date.now() + 2 * 24 * 3600 * 1000).toISOString();
        window.renderNotices({
            expires_at: in2Days,
            subscription_telemetry: {
                bandwidth: { used_gb: 72, quota_gb: 90, used_pct: 80 }
            },
            nwc: { connected: false }
        });

        const region = document.getElementById('notice-region');
        expect(region.classList.contains('hidden')).toBe(false);
        expect(region.querySelector('[data-notice-id="expiring_3d"]')).not.toBeNull();
        expect(region.querySelector('[data-notice-id="bandwidth_70"]')).not.toBeNull();
    });

    test('6. Notice suppression: healthy NWC auto-renewal suppresses 7d and 3d expiry notices but keeps <24h and bandwidth notices', () => {
        const in2Days = new Date(Date.now() + 2 * 24 * 3600 * 1000).toISOString();
        window.renderNotices({
            expires_at: in2Days,
            subscription_telemetry: {
                bandwidth: { used_gb: 82, quota_gb: 90, used_pct: 91 }
            },
            nwc: { connected: true, healthy: true, renewal_duration: '3m' }
        });

        const region = document.getElementById('notice-region');
        expect(region.querySelector('[data-notice-id="expiring_3d"]')).toBeNull();
        expect(region.querySelector('[data-notice-id="expiring_7d"]')).toBeNull();
        expect(region.querySelector('[data-notice-id="bandwidth_90"]')).not.toBeNull();

        // <24h is never suppressed even with healthy NWC
        const in6Hours = new Date(Date.now() + 6 * 3600 * 1000).toISOString();
        window.renderNotices({
            expires_at: in6Hours,
            nwc: { connected: true, healthy: true }
        });
        expect(region.querySelector('[data-notice-id="expiring_24h"]')).not.toBeNull();
    });

    test('7. NWC failure and stale telemetry notices render with actionable CTAs', () => {
        window.renderNotices({
            subscription_telemetry: { stale: true },
            nwc: { connected: true, healthy: false, last_error: 'Insufficient budget' }
        });

        const region = document.getElementById('notice-region');
        const nwcBanner = region.querySelector('[data-notice-id="nwc_failed"]');
        const staleBanner = region.querySelector('[data-notice-id="telemetry_stale"]');
        expect(nwcBanner).not.toBeNull();
        expect(nwcBanner.textContent).toContain('Insufficient budget');
        expect(staleBanner).not.toBeNull();
    });

    test('8. Paid Bandwidth Reset ($1) opens confirmation modal when usage < 70% and requests invoice when confirmed', async () => {
        window.latestStatusData = {
            subscription_telemetry: {
                bandwidth: { used_gb: 20, quota_gb: 90, used_pct: 22, next_reset_at: '2026-11-01T00:00:00Z' }
            }
        };

        window.handleBandwidthResetClick();
        const modal = document.getElementById('reset-confirm-modal');
        expect(modal.classList.contains('hidden')).toBe(false);
        expect(document.getElementById('reset-confirm-text').textContent).toContain('22%');

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-reset' })
                });
            }
            if (url === '/api/subscription/bandwidth-reset') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({
                        paymentHash: 'reset_hash_123',
                        invoice: 'lnbc10u1p_reset_invoice'
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        });

        await window.confirmBandwidthReset();

        expect(modal.classList.contains('hidden')).toBe(true);
        const resetBox = document.getElementById('bandwidth-reset-invoice-box');
        expect(resetBox.classList.contains('hidden')).toBe(false);
        expect(document.getElementById('invoice-bolt11-reset').value).toBe('lnbc10u1p_reset_invoice');
    });

    test('9. Server selection groups servers by region, disables non-online servers, and auto-selects first online server', async () => {
        global.fetch.mockResolvedValueOnce({
            ok: true,
            status: 200,
            json: () => Promise.resolve([
                { id: 'de1', country: 'Germany', city: 'Frankfurt', flag: '🇩🇪', status: 'full' },
                { id: 'ch1', country: 'Switzerland', city: 'Zurich', flag: '🇨🇭', status: 'online' },
                { id: 'us1', country: 'USA', city: 'Ashburn', flag: '🇺🇸', status: 'online' },
                { id: 'br1', country: 'Brazil', city: 'Sao Paulo', flag: '🇧🇷', status: 'maintenance' },
                { id: 'jp1', country: 'Japan', city: 'Tokyo', flag: '🇯🇵', status: 'online' }
            ])
        });

        await window.fetchServers();

        const list = document.getElementById('buy-server-list');
        const buttons = list.querySelectorAll('button');
        expect(buttons.length).toBe(5);
        expect(buttons[0].disabled).toBe(true);
        expect(buttons[1].disabled).toBe(false);
        expect(buttons[3].disabled).toBe(true);

        const groups = list.querySelectorAll('.server-region-group');
        const regions = Array.from(groups).map((g) => g.dataset.region);
        expect(regions).toEqual(['Europe', 'North America', 'Latin America', 'Asia-Pacific']);

        // Auto-selected first online server (ch1, since de1 is full)
        expect(document.getElementById('buy-server-select').value).toBe('ch1');
    });

    test('10. Duration choice pills render 4 options with -5%, -10%, -20% discount badges and effective monthly pricing', () => {
        window.renderDurations();
        const buyList = document.getElementById('buy-duration-list');
        const pills = buyList.querySelectorAll('button[role="radio"]');
        expect(pills.length).toBe(4);
        expect(pills[0].textContent).toContain('$2.89');
        expect(pills[1].textContent).toContain('-5%');
        expect(pills[1].textContent).toContain('$8.24');
        expect(pills[2].textContent).toContain('-10%');
        expect(pills[2].textContent).toContain('$15.61');
        expect(pills[3].textContent).toContain('-20%');
        expect(pills[3].textContent).toContain('$27.74');

        pills[2].click();
        expect(document.getElementById('buy-duration-select').value).toBe('6');
        expect(pills[2].getAttribute('aria-checked')).toBe('true');
    });

    test('11. Referral code is sent in createSub(buy) and claimSubscription(import) when provided', async () => {
        document.getElementById('buy-server-select').value = 'ch1';
        document.getElementById('buy-duration-select').value = '3';
        document.getElementById('buy-referral-code').value = '  SATOSHI21  ';

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-ref' })
                });
            }
            if (url === '/api/subscription/create') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ paymentHash: 'hash_ref_1', invoice: 'lnbc_ref_1' })
                });
            }
            if (url === '/api/subscription/claim') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ success: true })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        });

        await window.createSub('buy');
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/subscription/create',
            expect.objectContaining({
                method: 'POST',
                body: JSON.stringify({ serverId: 'ch1', duration: 3, referralCode: 'SATOSHI21' })
            })
        );

        await window.claimSubscription('import');
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/subscription/claim',
            expect.objectContaining({
                method: 'POST',
                body: JSON.stringify({
                    paymentHash: 'hash_ref_1',
                    wgPublicKey: '',
                    wgPresharedKey: '',
                    referralCode: 'SATOSHI21'
                })
            })
        );
    });

    test('12. NWC modal updates recommended +20% wallet budget and connects/disconnects wallet', async () => {
        window.openNwcModal();
        const modal = document.getElementById('nwc-modal');
        expect(modal.classList.contains('hidden')).toBe(false);

        window.selectNwcDuration('12m');
        expect(document.getElementById('nwc-duration-select').value).toBe('12m');
        expect(document.getElementById('nwc-recommended-budget').textContent).toContain('$33.29 (+20% buffer)');

        // Toggle password visibility
        const uriInput = document.getElementById('nwc-uri-input');
        expect(uriInput.type).toBe('password');
        window.toggleNwcUriVisibility();
        expect(uriInput.type).toBe('text');

        uriInput.value = 'nostr+walletconnect://pubkey123?relay=wss://relay.example.com&secret=sec123';
        document.getElementById('nwc-route-tor').checked = true;

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-nwc' })
                });
            }
            if (url === '/api/nwc/connect') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ success: true, message: 'Connected' })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ vpn_active: true }) });
        });

        await window.connectNwcWallet();
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/nwc/connect',
            expect.objectContaining({
                method: 'POST',
                body: JSON.stringify({
                    nwcUri: 'nostr+walletconnect://pubkey123?relay=wss://relay.example.com&secret=sec123',
                    renewalDuration: '12m',
                    routeViaTor: true
                })
            })
        );
    });

    test('13. Inbound Reachability Probe handles reachable, unreachable, and skipped responses', async () => {
        const resultEl = document.getElementById('reachability-result');

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-reach' })
                });
            }
            if (url === '/api/reachability') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({
                        reachable: true,
                        endpoint: 'ch1.tunnelsats.com:35825',
                        rtt_ms: 42
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        });

        await window.testReachability();
        expect(resultEl.textContent).toContain('Reachable: ch1.tunnelsats.com:35825 (42 ms)');

        // Skipped state
        global.fetch.mockImplementation((url) => {
            if (url === '/api/reachability') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ status: 'skipped', reason: 'Tunnel is down' })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-reach' }) });
        });
        await window.testReachability();
        expect(resultEl.textContent).toContain('Skipped: Tunnel is down');
    });

    test('14. Pure-DOM SVG QR renders real matrix when QRCode model is present and honest unavailable notice when absent', () => {
        // 1. With QRCode model providing getModuleCount/isDark -> renders real matrix SVG
        global.QRCode = function () {
            this._oQRCode = {
                getModuleCount: () => 21,
                isDark: (r, c) => (r + c) % 2 === 0
            };
        };
        const matrixSvg = window.createPureDomQrSvg('lnbc100n1p_real_matrix', 192);
        expect(matrixSvg.getAttribute('data-qr-mode')).toBe('matrix');
        expect(matrixSvg.getAttribute('viewBox')).toBe('0 0 25 25');

        // 2. Without QRCode library -> renders honest unavailable notice instead of fake QR
        delete global.QRCode;
        window.renderQR('buy', 'lnbc100n1p_fallback_test');
        const box = document.getElementById('qr-placeholder-buy');
        const svg = box.querySelector('svg');
        expect(svg).not.toBeNull();
        expect(svg.getAttribute('data-qr-fallback')).toBe('unavailable');
        expect(svg.textContent).toContain('QR Code Unavailable');

        // 3. Numeric saved NWC duration normalizes to pill format (e.g. 12 -> 12m)
        window.selectNwcDuration(12);
        const pill12m = document.querySelector('#nwc-duration-pills button[data-nwc-duration="12m"]');
        expect(pill12m.getAttribute('aria-checked')).toBe('true');
        expect(document.getElementById('nwc-recommended-budget').textContent).toContain('$33.29');
    });

    test('15. disconnectNwcWallet sends POST /api/nwc/disconnect and updates NWC promo card', async () => {
        window.renderNwcStatusUI({
            nwc: {
                connected: true,
                healthy: true,
                renewal_duration: '6m'
            }
        });

        expect(document.getElementById('nwc-status-badge').textContent).toBe('Active');
        expect(document.getElementById('nwc-active-duration').textContent).toContain('Plan: 6m');

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-disc' })
                });
            }
            if (url === '/api/nwc/disconnect') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ success: true })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ vpn_active: false }) });
        });

        await window.disconnectNwcWallet();
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/nwc/disconnect',
            expect.objectContaining({ method: 'POST' })
        );
    });

    test('16. payWithNwc sends POST /api/nwc/pay with invoice and mode', async () => {
        document.getElementById('invoice-bolt11-buy').value = 'lnbc2890n1p_nwc_pay_test';

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-nwcpay' })
                });
            }
            if (url === '/api/nwc/pay') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ success: true, paid: true, preimage: 'preimage_123' })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        });

        await window.payWithNwc('buy');
        expect(global.fetch).toHaveBeenCalledWith(
            '/api/nwc/pay',
            expect.objectContaining({
                method: 'POST',
                body: expect.stringContaining('"invoice":"lnbc2890n1p_nwc_pay_test"')
            })
        );
    });

    test('17. payWithWebln calls window.webln.enable() and window.webln.sendPayment(bolt11)', async () => {
        document.getElementById('invoice-bolt11-renew').value = 'lnbc8240n1p_webln_test';
        window.webln = {
            enable: jest.fn().mockResolvedValue(true),
            sendPayment: jest.fn().mockResolvedValue({ preimage: 'webln_preimage' })
        };

        await window.payWithWebln('renew');
        expect(window.webln.enable).toHaveBeenCalled();
        expect(window.webln.sendPayment).toHaveBeenCalledWith('lnbc8240n1p_webln_test');
        delete window.webln;
    });

    test('18. startInvoiceExpiryTimer updates live countdown and keyboard arrow navigation cycles radiogroup pills', () => {
        window.startInvoiceExpiryTimer('buy', Date.now() + 125 * 1000);
        const timerEl = document.getElementById('invoice-expiry-buy');
        expect(timerEl.textContent).toMatch(/Invoice expires in 02:0[45]/);

        // Keyboard arrow navigation on duration radiogroup
        window.renderDurations();
        const buyList = document.getElementById('buy-duration-list');
        const pills = buyList.querySelectorAll('button[role="radio"]');
        pills[0].focus();

        const arrowEvent = new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true });
        buyList.dispatchEvent(arrowEvent);
        expect(document.getElementById('buy-duration-select').value).toBe('3');
    });

    test('19. updateNwcRecommendedBudget("match") resolves saved 12-month duration ($33.29) and modal text states within 7 days before expiry', () => {
        expect(document.getElementById('nwc-modal').textContent).toContain('within 7 days before expiry');

        window.latestStatusData = {
            nwc: { lastDuration: 12 },
            subscription: { lastDuration: '12m' }
        };
        window.updateNwcRecommendedBudget('match');
        expect(document.getElementById('nwc-recommended-budget').textContent).toContain('$33.29 (+20% buffer)');

        window.latestStatusData = null;
        window.updateNwcRecommendedBudget('match');
        expect(document.getElementById('nwc-recommended-budget').textContent).toContain('$3.47 (+20% buffer)');
    });

    test('20. restorePendingPaymentFlow restores pending_order and pending_reset on startup', async () => {
        const futureIso = new Date(Date.now() + 10 * 60 * 1000).toISOString();

        global.fetch.mockImplementation((url) => {
            if (url === '/api/local/session') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ authenticated: true, csrf_token: 'csrf-restore' })
                });
            }
            if (url === '/api/subscription/order_hash_restore') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ status: 'pending' })
                });
            }
            if (url === '/api/subscription/reset_hash_restore') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () => Promise.resolve({ status: 'paid' })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
        });

        window.activePaymentHash = null;
        await window.restorePendingPaymentFlow({
            pending_order: {
                paymentHash: 'order_hash_restore',
                invoice: 'lnbc_restored_order',
                expiresAt: futureIso
            }
        });

        expect(window.activePaymentHash).toBe('order_hash_restore');
        expect(window.purchaseMode).toBe('buy');
        expect(document.getElementById('invoice-bolt11-buy').value).toBe('lnbc_restored_order');
        expect(document.getElementById('invoice-box-buy').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('btn-create-buy').disabled).toBe(true);

        // Now test pending_reset settling immediately when paid while closed
        window.activePaymentHash = null;
        if (window.pollInterval) clearInterval(window.pollInterval);
        await window.restorePendingPaymentFlow({
            pending_reset: {
                paymentHash: 'reset_hash_restore',
                invoice: 'lnbc_restored_reset',
                expiresAt: futureIso
            }
        });

        expect(document.getElementById('bandwidth-reset-invoice-box').textContent).toContain('Bandwidth Quota Reset!');
        expect(window.activePaymentHash).toBeNull();
    });

    test('21. Expired invoice clears activePaymentHash and does not re-lock btn-create-buy after switchTab', () => {
        window.activePaymentHash = 'expired_hash_1';
        window.purchaseMode = 'buy';
        document.getElementById('btn-create-buy').disabled = true;

        // Pass an already-expired timestamp
        window.startInvoiceExpiryTimer('buy', Date.now() - 5000);

        expect(window.activePaymentHash).toBeNull();
        expect(window.activeInvoiceExpired).toBe(true);
        expect(document.getElementById('btn-create-buy').disabled).toBe(false);
        expect(document.getElementById('invoice-expiry-buy').textContent).toContain('Invoice expired');

        // Switching away and back to buy must NOT re-lock the button or treat the invoice as active
        window.switchTab('dashboard');
        window.switchTab('buy');
        expect(document.getElementById('btn-create-buy').disabled).toBe(false);
        expect(document.getElementById('btn-create-buy').textContent).toBe('Generate Lightning Invoice');
        expect(document.getElementById('invoice-box-buy').classList.contains('hidden')).toBe(true);
    });

    test('22. fetchServers when all servers are offline leaves buy-server-select empty, disables btn-create-buy, and blocks createSub', async () => {
        global.fetch.mockResolvedValueOnce({
            ok: true,
            status: 200,
            json: () => Promise.resolve([
                { id: 'de1', country: 'Germany', city: 'Frankfurt', flag: '🇩🇪', status: 'full' },
                { id: 'br1', country: 'Brazil', city: 'Sao Paulo', flag: '🇧🇷', status: 'maintenance' }
            ])
        });

        await window.fetchServers();

        expect(document.getElementById('buy-server-select').value).toBe('');
        expect(document.getElementById('buy-server-label').textContent).toBe('No online servers available');
        expect(document.getElementById('btn-create-buy').disabled).toBe(true);

        // Even if an offline server ID is forced into the hidden input, createSub rejects it before calling API
        document.getElementById('buy-server-select').value = 'de1';
        global.fetch.mockClear();
        await window.createSub('buy');

        expect(global.fetch).not.toHaveBeenCalledWith('/api/subscription/create', expect.anything());
        const errEl = document.getElementById('purchase-error-buy');
        expect(errEl).not.toBeNull();
        expect(errEl.textContent).toContain('Selected server is currently unavailable');
    });

    test('23. resizeGlobe ignores 0x0 resize when hidden and resizes when switching to buy tab', () => {
        const container = document.getElementById('globe-container');
        mockGlobeInstance.width.mockClear();
        mockGlobeInstance.height.mockClear();

        // While hidden (0x0), resize event must not set width/height to 0
        Object.defineProperty(container, 'offsetWidth', { value: 0, configurable: true });
        Object.defineProperty(container, 'offsetHeight', { value: 0, configurable: true });
        window.dispatchEvent(new window.Event('resize'));
        expect(mockGlobeInstance.width).not.toHaveBeenCalled();
        expect(mockGlobeInstance.height).not.toHaveBeenCalled();

        // When Buy tab becomes visible with non-zero dimensions, switchTab('buy') resizes the globe
        Object.defineProperty(container, 'offsetWidth', { value: 160, configurable: true });
        Object.defineProperty(container, 'offsetHeight', { value: 160, configurable: true });
        window.switchTab('buy');
        expect(mockGlobeInstance.width).toHaveBeenCalledWith(160);
        expect(mockGlobeInstance.height).toHaveBeenCalledWith(160);
    });

    test('24. renderQR routes through createPureDomQrSvg when QRCode provides _oQRCode matrix', () => {
        global.QRCode = function () {
            this._oQRCode = {
                getModuleCount: () => 21,
                isDark: (r, c) => (r + c) % 2 === 0
            };
        };

        window.renderQR('buy', 'lnbc2500n1p_svg_route_test');
        const box = document.getElementById('qr-placeholder-buy');
        const svg = box.querySelector('svg');
        expect(svg).not.toBeNull();
        expect(svg.getAttribute('data-qr-mode')).toBe('matrix');
        expect(svg.getAttribute('width')).toBe('192');
    });
});
