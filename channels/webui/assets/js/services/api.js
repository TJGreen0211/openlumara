/* 
 * --- useful functions for sending/receiving to/from the backend API and websockets
 */
async function simpleApiFetch(url) {
    // fetches something from the API and returns the data extracted from the JSON response
    raw_data = await(
        await fetch(url)
    ).json()

    if (!raw_data.success) {
        throw raw_data.data;
    }

    return raw_data.data;
}
async function simpleApiPost(url, content=null) {
    // posts something to the API and returns the data extracted from the JSON response
    raw_data = await(
        await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(content)
        })
    ).json()

    console.log(raw_data.data);

    if (!raw_data.success) {
        throw raw_data.data;
    }

    return raw_data.data;
}

async function simpleSocketSend(data) {
    try {
        console.log(data);
        if (!window.socket || window.socket.readyState !== WebSocket.OPEN) {
            // the socket is gone or closing; send() would drop the payload
            // without raising, so report the failure instead of pretending success
            return false;
        }
        window.socket.send(JSON.stringify(data));
        return true;
    } catch (e) {
        return false
    }
}

async function reportDetectedTimezone() {
    // tell the backend this browser's IANA timezone so the AI's "current time"
    // reflects the user's zone instead of the server's. best-effort / non-fatal:
    // if it fails the server simply falls back to its own zone.
    try {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (!tz) return;
        await simpleApiPost('/api/settings/detected_timezone', { timezone: tz });
    } catch (e) {
        // ignore: detection is optional
    }
}
