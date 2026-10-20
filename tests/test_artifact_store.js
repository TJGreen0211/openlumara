const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(
    'channels/webui/assets/js/stores/artifact.js',
    'utf8'
);

function makeStore() {
    const values = new Map();
    const context = {
        Alpine: { store: () => ({ selectedChat: 'chat-1' }) },
        localStorage: {
            getItem: key => values.get(key) ?? null,
            setItem: (key, value) => values.set(key, value)
        },
        URLSearchParams,
        document: { getElementById: () => null },
        // /api/artifact-token
        simpleApiFetch: async url => ({ token: `T(${new URLSearchParams(url.split('?')[1]).get('folder')})` })
    };
    vm.runInNewContext(`${source}\nthis.artifactStore = ARTIFACT_STORE;`, context);
    const store = context.artifactStore;
    store.init();
    return { store, values };
}

const call = (name, sandbox, path, id) => ({
    id,
    function: { name, arguments: JSON.stringify({ sandbox, path, content: '<html></html>' }) }
});
const history = (...calls) => [{ role: 'assistant', messages: [{ role: 'assistant', tool_calls: calls }] }];
const plain = value => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {

// single site, restored from history: panel stays closed, persisted
{
    const { store, values } = makeStore();
    store.restoreFromHistory('chat-1', history(call('coder_file_create', 'project', 'site/index.html')));

    assert.equal(store.current.folder, 'site');
    assert.equal(store.currentPage, 'site/index.html');
    // file urls wait for the site's signed token, then carry it as a path prefix
    assert.equal(store.entryUrl, null);
    await settle();
    assert.equal(store.entryUrl, '/api/artifact/T(site)/project/site/index.html');
    assert.equal(store.open, false);
    assert.equal(values.has('openlumara_artifact'), true);
}

// separate prototypes become separate sites; pages of one site group together
{
    const { store } = makeStore();
    store.restoreFromHistory('chat-1', history(
        call('coder_file_create', 'coder', 'bakery/index.html'),
        call('coder_file_create', 'coder', 'bakery/about.html'),
        call('coder_file_create', 'coder', 'coder/bakery/blog/post.html'),  // sandbox-prefixed path
        call('coder_file_create', 'coder', 'portfolio/index.html'),
    ));

    assert.deepEqual(plain(store.sites.map(s => s.folder)), ['portfolio', 'bakery']);
    assert.equal(store.current.folder, 'portfolio');

    store.selectSite('coder:bakery');
    assert.deepEqual(plain(store.pages), ['bakery/index.html', 'bakery/about.html', 'bakery/blog/post.html']);
    assert.equal(store.currentPage, 'bakery/index.html');
    assert.equal(store.pageLabel('bakery/blog/post.html'), 'blog/post.html');

    store.selectPage('bakery/about.html');
    store.entryUrl;
    await settle();
    assert.equal(store.entryUrl, '/api/artifact/T(bakery)/coder/bakery/about.html');

    // a later history rebuild keeps the user's own pick
    store.restoreFromHistory('chat-1', history(
        call('coder_file_create', 'coder', 'bakery/index.html'),
        call('coder_file_create', 'coder', 'bakery/about.html'),
        call('coder_file_create', 'coder', 'portfolio/index.html'),
    ));
    assert.equal(store.current.folder, 'bakery');
    assert.equal(store.currentPage, 'bakery/about.html');
}

// a root index.html doesn't swallow other sites, and an opened entry above an
// existing site absorbs it
{
    const { store } = makeStore();
    store.restoreFromHistory('chat-1', history(
        call('coder_file_create', 'coder', 'index.html'),
        call('coder_file_create', 'coder', 'shop/blog/post.html'),
        call('coder_open_preview', 'coder', 'shop/index.html'),
    ));
    assert.deepEqual(plain(store.sites.map(s => s.folder)), ['shop', '']);
    assert.deepEqual(plain(store.current.pages), ['shop/blog/post.html', 'shop/index.html']);
    assert.equal(store.current.entry, 'shop/index.html');
}

// streaming: partial argument JSON is ignored, each call is handled once, and
// a manual close mid-stream isn't undone by the next chunk
{
    const { store } = makeStore();
    const partial = { id: 'a', function: { name: 'coder_file_create', arguments: '{"sandbox":"coder","path":"site/about.htm' } };
    store.recordToolCall(partial, 'a');
    assert.equal(store.current, null);

    const opened = call('coder_open_preview', 'coder', 'site/index.html', 'b');
    store.recordToolCall(opened, 'b');
    assert.equal(store.open, true);

    store.close();
    store.recordToolCall(opened, 'b');  // re-sent with the next chunk
    assert.equal(store.open, false);

    store.finalize();
    assert.equal(store.open, false);

    // file writes only open the panel once the stream completes
    store.recordToolCall(call('coder_file_edit', 'coder', 'site/index.html', 'c'), 'c');
    assert.equal(store.open, false);
    store.finalize();
    assert.equal(store.open, true);
}

// attachments target the active site unless the user opted out
{
    const { store } = makeStore();
    assert.equal(store.attachTarget, null);
    store.restoreFromHistory('chat-1', history(call('coder_file_create', 'coder', 'bakery/index.html')));
    assert.deepEqual(plain(store.attachTarget), { sandbox: 'coder', folder: 'bakery' });
    store.mediaUrl({ path: 'media/hero.jpg' });
    await settle();
    assert.equal(store.mediaUrl({ path: 'media/hero.jpg' }), '/api/artifact/T(bakery)/coder/bakery/media/hero.jpg');
    store.saveAttachments = false;
    assert.equal(store.attachTarget, null);
}

console.log('artifact store: ok');
})().catch(e => { console.error(e); process.exit(1); });
