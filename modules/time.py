import core
import os
import datetime
import zoneinfo

# get all available timezones
TIMEZONES = {"local": "Use your device's local timezone"}
TIMEZONES.update({tz: f"Set your timezone to {tz}" for tz in sorted(zoneinfo.available_timezones())})

class Time(core.module.Module):
    """Makes the AI aware of the current time and date"""

    settings = {
        "method": {
            "type": "select",
            "default": "message injection",
            "description": "What method to use to make your AI aware of time",
            "options": {
                "message injection": "Injects timestamps into the messages you send. This will make your AI able to see when any message was sent, and give it a sense of how much time has passed between each message!",
                "end prompt": "Injects the current time/date at the end of message history, which is a more basic way of making your AI aware of time. It will make your AI only know the current time and have no sense of the passage of time"
            }
        },
        "add_timezone": {
            "default": True,
            "description": "Puts your timezone in the timestamps that are sent to the AI. Makes the AI timezone-aware!"
        },
        "timezone": {
            "default": "local",
            "description": "Your timezone",
            "type": "select",
            "options": TIMEZONES,
            "depends": "add_timezone"
        },
        "date_format": {
            "default": "%c",
            "description": "A string that describes exactly how to display the date/time to your AI. Uses strftime format (https://github.com/Vishxnu/Python-strftime-cheatsheet)",
            "depends": "add_timezone"
        }
    }

    # self.config only sees the GLOBAL config, so module settings must be read
    # through core.config.get() (which merges the user's own override from
    # data/{username}/config.json over it), or per-user settings saved via the
    # WebUI would be silently ignored
    def _get_setting(self, key, default):
        return core.config.get("modules", "settings", self.name, key, default=default)

    def _get_current_time(self):
        """gets the current time/date, with timezone support"""
        tz_setting = self._get_setting("timezone", "local")

        if tz_setting == "local":
            # "local" means the user's device timezone, which the browser detects
            # and reports to the server. fall back to the server's own zone when
            # nothing has been detected yet (e.g. CLI, or first load), or when
            # the stored zone no longer resolves (stale file, tzdata change).
            # an unresolvable zone must NOT raise here: in on_end_prompt() that
            # would mark the module broken for the rest of the run.
            detected = self._read_detected_timezone()
            if detected:
                try:
                    now = datetime.datetime.now(tz=zoneinfo.ZoneInfo(detected))
                    return now
                except Exception:
                    pass

            return datetime.datetime.now().astimezone()

        try:
            tz = zoneinfo.ZoneInfo(tz_setting)
        except Exception:
            # an explicit but invalid zone (e.g. left over from an older config)
            # falls back to the server's own zone instead of breaking the hook
            return datetime.datetime.now().astimezone()

        return datetime.datetime.now(tz=tz)

    def _read_detected_timezone(self):
        """read the browser-detected IANA timezone for the current user, if any"""
        try:
            path = core.get_data_path("detected_timezone")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    value = f.read().strip()
                return value or None
        except Exception as e:
            self.log("module error", f"{self.name}: could not read detected timezone: {core.detail_error(e)}")
        return None

    async def on_end_prompt(self):
        if self._get_setting("method", "message injection") != "end prompt":
            return None

        now = self._get_current_time()
        time_info = [now.strftime(self._get_setting("date_format", "%c"))]

        if self._get_setting("add_timezone", True):
            time_info.append(str(now.tzname()))

        time_info_str = " ".join(time_info)
        return f"Current time/date is {time_info_str}"

    async def on_message_inject(self):
        if self._get_setting("method", "message injection") != "message injection":
            return None

        now = self._get_current_time()
        time_info = [now.strftime(self._get_setting("date_format", "%c"))]

        if self._get_setting("add_timezone", True):
            time_info.append(str(now.tzname()))

        time_info_str = " ".join(time_info)
        return f"sent on {time_info_str}"
