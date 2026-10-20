// used for first-time visitors, and when a saved family no longer exists
const DEFAULT_THEME_FAMILY = 'colors-blue';

THEME_STORE = {
    family: localStorage.getItem('themeFamily') || DEFAULT_THEME_FAMILY,
    mode: localStorage.getItem('themeMode') || 'dark',
    themeCache: {},  // Cache for loaded theme data
    themeList: [],   // List of available theme families

    init() {
        // Load theme list (just names and modes) - async, doesn't block
        fetch('/api/themes')
            .then(r => r.json())
            .then(data => { this.themeList = data; })
            .catch(e => console.error('Failed to load themes:', e));

        // Load base theme and current theme family - async, will apply when ready
        this.loadThemeFamily('base').then(() => {
            this.loadThemeFamily(this.family).then(async (data) => {
                // a stale saved name (the old 'monochrome' default never existed)
                // would otherwise leave the page with no theme at all
                if (!data && this.family !== DEFAULT_THEME_FAMILY) {
                    this.family = DEFAULT_THEME_FAMILY;
                    await this.loadThemeFamily(this.family);
                }
                this.apply(this.family, this.mode);
            });
        });

    },

    // Load a specific theme family if not already cached
    async loadThemeFamily(family) {
        if (this.themeCache[family]) {
            return this.themeCache[family];
        }

        try {
            const response = await fetch(`/api/themes/${family}`);
            if (!response.ok) {
                throw new Error(`Failed to load theme: ${response.statusText}`);
            }
            this.themeCache[family] = await response.json();
            return this.themeCache[family];
        } catch (e) {
            console.error(`Failed to load theme family '${family}':`, e);
            return null;
        }
    },

    apply(family, mode) {
        const themeData = this.themeCache[family];
        if (!themeData) {
            console.error('Theme family not found:', family);
            return;
        }

        // Handle mode fallback
        let effectiveMode = mode;
        if (!themeData[mode]) {
            const alternateMode = mode === 'dark' ? 'light' : 'dark';
            if (themeData[alternateMode]) {
                effectiveMode = alternateMode;
            } else {
                effectiveMode = 'dark';
            }
        }

        const finalTheme = themeData[effectiveMode];
        const root = document.documentElement;

        // Apply base theme vars first
        const baseTheme = this.themeCache['base'];
        if (baseTheme) {
            for (const [varName, value] of Object.entries(baseTheme)) {
                root.style.setProperty(varName, value);
            }
        }

        // apply font family
        const fontFam = localStorage.getItem('fontFamily');
        if (fontFam) {
            this.setFont(fontFam);
        }

        // apply font size
        const fontSize = localStorage.getItem('fontSize') || 16;
        root.style.setProperty('--font-size-base', `${fontSize}px`);

        // apply chat width
        const chatWidth = localStorage.getItem('chatContentWidth') || 100;
        root.style.setProperty('--chat-content-width', `${chatWidth}%`);
        
        // apply message bubble width
        const messageWidth = localStorage.getItem('messageMaxWidth') || 70;
        root.style.setProperty('--message-max-width', `${messageWidth}%`);

        // Apply theme vars on top of base
        if (finalTheme) {
            for (const [varName, value] of Object.entries(finalTheme)) {
                root.style.setProperty(varName, value);
            }
        }

        this.applyAccentContrast();

        // Switch code syntax highlighting theme based on mode
        const codeThemeLink = document.getElementById('code-theme');
        if (codeThemeLink) {
            codeThemeLink.href = effectiveMode === 'dark'
                ? '/assets/css/code-themes/github-dark.css'
                : '/assets/css/code-themes/github-light.css';
        }

        // Update state
        this.family = family;
        this.mode = effectiveMode;

        // Persist
        localStorage.setItem('themeFamily', family);
        localStorage.setItem('themeMode', effectiveMode);

        // Dispatch event for other components to react
        document.dispatchEvent(new CustomEvent('theme-changed', {
            detail: { family, mode: effectiveMode }
        }));
    },

    // themes only define --accent, so derive a readable text color for things
    // filled with it (e.g. the send button): black on light accents, white on dark
    applyAccentContrast() {
        const root = document.documentElement;
        const probe = document.createElement('span');
        probe.style.color = 'var(--accent)';
        document.body.appendChild(probe);
        const [r, g, b] = (getComputedStyle(probe).color.match(/[\d.]+/g) || [0, 0, 0]).map(Number);
        probe.remove();

        // WCAG relative luminance
        const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
        const luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);

        // 0.179 is where black and white text have equal WCAG contrast, but
        // white reads better on saturated mid-tones (indigo, violet, blue), so
        // only switch to black for genuinely light accents (nord frost, white)
        root.style.setProperty('--accent-contrast', luminance > 0.4 ? '#000000' : '#ffffff');
    },

    // Toggle mode (dark/light)
    toggleMode() {
        this.apply(this.family, this.mode === 'dark' ? 'light' : 'dark');
    },

    // Set font
    setFont(font) {
        const root = document.documentElement;
        localStorage.setItem('fontFamily', font);

        if (font && font !== 'default') {
            this.loadGoogleFont(font);
            root.style.setProperty('--font-primary', `'${font}', sans-serif`);
            root.style.setProperty('--code-font', `'${font}', monospace`);
        } else {
            // "System Default": go back to base.json's stacks (the OS UI font),
            // including the code font a previous custom pick overrode
            const base = this.themeCache['base'] || {};
            root.style.setProperty('--font-primary', base['--font-primary'] || 'system-ui, sans-serif');
            root.style.setProperty('--code-font', base['--code-font'] || 'monospace');
        }
    },
    
    // Set font size
    setFontSize(size) {
        localStorage.setItem('fontSize', size);

        const root = document.documentElement;
        root.style.setProperty('--font-size-base', `${size}px`);
    },

    // Load Google Font
    loadGoogleFont(fontName) {
        const id = `font-${fontName.replace(/\s+/g, '-').toLowerCase()}`;
        if (document.getElementById(id)) return;

        const link = document.createElement('link');
        link.id = id;
        link.rel = 'stylesheet';
        link.href = `https://fonts.googleapis.com/css2?family=${fontName.replace(/ /g, '+')}:wght@400;500;600;700&display=swap`;
        document.head.appendChild(link);
    },

    // Get theme families for UI (grouped by prefix)
    getFamilies() {
        const groups = {};
        const others = [];
        
        for (const theme of this.themeList) {
            const themeInfo = {
                dark: theme.dark,
                light: theme.light
            };
            
            // Group by the word before the first hyphen
            const parts = theme.name.split('-');
            if (parts.length > 1) {
                const groupKey = parts[0].toLowerCase();
                if (!groups[groupKey]) {
                    groups[groupKey] = [];
                }
                groups[groupKey].push({ name: theme.name, ...themeInfo });
            } else {
                others.push({ name: theme.name, ...themeInfo });
            }
        }
        
        // Sort others alphabetically
        others.sort((a, b) => a.name.localeCompare(b.name));
        
        return { groups, others };
    },
    
    // Get sorted group names
    getGroupNames() {
        const families = this.getFamilies();
        return Object.keys(families.groups).sort();
    },
    
    // Get group name (capitalized)
    getGroupName(groupKey) {
        return groupKey.charAt(0).toUpperCase() + groupKey.slice(1);
    }
}
