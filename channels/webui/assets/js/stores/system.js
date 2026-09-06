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
        try {
            this.logs = await simpleApiFetch("/api/system/logs");
        } catch (e) {
            this.logs = [];
        }
        this.data = await simpleApiFetch("/api/system/data");
    }
}
