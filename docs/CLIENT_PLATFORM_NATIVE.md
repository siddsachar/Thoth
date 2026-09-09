# Browser and native capability contract

`frontend/src/platform` owns one `ClientPlatform` interface: file/folder
selection, upload, clipboard read/write, external URLs, managed windows,
download/save and discovery. Each operation returns `ok`, `cancelled` or an
explicit `unavailable` reason. Components consume this interface; they do not
look at screen size, user-agent strings, native flags or `pywebview.api`.

The production v1.0 handshake's `native_adapter.available` is the literal
`false`. `selectClientPlatform(media, handshake)` therefore selects browser
behavior, including inside a legacy desktop window. It never calls legacy
`_JsApi` methods. The legacy launcher API is shared across windows and returns
paths, which does not meet the new per-window opaque-reference contract.
Its existing NiceGUI consumers remain unchanged until their migration gate.

The browser implementation uses an explicit file input (directory input where
supported), browser clipboard permissions, safe HTTP(S) links with
`noopener noreferrer`, and authenticated Blob downloads. It returns `File`
objects, never host paths. Clipboard and picker denial/cancellation are visible;
managed native windows report unavailable. User-activation checks prevent
background native-like interactions where the browser exposes that state.
Upload/download call the injected authenticated `ClientController` methods;
the platform does not create a fetch/event connection or store session secrets.
An unavailable native operation never silently runs on a different computer.

`createFakePlatform` has the same public methods with deterministic scripted
results and operation-name counters. `createPyWebViewPlatform` calls only a
typed `NativeEndpoint.dispatch`, validates returned references and rejects
path-shaped native responses. It is independently testable without starting
an OS window. Constructing that adapter is not permission to expose a native
endpoint to production: protocol negotiation and the trusted shell must both
permit it.

An aborted native picker/save request discards a late completion. This does not
dismiss an already-open OS dialog or undo a completed host save. The trusted
host remains responsible for cancellation and authority checks before effects.

`row_bot.native_client.NativeClientBridge` is the implementation for the future
trusted shell integration. A `PyWebViewDriver` wraps one explicit window;
optional clipboard/window/save callbacks retain their existing platform owners.
Only `native_client_dispatch` is exposed to JavaScript. The Python-only loaded
hook issues a random document lease bound to instance, exact window, canonical
loopback origin and epoch. Remote origins cannot be bound. `before_load`, close
and a new loaded document revoke the old lease. Every operation rechecks the
current shell URL and expiry, including after trusted callbacks complete. The lease
is a closure value rather than a URL, storage value or asserted bridge flag.
The shell forbids frames, and the bootstrap only installs in the top document.

File/folder selection requires a trusted backend registration callback. After
the OS picker returns, the bridge rechecks its document lease, passes the chosen
`Path` only to that backend callback and returns only the registered opaque
reference. Without that callback selection is unavailable. The backend remains
responsible for current session/resource/root/symlink and approval policy;
native selection does not grant arbitrary JavaScript filesystem access.
Save accepts an opaque reference and safe display name. Its backend callback
revalidates resource access and writes only after the picker and lease check.
Browser-provided paths, arbitrary native methods and non-HTTP(S) external URLs
are rejected. Managed windows receive only `/app-v2/` routes; each new window
needs its own binding. Native errors expose no paths or raw exception text.

The trusted hook `attach_native_client(...)` is provided for isolated/fake
integration and the later platform lane. The production launcher intentionally
does not invoke it while the canonical handshake advertises unavailable.
Enabling it requires the Phase 5 negotiated native capability and platform
integration gates, including native event-order behavior, actual file dialogs,
clipboard permissions, save/cancellation, cross-window isolation and navigation
revocation on Windows/macOS/Linux. Deterministic fake-driver tests establish
the contract and effect boundaries; they do not establish OS or installed-machine
behavior. No new dependency, shell command, native process or user-data access
is needed to import or test this adapter.
