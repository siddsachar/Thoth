const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const defaultNavigator = {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    platform: 'Win32',
    maxTouchPoints: 0,
};

global.window = {
    navigator: defaultNavigator,
    screen: { width: 1920, height: 1080 },
    location: { href: 'https://row-bot.ai/' },
    matchMedia: () => ({ matches: false }),
    addEventListener() {},
    setTimeout() {},
};
global.navigator = defaultNavigator;
global.document = {
    documentElement: {
        clientHeight: 900,
        dataset: {},
        scrollHeight: 900,
        scrollTop: 0,
    },
    body: {},
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
};
window.document = document;

require(path.resolve(__dirname, '../../docs/site.js'));

const { detectDevice, isHomepage, sectionTarget, platformChoice } = window.RowBotLanding;
const media = coarse => query => ({ matches: query === '(pointer: coarse)' && coarse });
const environment = ({
    userAgent,
    platform,
    touchPoints = 0,
    coarse = false,
    width = 1920,
    height = 1080,
    userAgentData,
}) => ({
    navigator: {
        userAgent,
        platform,
        maxTouchPoints: touchPoints,
        ...(userAgentData ? { userAgentData } : {}),
    },
    matchMedia: media(coarse),
    screen: { width, height },
});

const cases = [
    {
        name: 'Windows desktop',
        input: environment({ userAgent: defaultNavigator.userAgent, platform: 'Win32' }),
        expected: { device: 'desktop', platform: 'windows', name: 'Windows' },
    },
    {
        name: 'macOS desktop',
        input: environment({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', platform: 'MacIntel' }),
        expected: { device: 'desktop', platform: 'macos', name: 'macOS' },
    },
    {
        name: 'Linux desktop',
        input: environment({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', platform: 'Linux x86_64' }),
        expected: { device: 'desktop', platform: 'linux', name: 'Linux' },
    },
    {
        name: 'iPhone',
        input: environment({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148', platform: 'iPhone', touchPoints: 5, coarse: true, width: 390, height: 844 }),
        expected: { device: 'mobile', platform: 'ios', name: 'iOS' },
    },
    {
        name: 'Android before Linux fallback',
        input: environment({ userAgent: 'Mozilla/5.0 (Linux; Android 15; Pixel 9) Mobile', platform: 'Linux armv8l', touchPoints: 5, coarse: true, width: 412, height: 915 }),
        expected: { device: 'mobile', platform: 'android', name: 'Android' },
    },
    {
        name: 'iPadOS desktop user agent',
        input: environment({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)', platform: 'MacIntel', touchPoints: 5, coarse: true, width: 820, height: 1180 }),
        expected: { device: 'mobile', platform: 'ios', name: 'iPadOS' },
    },
    {
        name: 'UA client hint mobile',
        input: environment({ userAgent: 'Mozilla/5.0 AppleWebKit/537.36', platform: 'MysteryOS', touchPoints: 5, coarse: true, width: 430, height: 932, userAgentData: { mobile: true, platform: 'MysteryOS' } }),
        expected: { device: 'mobile', platform: 'mobile', name: 'Mobile' },
    },
    {
        name: 'unknown mobile-like device',
        input: environment({ userAgent: 'Mozilla/5.0 AppleWebKit/537.36', platform: 'MysteryOS', touchPoints: 2, coarse: true, width: 600, height: 960 }),
        expected: { device: 'mobile', platform: 'mobile', name: 'Mobile' },
    },
    {
        name: 'unknown desktop fallback',
        input: environment({ userAgent: 'Mozilla/5.0 AppleWebKit/537.36', platform: 'MysteryOS', width: 1366, height: 768 }),
        expected: { device: 'desktop', platform: 'windows', name: 'Windows' },
    },
    {
        name: 'Windows touch device',
        input: environment({ userAgent: defaultNavigator.userAgent, platform: 'Win32', touchPoints: 10, coarse: true, width: 800, height: 1280 }),
        expected: { device: 'desktop', platform: 'windows', name: 'Windows' },
    },
];

for (const testCase of cases) {
    assert.deepEqual(detectDevice(testCase.input), testCase.expected, testCase.name);
}

assert.equal(isHomepage({ pathname: '/' }), true);
assert.equal(isHomepage({ pathname: '/index.html' }), true);
assert.equal(isHomepage({ pathname: '/features.html' }), false);
assert.equal(sectionTarget('install', { pathname: '/' }), '#install');
assert.equal(sectionTarget('install', { pathname: '/features.html' }), 'index.html#install');
assert.equal(sectionTarget('demos', { pathname: '/contact.html' }), 'index.html#demos');
assert.equal(platformChoice('linux', { pathname: '/' }).href, '#install');
assert.equal(platformChoice('linux', { pathname: '/architecture.html' }).href, 'index.html#install');
assert.match(platformChoice('windows', { pathname: '/features.html' }).href, /Windows-x64\.exe$/);
assert.match(platformChoice('macos', { pathname: '/features.html' }).href, /macOS-arm64\.dmg$/);

const siteScript = fs.readFileSync(path.resolve(__dirname, '../../docs/site.js'), 'utf8');

function fakeLink(dataset, href) {
    const attributes = {};
    return {
        dataset: {...dataset},
        href,
        textContent: 'Static fallback',
        innerHTML: 'Static fallback',
        target: '',
        classList: { toggle() {} },
        addEventListener() {},
        removeAttribute(name) {
            delete attributes[name];
            if (name === 'data-desktop-download') delete this.dataset.desktopDownload;
        },
        setAttribute(name, value) { attributes[name] = value; },
        hasAttribute(name) { return Object.hasOwn(attributes, name); },
    };
}

function evaluateCtas({navigator: runtimeNavigator, screen, pathname, coarse = false}) {
    const links = [
        fakeLink({osLabel: 'short', placement: 'navigation'}, 'index.html#install'),
        fakeLink({placement: 'hero'}, 'index.html#demos'),
        fakeLink({mobileTarget: 'install', mobileLabel: 'Desktop installation options', placement: 'final_install'}, 'index.html#install'),
    ];
    const documentElement = {dataset: {}, clientHeight: 900, scrollHeight: 900, scrollTop: 0};
    const document = {
        documentElement,
        body: {},
        activeElement: null,
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll(selector) { return selector === '[data-os-primary]' ? links : []; },
    };
    const runtimeWindow = {
        navigator: runtimeNavigator,
        screen,
        document,
        location: {href: `https://row-bot.ai${pathname}`, pathname},
        matchMedia(query) {
            return {
                matches: query === '(pointer: coarse)' ? coarse : false,
                addEventListener() {},
            };
        },
        addEventListener() {},
        setTimeout() {},
    };
    const context = {
        window: runtimeWindow,
        document,
        navigator: runtimeNavigator,
        URL,
        console,
    };
    vm.runInNewContext(siteScript, context);
    return {links, device: documentElement.dataset.device, platform: documentElement.dataset.platform};
}

const mobileCtaCases = [
    {name: 'iOS', navigator: {userAgent: 'Mozilla/5.0 (iPhone) Mobile', platform: 'iPhone', maxTouchPoints: 5}, screen: {width: 390, height: 844}, coarse: true},
    {name: 'Android', navigator: {userAgent: 'Mozilla/5.0 (Linux; Android 15) Mobile', platform: 'Linux armv8l', maxTouchPoints: 5}, screen: {width: 412, height: 915}, coarse: true},
    {name: 'iPadOS', navigator: {userAgent: 'Mozilla/5.0 (Macintosh)', platform: 'MacIntel', maxTouchPoints: 5}, screen: {width: 820, height: 1180}, coarse: true},
    {name: 'unknown mobile', navigator: {userAgent: 'Mozilla/5.0', platform: 'MysteryOS', maxTouchPoints: 2}, screen: {width: 600, height: 960}, coarse: true},
];

for (const testCase of mobileCtaCases) {
    const result = evaluateCtas({...testCase, pathname: '/features.html'});
    assert.equal(result.device, 'mobile', testCase.name);
    assert.deepEqual(result.links.map(link => link.href), [
        'index.html#install',
        'index.html#demos',
        'index.html#install',
    ], testCase.name);
    assert.equal(result.links.some(link => /\.(exe|dmg)$/.test(link.href)), false, testCase.name);
}

const desktopCtaCases = [
    {name: 'Windows', navigator: defaultNavigator, screen: {width: 1920, height: 1080}, suffix: 'Windows-x64.exe'},
    {name: 'macOS', navigator: {userAgent: 'Mozilla/5.0 (Macintosh)', platform: 'MacIntel', maxTouchPoints: 0}, screen: {width: 1728, height: 1117}, suffix: 'macOS-arm64.dmg'},
    {name: 'unknown desktop', navigator: {userAgent: 'Mozilla/5.0', platform: 'MysteryOS', maxTouchPoints: 0}, screen: {width: 1366, height: 768}, suffix: 'Windows-x64.exe'},
    {name: 'Windows touch', navigator: {userAgent: defaultNavigator.userAgent, platform: 'Win32', maxTouchPoints: 10}, screen: {width: 800, height: 1280}, coarse: true, suffix: 'Windows-x64.exe'},
];

for (const testCase of desktopCtaCases) {
    const result = evaluateCtas({...testCase, pathname: '/contact.html'});
    assert.equal(result.device, 'desktop', testCase.name);
    assert.equal(result.links.every(link => link.href.endsWith(testCase.suffix)), true, testCase.name);
}

const linuxSubpage = evaluateCtas({
    navigator: {userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', platform: 'Linux x86_64', maxTouchPoints: 0},
    screen: {width: 1920, height: 1080},
    pathname: '/architecture.html',
});
assert.deepEqual(linuxSubpage.links.map(link => link.href), [
    'index.html#install',
    'index.html#install',
    'index.html#install',
]);
assert.equal(linuxSubpage.links.every(link => link.hasAttribute('data-linux-install')), true);

const linuxHomepage = evaluateCtas({
    navigator: {userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', platform: 'Linux x86_64', maxTouchPoints: 0},
    screen: {width: 1920, height: 1080},
    pathname: '/',
});
assert.equal(linuxHomepage.links.every(link => link.href === '#install'), true);
