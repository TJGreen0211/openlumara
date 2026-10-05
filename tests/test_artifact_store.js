const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(
    'channels/webui/assets/js/stores/artifact.js',
    'utf8'
);
const values = new Map();
const context = {
    Alpine: { store: () => ({ selectedChat: 'chat-1' }) },
    localStorage: {
        getItem: key => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value)
    }
};
vm.runInNewContext(`${source}\nthis.artifactStore = ARTIFACT_STORE;`, context);

const store = context.artifactStore;
store.init();
store.restoreFromHistory('chat-1', [{
    role: 'assistant',
    messages: [{
        role: 'assistant',
        tool_calls: [{
            function: {
                name: 'coder_file_create',
                arguments: '{"sandbox":"project","path":"site/index.html"}'
            }
        }]
    }]
}]);

assert.deepEqual(
    JSON.parse(JSON.stringify(store.byChat['chat-1'])),
    { sandbox: 'project', path: 'site/index.html' }
);
assert.equal(store.current.path, 'site/index.html');
assert.equal(store.open, false);
assert.equal(values.has('openlumara_artifact'), true);
