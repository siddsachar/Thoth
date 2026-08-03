(() => {
    'use strict';

    const WINDOWS_URL = 'https://github.com/siddsachar/row-bot/releases/download/v4.5.0/Row-Bot-4.5.0-Windows-x64.exe';
    const MAC_URL = 'https://github.com/siddsachar/row-bot/releases/download/v4.5.0/Row-Bot-4.5.0-macOS-arm64.dmg';
    const LINUX_COMMAND = 'curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash -s -- 4.5.0';
    const DESKTOP_LINK = 'https://row-bot.ai/';
    const CTA_PLACEMENTS = new Set([
        'navigation',
        'hero',
        'platform_selector',
        'final_install',
        'mobile_handoff',
        'product_demo'
    ]);

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function detectDevice(environment = {}) {
        const nav = environment.navigator || window.navigator;
        const media = environment.matchMedia || window.matchMedia.bind(window);
        const screenInfo = environment.screen || window.screen;
        const ua = String(nav.userAgent || '').toLowerCase();
        const platform = String(nav.userAgentData?.platform || nav.platform || '').toLowerCase();
        const touchPoints = Number(nav.maxTouchPoints || 0);
        const coarsePointer = Boolean(media('(pointer: coarse)').matches);
        const shortestScreenEdge = Math.min(
            Number(screenInfo?.width || Number.POSITIVE_INFINITY),
            Number(screenInfo?.height || Number.POSITIVE_INFINITY)
        );

        const isWindows = platform.includes('win') || ua.includes('windows');
        const isIPad = ua.includes('ipad') || (platform === 'macintel' && touchPoints > 1);
        const isIOS = isIPad || /iphone|ipod/.test(ua);
        const isAndroid = ua.includes('android');
        const hasMobileSignal = nav.userAgentData?.mobile === true
            || /mobile|phone|tablet|silk|kindle|webos|blackberry|opera mini|iemobile/.test(ua);
        const isUnknownMobileLike = !isWindows
            && coarsePointer
            && touchPoints > 0
            && shortestScreenEdge <= 1024;

        if (isIOS) return { device: 'mobile', platform: 'ios', name: isIPad ? 'iPadOS' : 'iOS' };
        if (isAndroid) return { device: 'mobile', platform: 'android', name: 'Android' };
        if (hasMobileSignal || isUnknownMobileLike) return { device: 'mobile', platform: 'mobile', name: 'Mobile' };
        if (platform.includes('mac') || ua.includes('macintosh')) return { device: 'desktop', platform: 'macos', name: 'macOS' };
        if (platform.includes('linux') || platform.includes('x11') || ua.includes('linux')) return { device: 'desktop', platform: 'linux', name: 'Linux' };
        return { device: 'desktop', platform: 'windows', name: 'Windows' };
    }

    function isHomepage(locationInfo = window.location) {
        const pathname = String(locationInfo?.pathname || '');
        return pathname === '' || pathname === '/' || pathname.endsWith('/index.html');
    }

    function sectionTarget(section, locationInfo = window.location) {
        return `${isHomepage(locationInfo) ? '' : 'index.html'}#${section}`;
    }

    function platformChoice(platform, locationInfo = window.location) {
        if (platform === 'macos') return { label: 'Download for macOS', name: 'macOS', hint: '.dmg', href: MAC_URL, platform: 'macos' };
        if (platform === 'linux') return { label: 'Install on Linux', name: 'Linux', hint: 'curl', href: sectionTarget('install', locationInfo), platform: 'linux' };
        return { label: 'Download for Windows', name: 'Windows', hint: '.exe', href: WINDOWS_URL, platform: 'windows' };
    }

    function placementFor(element, fallback) {
        const requested = element?.dataset.placement;
        return CTA_PLACEMENTS.has(requested) ? requested : fallback;
    }

    function trackSiteEvent(name, params) {
        if (typeof window.gtag === 'function') window.gtag('event', name, params || {});
    }

    function trackAdsConversion(params) {
        if (typeof window.gtag === 'function' && window.ROW_BOT_GOOGLE_ADS_CONVERSION_ID) {
            window.gtag('event', 'conversion', {
                send_to: window.ROW_BOT_GOOGLE_ADS_CONVERSION_ID,
                ...(params || {})
            });
        }
    }

    function setPrimaryIntent(link, intent, platform) {
        link.removeAttribute('data-desktop-download');
        link.removeAttribute('data-linux-install');
        if (intent === 'download') link.dataset.desktopDownload = platform;
        if (intent === 'linux') link.setAttribute('data-linux-install', '');
    }

    const detectedDevice = detectDevice();
    const selectedPlatform = platformChoice(detectedDevice.platform);
    document.documentElement.dataset.device = detectedDevice.device;
    document.documentElement.dataset.platform = detectedDevice.platform;

    document.querySelectorAll('[data-os-primary]').forEach(link => {
        if (detectedDevice.device === 'mobile') {
            const isNavigation = link.dataset.osLabel === 'short';
            const target = link.dataset.mobileTarget || (isNavigation ? 'install' : 'demos');
            link.href = sectionTarget(target);
            link.textContent = link.dataset.mobileLabel || (isNavigation ? 'Desktop install' : 'See Row-Bot in action');
            setPrimaryIntent(link, 'explore');
            return;
        }

        link.href = selectedPlatform.href;
        setPrimaryIntent(link, selectedPlatform.platform === 'linux' ? 'linux' : 'download', selectedPlatform.platform);
        link.innerHTML = link.dataset.osLabel === 'short'
            ? 'Download'
            : `${selectedPlatform.label} <small>&middot; ${selectedPlatform.hint}</small>`;
    });

    document.querySelectorAll('[data-hero-secondary]').forEach(link => {
        if (detectedDevice.device === 'desktop') {
            link.href = sectionTarget('proof');
            link.innerHTML = 'See Row-Bot in action <span aria-hidden="true">&rarr;</span>';
        }
    });

    document.querySelectorAll('[data-platform-note]').forEach(note => {
        note.textContent = `Detected: ${selectedPlatform.name}. Choose any desktop platform:`;
    });
    document.querySelectorAll('[data-platform-choice]').forEach(link => {
        link.classList.toggle('is-detected', detectedDevice.device === 'desktop' && link.dataset.platformChoice === selectedPlatform.platform);
    });

    document.querySelectorAll('[data-desktop-download]').forEach(link => {
        link.addEventListener('click', event => {
            const platform = link.dataset.desktopDownload;
            const placement = placementFor(link, 'final_install');
            const params = { platform, cta_placement: placement };
            trackSiteEvent('desktop_download_click', params);

            const standardClick = event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
            if (!standardClick || link.target === '_blank') {
                trackAdsConversion(params);
                return;
            }

            event.preventDefault();
            let navigated = false;
            const navigate = () => {
                if (!navigated) {
                    navigated = true;
                    window.location.href = link.href;
                }
            };
            trackAdsConversion({ ...params, event_callback: navigate });
            window.setTimeout(navigate, 700);
        });
    });

    document.querySelectorAll('[data-linux-install]').forEach(link => {
        link.addEventListener('click', () => {
            trackSiteEvent('linux_install_view', {
                platform: 'linux',
                cta_placement: placementFor(link, 'platform_selector')
            });
        });
    });

    document.querySelectorAll('[data-install-docs]').forEach(link => {
        link.addEventListener('click', () => {
            trackSiteEvent('installation_docs_open', {
                cta_placement: placementFor(link, 'final_install')
            });
        });
    });

    async function copyText(text) {
        if (navigator.clipboard?.writeText) {
            try {
                await navigator.clipboard.writeText(text);
                return 'clipboard';
            } catch (_error) {
                // Fall through to the selection-based browser copy path.
            }
        }

        const field = document.createElement('textarea');
        field.value = text;
        field.setAttribute('readonly', '');
        field.style.position = 'fixed';
        field.style.opacity = '0';
        field.style.pointerEvents = 'none';
        document.body.appendChild(field);
        field.select();
        let copied = false;
        try {
            copied = Boolean(document.execCommand?.('copy'));
        } catch (_error) {
            copied = false;
        }
        field.remove();
        return copied ? 'fallback' : null;
    }

    document.querySelectorAll('[data-linux-command]').forEach(command => {
        command.textContent = LINUX_COMMAND;
    });

    document.querySelectorAll('[data-copy-linux]').forEach(button => {
        button.addEventListener('click', async () => {
            const previous = button.textContent;
            const status = button.closest('.linux-box')?.querySelector('[data-linux-status]');
            const method = await copyText(LINUX_COMMAND);
            if (method) {
                button.textContent = 'Copied';
                if (status) status.textContent = 'Linux command copied.';
                trackSiteEvent('linux_command_copy', {
                    copy_method: method,
                    cta_placement: placementFor(button, 'final_install')
                });
            } else {
                const command = button.closest('.linux-box')?.querySelector('[data-linux-command]');
                if (command) {
                    const range = document.createRange();
                    range.selectNodeContents(command);
                    const selection = window.getSelection();
                    selection?.removeAllRanges();
                    selection?.addRange(range);
                }
                button.textContent = 'Select text';
                if (status) status.textContent = 'Command selected. Press Ctrl+C or Command+C.';
            }
            window.setTimeout(() => {
                button.textContent = previous;
                if (status) status.textContent = '';
            }, 2400);
        });
    });

    const shareButton = document.querySelector('[data-share-desktop]');
    const copyDesktopButton = document.querySelector('[data-copy-desktop]');
    const handoffStatus = document.querySelector('[data-handoff-status]');

    if (shareButton && typeof navigator.share === 'function') {
        shareButton.hidden = false;
        shareButton.addEventListener('click', async () => {
            try {
                await navigator.share({
                    title: 'Row-Bot',
                    text: 'Open Row-Bot on your computer to install it.',
                    url: DESKTOP_LINK
                });
                if (handoffStatus) handoffStatus.textContent = 'Desktop link shared.';
                trackSiteEvent('mobile_desktop_link_share', { cta_placement: 'mobile_handoff' });
            } catch (error) {
                if (error?.name !== 'AbortError' && handoffStatus) {
                    handoffStatus.textContent = 'Sharing is unavailable. Use Copy row-bot.ai instead.';
                }
            }
        });
    }

    copyDesktopButton?.addEventListener('click', async () => {
        const method = await copyText(DESKTOP_LINK);
        if (method) {
            if (handoffStatus) handoffStatus.textContent = 'row-bot.ai copied for your desktop.';
            trackSiteEvent('mobile_desktop_link_copy', {
                copy_method: method,
                cta_placement: 'mobile_handoff'
            });
        } else if (handoffStatus) {
            handoffStatus.textContent = 'Copy unavailable. Open row-bot.ai on your computer.';
        }
    });

    const nav = document.querySelector('.site-nav');
    const navToggle = document.querySelector('.nav-hamburger');
    const navMenu = document.querySelector('.nav-menu');
    const compactNav = window.matchMedia('(max-width: 1080px)');
    const setNavOpen = open => {
        const shouldOpen = Boolean(open && compactNav.matches);
        nav?.classList.toggle('is-open', shouldOpen);
        navToggle?.setAttribute('aria-expanded', String(shouldOpen));
        navToggle?.setAttribute('aria-label', shouldOpen ? 'Close navigation' : 'Open navigation');
        if (navMenu) {
            if (compactNav.matches) {
                navMenu.setAttribute('aria-hidden', String(!shouldOpen));
                navMenu.inert = !shouldOpen;
            } else {
                navMenu.removeAttribute('aria-hidden');
                navMenu.inert = false;
            }
        }
    };
    navToggle?.addEventListener('click', () => setNavOpen(!nav?.classList.contains('is-open')));
    document.querySelectorAll('.nav-menu a').forEach(link => {
        link.addEventListener('click', () => setNavOpen(false));
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && nav?.classList.contains('is-open')) {
            setNavOpen(false);
            navToggle?.focus();
        }
    });
    compactNav.addEventListener?.('change', () => setNavOpen(false));
    setNavOpen(false);

    const lightboxes = Array.from(document.querySelectorAll('[data-lightbox]'));
    if (lightboxes.length) {
        const lightboxBackground = Array.from(document.querySelectorAll('body > header, body > main, body > footer'));
        let activeLightbox = null;
        let lightboxTrigger = null;

        const closeLightbox = restoreFocus => {
            if (!activeLightbox) return;
            activeLightbox = null;
            document.documentElement.classList.remove('lightbox-open');
            lightboxBackground.forEach(element => { element.inert = false; });
            if (restoreFocus) lightboxTrigger?.focus();
        };

        const syncLightbox = () => {
            const target = document.getElementById(window.location.hash.slice(1));
            if (!target?.matches('[data-lightbox]')) {
                closeLightbox(true);
                return;
            }

            activeLightbox = target;
            document.documentElement.classList.add('lightbox-open');
            lightboxBackground.forEach(element => { element.inert = true; });
            window.setTimeout(() => target.focus(), 0);
        };

        document.querySelectorAll('[data-lightbox-open]').forEach(trigger => {
            trigger.addEventListener('click', () => { lightboxTrigger = trigger; });
        });
        document.querySelectorAll('[data-lightbox-close]').forEach(close => {
            close.addEventListener('click', () => {
                window.setTimeout(() => closeLightbox(true), 0);
            });
        });
        document.addEventListener('keydown', event => {
            if (!activeLightbox) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                window.location.hash = 'diagrams';
                return;
            }
            if (event.key !== 'Tab') return;
            const focusable = Array.from(activeLightbox.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'));
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && (document.activeElement === first || document.activeElement === activeLightbox)) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
        window.addEventListener('hashchange', syncLightbox);
        syncLightbox();
    }

    const progress = document.querySelector('.site-progress');
    if (progress) {
        const updateProgress = () => {
            const available = document.documentElement.scrollHeight - document.documentElement.clientHeight;
            progress.style.width = `${available > 0 ? Math.min(100, document.documentElement.scrollTop / available * 100) : 0}%`;
        };
        updateProgress();
        window.addEventListener('scroll', updateProgress, { passive: true });
    }

    if (!reduceMotion && 'IntersectionObserver' in window) {
        const revealObserver = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('is-visible');
                    revealObserver.unobserve(entry.target);
                }
            });
        }, { threshold: 0.11 });
        document.querySelectorAll('.reveal').forEach(element => revealObserver.observe(element));
    } else {
        document.querySelectorAll('.reveal').forEach(element => element.classList.add('is-visible'));
    }

    const stage = document.querySelector('.core-stage');
    if (stage && !reduceMotion && window.matchMedia('(pointer: fine)').matches) {
        const visual = stage.closest('.hero-visual');
        visual?.addEventListener('pointermove', event => {
            const bounds = visual.getBoundingClientRect();
            const x = (event.clientX - bounds.left) / bounds.width - 0.5;
            const y = (event.clientY - bounds.top) / bounds.height - 0.5;
            stage.style.setProperty('--ry', `${8 + x * 7}deg`);
            stage.style.setProperty('--rx', `${-4 - y * 5}deg`);
        });
        visual?.addEventListener('pointerleave', () => {
            stage.style.setProperty('--ry', '8deg');
            stage.style.setProperty('--rx', '-4deg');
        });
    }

    function activateVideo(facade, event) {
        event?.preventDefault();
        if (facade.dataset.active === 'true') return;
        const id = facade.dataset.youtube;
        if (!id) return;

        facade.dataset.active = 'true';
        trackSiteEvent('product_demo_open', {
            video_id: id,
            cta_placement: placementFor(facade, 'product_demo')
        });
        const iframe = document.createElement('iframe');
        iframe.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1`;
        iframe.title = facade.getAttribute('aria-label') || 'Row-Bot product video';
        iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture';
        iframe.referrerPolicy = 'strict-origin-when-cross-origin';
        iframe.allowFullscreen = true;
        facade.removeAttribute('href');
        facade.removeAttribute('target');
        facade.replaceChildren(iframe);
    }

    document.querySelectorAll('[data-youtube]').forEach(facade => {
        facade.addEventListener('click', event => activateVideo(facade, event));
        facade.addEventListener('keydown', event => {
            if (event.key === ' ') activateVideo(facade, event);
        });
    });

    document.querySelectorAll('[data-demo-link]').forEach(link => {
        link.addEventListener('click', () => {
            const id = new URL(link.href).searchParams.get('v') || 'external';
            trackSiteEvent('product_demo_open', {
                video_id: id,
                cta_placement: placementFor(link, 'product_demo')
            });
        });
    });

    window.RowBotLanding = Object.freeze({ detectDevice, isHomepage, sectionTarget, platformChoice });
})();
