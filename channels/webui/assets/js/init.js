marked.setOptions({
    breaks: true,
    gfm: true
});

// a code block without a language tag gets auto-detected, which tries every
// registered language in turn. limit that guess to the common ones; blocks
// tagged with any other bundled language still highlight normally
hljs.configure({
    languages: ['python', 'javascript', 'typescript', 'bash', 'json', 'xml', 'css', 'sql', 'yaml', 'cpp']
});

/*
 * initializes Alpine and registers all the necessary stuff
 */
document.addEventListener('alpine:init', async () => {
    // these are all defined in js/stores/
    Alpine.store("system", SYSTEM_STORE);
    Alpine.store("ui", UI_STORE);
    Alpine.store("settings", SETTINGS_STORE);
    Alpine.store("chat", CHAT_STORE);
    Alpine.store('stream', STREAM_STORE);
    Alpine.store('theme', THEME_STORE);
    Alpine.store('audio', AUDIO_STORE);
    Alpine.store('upload', UPLOAD_STORE);
    Alpine.store('voice', VOICE_STORE);
    Alpine.store('meets', MEETS_STORE);
    Alpine.store('artifact', ARTIFACT_STORE);
    if (typeof USERS_STORE !== 'undefined') {
        Alpine.store('users', USERS_STORE);
    }

    // start the browser notification system
    Alpine.store('notifications', NOTIFY_STORE);
    await Alpine.store('notifications').init();

    // defined in directives/
    Alpine.directive('auto-scroll', autoScroll);
    Alpine.directive('copy-code', copyCode);

    // chat copies should be plain text (js/services/plain_copy.js)
    registerPlainCopy();

    // restore the per-chat preview map *before* the websocket connects, so an
    // in-flight stream can't persist a new entry that init() would then
    // overwrite with the stale saved map
    Alpine.store('artifact').init();

    // check if we're on a phone (the layout needs this before anything else)
    await Alpine.store('ui').init();

    // the chat is what the user is waiting for, so it's requested first and
    // nothing waits on anything else: these used to run one after another,
    // about a dozen round trips before the chat was even asked for
    await Promise.allSettled([
        // fetch current chat
        Alpine.store('chat').load(),
        connectWebSocket(),
        // report the user's timezone (per-user) so the AI reflects it, not the server's
        reportDetectedTimezone(),
        // fetch any relevant system data (like system logs, max context, etc)
        Alpine.store('system').loadData(),
        // load settings for feature flags (voice_enabled, etc)
        Alpine.store('settings').init(),
        // initialize users store (sets isAdmin flag)
        Alpine.store('users')?.init(),
    ]);

    // register the service worker
    if ('serviceWorker' in navigator) {
        // updateViaCache: 'none' forces the browser to refetch /sw.js from the
        // network on every update check, so a stale cached service worker can
        // never keep serving stale precached CSS/JS after a deploy
        navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' });
    }

    // do the initial scroll to bottom
    requestAnimationFrame(() => {
        Alpine.store('ui').forceScrollToBottom();
    });

    await registerKeyboardShortcuts();

    // auto-close sidebar on resizing to below desktop size (mobile size)
    // window.addEventListener('resize', () => {
    //     if (window.innerWidth < 768 && Alpine.store('ui').sidebarOpen) {
    //         Alpine.store('ui').sidebarOpen = false;
    //     }
    //     else if (window.innerWidth > 768 && !Alpine.store('ui').sidebarOpen) {
    //         Alpine.store('ui').sidebarOpen = true;
    //     }
    // });
});
