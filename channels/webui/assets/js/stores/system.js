SYSTEM_STORE = {
    data: {},
    logs: [],
    running: true,
    restarting: false,
    message: '',

    async restart(message = 'Restarting server..') {
        this.message = message || "Restarting server..";
        this.restarting = true;
        await simpleApiPost("/api/system/restart");
        this.restarting = false;
    },

    async loadData() {
        // logs are admin-only in multi-user mode, so fetch them separately
        // so that the rest of the system data still loads for regular users
        const [logs, data] = await Promise.allSettled([
            simpleApiFetch("/api/system/logs"),
            simpleApiFetch("/api/system/data"),
        ]);
        this.logs = logs.status === 'fulfilled' ? logs.value : [];
        if (data.status === 'rejected') { throw data.reason; }
        this.data = data.value;
    }
}
