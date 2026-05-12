import core
import datetime
import ulid

class Calendar(core.module.Module):
    """Lets your AI manage a calendar for you"""

    # TODO: add caldav support, iCal support, etc. maybe also google cal

    settings = {
        "range": {
            "type": "date",
            "description": "The range of days relative to today that you want the AI to see the appointments of",
            "default": 7
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.events = core.storage.StorageList("calendar", "json")

    async def _get_events_in_range(self):
        # display appointments between certain range (days before -> center (today) -> days after. divide by 2?)
        matches = []

        date_range = self.config.get("range", default=7)
        day_margin = date_range/2 # amount of days before and after today to filter by

        today = datetime.datetime.today()
        past_boundary = today - datetime.timedelta(days=day_margin)
        future_boundary = today + datetime.timedelta(days=day_margin)

        for event in self.events:
            event_date = datetime.datetime.fromisoformat(event["date"])
            if event_date <= future_boundary and event_date >= past_boundary:
                matches.append(event)

        return matches

    async def _get_event_by_id(self, id: str):
        for index, event in enumerate(self.events):
            if event['id'].strip() == id.strip():
                return index

        return -1

    async def on_system_prompt(self):
        matches = await self._get_events_in_range()
        output = []

        for event in matches:
            output.append(f"{event.get('id')}: on {event['date']}: {event['title']}")

        if not output:
            return None

        return "\n".join(output)

    async def add_event(self, title: str, year: int, month: int, day: int, hour: int, minute: int):
        event = {
            "id": str(ulid.ULID()),
            "title": title,
            "date": datetime.datetime.isoformat(
                datetime.datetime(
                    year=year,
                    month=month,
                    day=day,
                    hour=hour,
                    minute=minute
                )
            )
        }

        self.events.append(event)
        self.events.save()

        return self.result(f"appointment added with ID {event['id']}")

    async def edit_event(self, id: str, title: str = None, year: int = None, month: int = None, day: int = None, hour: int = None, minute: int = None):
        index = await self._get_event_by_id(id)
        if index < 0:
            return self.result("Error: Event with that ID does not exist", success=False)

        event = self.events[index]
        event_date = datetime.datetime.fromisoformat(event['date'])
        new_date_iso = datetime.datetime.isoformat(
            datetime.datetime(
                year=year if year else event_date.year,
                month=month if month else event_date.month,
                day=day if day else event_date.day,
                hour=hour if hour else event_date.hour,
                minute=minute if minute else event_date.minute
            )
        )

        self.events[index]["title"] = title if title else event['title']
        self.events[index]["date"] = new_date_iso
        self.events.save()

        return self.result(f"event {event['id']} edited")

    async def delete_event(self, id: str):
        index = await self._get_event_by_id(id)
        if index < 0:
            return self.result("Error: Event with that ID does not exist", success=False)

        self.events.pop(index)
        return self.result(f"event {id} deleted")
