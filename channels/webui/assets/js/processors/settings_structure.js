// mirrors PER_USER_CORE_KEYS in core/config.py - the only "core" settings
// that are stored per-user; everything else in "core" is instance-wide
const PER_USER_CORE_KEYS = ["auto_resume_chats", "cmd_prefix", "tool_timeout"];

function isToggleList(data) {
    if (typeof data !== 'object' || data === null) return false;
    return Array.isArray(data.enabled) && Array.isArray(data.disabled);
}

function detectFieldType(value, key = '') {
    // special keys that should be displayed in a special way
    switch (key) {
        case "model.name":                  return "model_select"
        case "api.url":                     return "api_url"
        case "api.key":                     return "api_key"
        case "model.reasoning_effort":      return "reasoning_effort_slider"
    }

    // standard types
    if (value === null || value === undefined) return 'text';
    else if (typeof value === 'boolean') return 'boolean';
    else if (typeof value === 'number' && !key.toLowerCase().endsWith('id')) return 'number';
    else if (Array.isArray(value)) return 'array';
    else if (typeof value === 'string') {
        if (value.match(/^https?:\/\//)) return 'url';
        else if (value.includes('\n')) return 'textarea';
        else return 'text';
    } else {
        return 'text';
    }
}

function buildSettingsStructure(originalData, moduleInfo = {}, isAdmin = true) {
    const categories = {};
    let order = 0;

    categories.appearance = {
        title: 'Appearance',
        description: 'Theme and interface customization',
        order: order++,
        isThemeCategory: true
    };
    categories.audio = {
        title: 'Audio',
        description: 'Audio settings',
        order: order++,
        isThemeCategory: true
    };
    categories.system_prompt = {
        title: 'System Prompt',
        description: 'See the current system prompt',
        order: 100,
        isThemeCategory: true
    };
    if (isAdmin) {
        categories.system_logs = {
            title: 'System Logs',
            description: 'Peek into the great unknown',
            order: 999,
            isThemeCategory: true
        };
    }

    for (const [topKey, topValue] of Object.entries(originalData)) {
        if (topKey.toLowerCase() === 'theme' || topKey.toLowerCase() === 'theme_mode') {
            continue;
        }

        // global instance categories: only admins can manage channels
        if (!isAdmin && (topKey === 'channels' || topKey === 'user_channels')) {
            continue;
        }

        // for non-admins, the "core" section only exposes the per-user keys
        // (the rest of "core" is instance-wide and would be ignored by the backend)
        if (!isAdmin && topKey === 'core' && topValue && typeof topValue === 'object') {
            const filtered = {};
            for (const key of Object.keys(topValue)) {
                if (PER_USER_CORE_KEYS.includes(key)) {
                    filtered[key] = topValue[key];
                }
            }
            topValue = filtered;
        }

        const category = {
            title: formatLabel(topKey),
            description: `Configure ${formatLabel(topKey).toLowerCase()}`,
            order: order++
        };

        if (topKey === 'modules' || topKey === 'user_modules' || 
            topKey === 'channels' || topKey === 'user_channels') {
            category.isModuleCategory = true;
            category.enabled = topValue.enabled || [];
            category.disabled = topValue.disabled || [];
            
            const descriptions = {};
            const unsafeModules = {};
            for (const [itemName, info] of Object.entries(moduleInfo)) {
                if (info.description) descriptions[itemName] = info.description;
                if (info.unsafe) unsafeModules[itemName] = true;
            }
            category.descriptions = descriptions;
            category.unsafeModules = unsafeModules;

            category.settings = {};
            if (topValue.settings && typeof topValue.settings === 'object') {
                for (const [itemName, itemSettings] of Object.entries(topValue.settings)) {
                    if (!itemSettings) continue;
                    const itemInfo = moduleInfo[itemName] || {};
                    const itemSchema = itemInfo.settings_schema || {};
                    category.settings[itemName] = {
                        title: formatLabel(itemName),
                        description: itemInfo.description || '',
                        unsafe: itemInfo.unsafe || false,
                        value: buildFieldSettings(itemSettings, itemSchema, itemName)
                    };
                }
            }
        } else {
            // For core config sections (api, model, core, etc.), use the schema from moduleInfo
            const sectionSchema = (moduleInfo[topKey] && moduleInfo[topKey].settings_schema) || {};
            category.settings = (topValue && typeof topValue === 'object') ? 
                buildFieldSettings(topValue, sectionSchema, topKey) : {};
        }

        categories[topKey] = category;
    }

    return categories;
}

function buildFieldSettings(obj, schema, prefix = '') {
    if (!obj || typeof obj !== 'object') return {};
    
    const settings = {};

    for (const [key, value] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key;
        const fieldSchema = schema[key] || {};

        // Check if schema defines this field with metadata
        const hasSchemaDefinition = fieldSchema && (fieldSchema.type !== undefined || fieldSchema.default !== undefined || fieldSchema.description !== undefined);

        if (hasSchemaDefinition) {
            // Schema defines the field - use schema for metadata, value for current value
            const schemaValue = fieldSchema.default !== undefined ? fieldSchema.default : value;
            settings[key] = {
                title: formatLabel(key),
                type: fieldSchema.type === 'long_text' ? 'textarea' : (fieldSchema.type || detectFieldType(schemaValue, fullKey)),
                description: fieldSchema.description || null,
                unsafe: fieldSchema.unsafe || false,
                value: value,
                options: fieldSchema.options || null,
                min: fieldSchema.min,
                max: fieldSchema.max,
                step: fieldSchema.step,
                depends: fieldSchema.depends || null
            };
        } else if (typeof value === 'object' && value !== null && !Array.isArray(value) && !isToggleList(value)) {
            // Nested object without schema definition - recurse
            settings[key] = {
                type: 'object',
                title: formatLabel(key),
                description: fieldSchema.description || null,
                depends: fieldSchema.depends || null,
                settings: buildFieldSettings(value, fieldSchema, fullKey)
            };
        } else if (isToggleList(value)) {
            settings[key] = {
                type: 'toggle_list',
                title: formatLabel(key),
                description: fieldSchema.description || null,
                value: value
            };
        } else if (Array.isArray(value)) {
            settings[key] = {
                type: 'array',
                title: formatLabel(key),
                description: fieldSchema.description || null,
                value: value
            };
        } else if (typeof value === 'object') {
            settings[key] = {
                type: 'object',
                title: formatLabel(key),
                description: fieldSchema.description || null,
                settings: buildFieldSettings(value, fieldSchema, fullKey)
            };
        } else {
            // Primitive value without schema definition
            settings[key] = {
                title: formatLabel(key),
                type: detectFieldType(value, fullKey),
                description: fieldSchema.description || null,
                unsafe: fieldSchema.unsafe || false,
                depends: fieldSchema.depends || null,
                value: value,
                options: fieldSchema.options || null,
                min: fieldSchema.min,
                max: fieldSchema.max,
                step: fieldSchema.step
            };
        }
    }

    return settings;
}
