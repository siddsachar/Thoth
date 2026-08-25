const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const scenarioName = process.argv[2];
const sourcePath = process.argv[3];
const source = fs.readFileSync(sourcePath, 'utf8');

class FakeClassList {
    constructor(initial = []) {
        this.values = new Set(initial);
    }

    add(...names) {
        names.forEach(name => this.values.add(name));
    }

    remove(...names) {
        names.forEach(name => this.values.delete(name));
    }

    contains(name) {
        return this.values.has(name);
    }
}

class FakeElement {
    constructor(id, rect, classes = []) {
        this.id = id;
        this.rect = { ...rect };
        this.classList = new FakeClassList(classes);
        this.dataset = {};
        this.style = {};
        this.parentElement = null;
        this.listeners = new Map();
        this.capturedPointers = new Set();
        this.offsetWidth = rect.right - rect.left;
        this.offsetHeight = rect.bottom - rect.top;
    }

    addEventListener(name, callback) {
        const callbacks = this.listeners.get(name) || [];
        callbacks.push(callback);
        this.listeners.set(name, callbacks);
    }

    dispatchEvent(event) {
        event.target = event.target || this;
        event.currentTarget = this;
        for (const callback of this.listeners.get(event.type) || []) callback(event);
        return true;
    }

    appendChild(child) {
        child.parentElement = this;
        // Moving a captured element in the DOM invalidates capture until the
        // controller explicitly establishes it again.
        child.capturedPointers.clear();
        return child;
    }

    setPointerCapture(pointerId) {
        this.capturedPointers.add(pointerId);
    }

    hasPointerCapture(pointerId) {
        return this.capturedPointers.has(pointerId);
    }

    releasePointerCapture(pointerId) {
        this.capturedPointers.delete(pointerId);
    }

    getBoundingClientRect() {
        const left = Number.parseFloat(this.style.left);
        const top = Number.parseFloat(this.style.top);
        const x = Number.isFinite(left) ? left : this.rect.left;
        const y = Number.isFinite(top) ? top : this.rect.top;
        return {
            left: x,
            top: y,
            right: x + this.offsetWidth,
            bottom: y + this.offsetHeight,
            width: this.offsetWidth,
            height: this.offsetHeight,
        };
    }
}

class FakeCustomEvent {
    constructor(type, options = {}) {
        this.type = type;
        this.bubbles = Boolean(options.bubbles);
    }
}

function pointer(type, overrides = {}) {
    return {
        type,
        button: 0,
        pointerId: 7,
        clientX: 110,
        clientY: 110,
        screenX: 1010,
        screenY: 710,
        preventDefault() {},
        ...overrides,
    };
}

function createEnvironment(tearOff) {
    const dock = new FakeElement('dock', { left: 80, top: 80, right: 260, bottom: 260 });
    const target = new FakeElement(
        'buddy',
        { left: 100, top: 100, right: 232, bottom: 232 },
        ['row-bot-buddy-docked'],
    );
    const body = new FakeElement('body', { left: 0, top: 0, right: 1200, bottom: 800 });
    dock.appendChild(target);
    const elements = new Map([['buddy', target], ['dock', dock]]);
    const notifications = [];
    const apiCalls = [];
    let clicks = 0;
    target.addEventListener('buddy-click', () => { clicks += 1; });

    const window = {
        innerWidth: 1200,
        innerHeight: 800,
        location: { port: '8080' },
        Quasar: { Notify: { create: value => notifications.push(value) } },
    };
    if (tearOff !== null) {
        window.pywebview = {
            api: {
                tear_off_buddy(screenX, screenY, port) {
                    apiCalls.push({ screenX, screenY, port });
                    return tearOff(screenX, screenY, port);
                },
            },
        };
    }
    const document = {
        body,
        getElementById: id => elements.get(id) || null,
        querySelectorAll: () => [],
    };
    const context = { window, document, CustomEvent: FakeCustomEvent, Promise, Number, Math };
    window.document = document;
    vm.runInNewContext(`(function () { ${source}\n})();`, context, { filename: sourcePath });

    function deliver(event) {
        const rect = target.getBoundingClientRect();
        const insideTarget = (
            event.clientX >= rect.left && event.clientX <= rect.right
            && event.clientY >= rect.top && event.clientY <= rect.bottom
        );
        if (
            ['pointerup', 'pointermove'].includes(event.type)
            && !target.hasPointerCapture(event.pointerId)
            && !insideTarget
        ) return false;
        target.dispatchEvent(event);
        return true;
    }

    return {
        apiCalls,
        body,
        clicks: () => clicks,
        deliver,
        dock,
        notifications,
        target,
    };
}

async function settle() {
    await Promise.resolve();
    await Promise.resolve();
}

async function clickBelowThreshold() {
    const env = createEnvironment(() => true);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 114, clientY: 113 }));
    env.deliver(pointer('pointerup', { clientX: 114, clientY: 113 }));
    await settle();
    assert.equal(env.clicks(), 1);
    assert.equal(env.apiCalls.length, 0);
}

async function firstDragKeepsCaptureAndCommits() {
    const env = createEnvironment(() => true);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 150, clientY: 150 }));
    assert.equal(env.target.parentElement, env.body);
    assert.equal(env.target.hasPointerCapture(7), true);
    const delivered = env.deliver(pointer('pointerup', {
        clientX: 500,
        clientY: 400,
        screenX: 1500,
        screenY: 1000,
    }));
    assert.equal(delivered, true);
    await settle();
    assert.equal(env.apiCalls.length, 1);
    assert.equal(env.target.style.display, 'none');
}

async function edgeAndPointerUpCommitOnce() {
    let resolveNative;
    const env = createEnvironment(() => new Promise(resolve => { resolveNative = resolve; }));
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 150, clientY: 150 }));
    env.deliver(pointer('pointermove', { clientX: 0, clientY: 300, screenX: 900, screenY: 900 }));
    env.target.dispatchEvent(pointer('pointerup', { clientX: 500, clientY: 400 }));
    env.target.dispatchEvent(pointer('pointercancel'));
    env.target.capturedPointers.delete(7);
    env.target.dispatchEvent(pointer('lostpointercapture'));
    assert.equal(env.apiCalls.length, 1);
    resolveNative(true);
    await settle();
    assert.equal(env.apiCalls.length, 1);
}

async function releaseOverDockCancels() {
    const env = createEnvironment(() => true);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.deliver(pointer('pointerup', { clientX: 180, clientY: 180 }));
    await settle();
    assert.equal(env.apiCalls.length, 0);
    assert.equal(env.clicks(), 0);
    assert.equal(env.target.parentElement, env.dock);
    assert.equal(env.target.style.display, '');
}

async function pointerCancelRestores() {
    const env = createEnvironment(() => true);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.target.dispatchEvent(pointer('pointercancel'));
    await settle();
    assert.equal(env.apiCalls.length, 0);
    assert.equal(env.clicks(), 0);
    assert.equal(env.target.parentElement, env.dock);
}

async function captureLossRestores() {
    const env = createEnvironment(() => true);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.target.capturedPointers.delete(7);
    env.target.dispatchEvent(pointer('lostpointercapture'));
    await settle();
    assert.equal(env.apiCalls.length, 0);
    assert.equal(env.clicks(), 0);
    assert.equal(env.target.parentElement, env.dock);
}

async function nativeFailureAllowsNextDrag() {
    let succeed = false;
    const env = createEnvironment(() => succeed);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.deliver(pointer('pointerup', { clientX: 500, clientY: 400 }));
    await settle();
    assert.equal(env.target.parentElement, env.dock);
    assert.equal(env.target.style.display, '');
    succeed = true;
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.deliver(pointer('pointerup', { clientX: 500, clientY: 400 }));
    await settle();
    assert.equal(env.apiCalls.length, 2);
    assert.equal(env.target.style.display, 'none');
}

async function nativeUnavailableRestores() {
    const env = createEnvironment(null);
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.deliver(pointer('pointerup', { clientX: 500, clientY: 400 }));
    await settle();
    assert.equal(env.apiCalls.length, 0);
    assert.equal(env.clicks(), 0);
    assert.equal(env.target.parentElement, env.dock);
    assert.equal(env.target.style.display, '');
    assert.match(env.notifications[0].message, /native Row-Bot window/);
}

async function nativeRejectionRestores() {
    const env = createEnvironment(() => Promise.reject(new Error('fake native rejection')));
    env.deliver(pointer('pointerdown'));
    env.deliver(pointer('pointermove', { clientX: 160, clientY: 160 }));
    env.deliver(pointer('pointerup', { clientX: 500, clientY: 400 }));
    await settle();
    assert.equal(env.apiCalls.length, 1);
    assert.equal(env.clicks(), 0);
    assert.equal(env.target.parentElement, env.dock);
    assert.equal(env.target.style.display, '');
}

const scenarios = {
    click_below_threshold: clickBelowThreshold,
    first_drag: firstDragKeepsCaptureAndCommits,
    edge_idempotent: edgeAndPointerUpCommitOnce,
    release_over_dock: releaseOverDockCancels,
    pointer_cancel: pointerCancelRestores,
    capture_loss: captureLossRestores,
    native_failure_retry: nativeFailureAllowsNextDrag,
    native_unavailable: nativeUnavailableRestores,
    native_rejection: nativeRejectionRestores,
};

if (!scenarios[scenarioName]) throw new Error(`Unknown scenario: ${scenarioName}`);
scenarios[scenarioName]().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
