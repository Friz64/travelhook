"contains our bot commands and the incoming webhook handler"
import json
import secrets
import typing
import urllib
from datetime import datetime, timedelta

from aiohttp import ClientSession, web
import discord
from discord.ext import commands
from haversine import haversine
import tomli
import tomli_w

from . import database as DB
from .format import format_travelynx
from .helpers import (
    format_time,
    get_train_emoji,
    is_token_valid,
    not_registered_embed,
    train_type_color,
    train_type_emoji,
    zugid,
    tz,
    train_presentation,
    parse_manual_time,
    fetch_headsign,
    blanket_replace_train_type,
)

config = {}
with open("settings.json", "r", encoding="utf-8") as f:
    config = json.load(f)

DB.connect(config["database"])

servers = [server.as_discord_obj() for server in DB.Server.find_all()]
intents = discord.Intents.default() | discord.Intents(members=True)
bot = commands.Bot(command_prefix=" ", intents=intents)


async def setup_hook():
    "enable restart persistence for the register button by adding the view on start"
    bot.add_view(RegisterTravelynxStepZero())


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    "once we're logged in, set up commands and start the web server"
    for server in servers:
        await bot.tree.sync(guild=server)
    bot.loop.create_task(receive(bot))
    print(f"logged in as {bot.user}")


def handle_status_update(userid, reason, status):
    """update trip data in the database, also starting a new journey if the last data
    we have is too old or distant for this to be a changeover"""

    user = DB.User.find(discord_id=userid)

    def is_new_journey(old, new):
        "determine if the user has merely changed into a new transport or if they have started another journey altogether"

        # don't drop the journey in case we receive a checkout after the new checkin
        if reason == "checkout" and status["train"]["id"] in [
            trip.status["train"]["id"]
            for trip in DB.Trip.find_current_trips_for(user.discord_id)
        ]:
            return False

        if old["train"]["id"] == new["train"]["id"]:
            return False

        if user.break_journey == DB.BreakMode.FORCE_BREAK:
            user.set_break_mode(DB.BreakMode.NATURAL)
            return True

        if user.break_journey == DB.BreakMode.FORCE_GLUE:
            user.set_break_mode(DB.BreakMode.NATURAL)
            return False

        change_from = old["toStation"]
        change_to = new["fromStation"]
        change_distance = haversine(
            (change_from["latitude"], change_from["longitude"]),
            (change_to["latitude"], change_to["longitude"]),
        )
        change_duration = datetime.fromtimestamp(
            change_to["realTime"], tz=tz
        ) - datetime.fromtimestamp(change_from["realTime"], tz=tz)

        return (
            change_distance > 2.0
            and not "travelhookfaked" in (new["train"]["id"] + old["train"]["id"])
        ) or change_duration > timedelta(hours=2)

    if (last_trip := DB.Trip.find_last_trip_for(userid)) and is_new_journey(
        last_trip.status, status
    ):
        user.do_break_journey()

    DB.Trip.upsert(userid, status)
    trip = DB.Trip.find(userid, zugid(status))
    trip.maybe_fix_circle_line()
    trip.maybe_fix_1970()


async def receive(bot):
    """our own little web server that receives incoming webhooks from
    travelynx and runs the live feed for the users that have enabled it"""

    async def handler(req):
        user = DB.User.find(
            token_webhook=req.headers["authorization"].removeprefix("Bearer ")
        )
        if not user:
            print(f"unknown user {req.headers['authorization']}")
            return

        async with user.get_lock():
            userid = user.discord_id
            data = await req.json()

            if data["reason"] == "ping" and not data["status"]["checkedIn"]:
                return web.Response(text="travelynx relay bot successfully connected!")

            if (
                not data["reason"] in ("update", "checkin", "ping", "checkout", "undo")
                or not data["status"]["toStation"]["name"]
            ):
                raise web.HTTPNoContent()

            # hopefully debug this mess eventually
            print(userid, data["reason"], train_presentation(data["status"]))

            # when checkin is undone, delete its message
            if data["reason"] == "undo" and not data["status"]["checkedIn"]:
                last_trip = DB.Trip.find_last_trip_for(user.discord_id)
                if not last_trip.status["checkedIn"]:
                    print("sussy")
                    return web.Response(
                        text="Not unpublishing last checkin — you're already checked out. "
                        "In case this is intentional and you want to force deletion, undo your checkout, "
                        "save the journey comment once, and then finally undo your checkin. Sorry for the hassle."
                    )

                messages_to_delete = DB.Message.find_all(
                    user.discord_id, last_trip.journey_id
                )
                for message in messages_to_delete:
                    await message.delete(bot)
                last_trip.delete()

                if current_trips := DB.Trip.find_current_trips_for(user.discord_id):
                    for message in DB.Message.find_all(
                        user.discord_id, current_trips[-1].journey_id
                    ):
                        msg = await message.fetch(bot)
                        await msg.edit(
                            embed=format_travelynx(
                                bot, userid, [trip.status for trip in current_trips]
                            ),
                            view=None,
                        )

                return web.Response(
                    text=f"Unpublished last checkin for {len(messages_to_delete)} channels"
                )

            # don't share completely private checkins, only unlisted and upwards
            if data["status"]["visibility"]["desc"] == "private":
                # just to make sure we don't have it lying around for some reason anyway
                if trip := DB.Trip.find(user.discord_id, zugid(data["status"])):
                    trip.delete()
                return web.Response(
                    text=f'Not publishing private {data["reason"]} in {data["status"]["train"]["type"]} {data["status"]["train"]["no"]}'
                )

            # update database to maintain trip data
            handle_status_update(userid, data["reason"], data["status"])

            current_trips = DB.Trip.find_current_trips_for(user.discord_id)

            # get all channels that live updates get pushed to for this user
            channels = [bot.get_channel(cid) for cid in user.find_live_channel_ids()]
            for channel in channels:
                member = channel.guild.get_member(user.discord_id)
                # don't post if the user has left or can't see the live channel
                if not member or not channel.permissions_for(member).read_messages:
                    continue

                # check if we already have a message for this particular trip
                # edit it if it exists, otherwise create a new one and submit it into the database
                if message := DB.Message.find(
                    userid, zugid(data["status"]), channel.id
                ):
                    # if we get a checkout after another checkin has already been posted (manually)
                    # stop pretending we're at the end of the journey and link to the new ones
                    continue_link = None
                    if newer_message := DB.Message.find_newer_than(
                        userid, channel.id, message.message_id
                    ):
                        continue_link = (await newer_message.fetch(bot)).jump_url
                        current_trip_index = [
                            trip.journey_id for trip in current_trips
                        ].index(zugid(data["status"]))
                        current_trips = current_trips[0 : current_trip_index + 1]

                    msg = await message.fetch(bot)
                    await msg.edit(
                        embed=format_travelynx(
                            bot,
                            userid,
                            [trip.status for trip in current_trips],
                            continue_link=continue_link,
                        ),
                        view=TripActionsView(current_trips[-1]),
                    )
                else:
                    message = await channel.send(
                        embed=format_travelynx(
                            bot, userid, [trip.status for trip in current_trips]
                        ),
                        view=TripActionsView(current_trips[-1]),
                    )
                    DB.Message(
                        zugid(data["status"]), user.discord_id, channel.id, message.id
                    ).write()
                    # shrink previous message to prevent clutter
                    if len(current_trips) > 1 and (
                        prev_message := DB.Message.find(
                            user.discord_id, current_trips[-2].journey_id, channel.id
                        )
                    ):
                        prev_msg = await prev_message.fetch(bot)
                        await prev_msg.edit(
                            embed=format_travelynx(
                                bot,
                                userid,
                                [trip.status for trip in current_trips[0:-1]],
                                continue_link=message.jump_url,
                            ),
                            view=None,
                        )
            return web.Response(
                text=f'Successfully published {data["status"]["train"]["type"]} {data["status"]["train"]["no"]} {data["reason"]} to {len(channels)} channels'
            )

    async def unshortener(req):
        link = DB.Link.find_by_short(short_id=req.match_info["randid"])

        if not link:
            raise web.HTTPNotFound()
            return

        raise web.HTTPFound(link.long_url)

    app = web.Application()
    app.router.add_post("/travelynx", handler)
    app.router.add_get("/s/{randid}", unshortener)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 6005)
    await site.start()


@bot.tree.command(guilds=servers)
@discord.app_commands.describe(
    member="the member whose status to query, defaults to current user"
)
async def zug(ia, member: typing.Optional[discord.Member]):
    "Get current travelynx status for yourself and others."
    if not member:
        member = ia.user

    user = DB.User.find(discord_id=member.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    if user.find_privacy_for(ia.guild.id) == DB.Privacy.ME and not member == ia.user:
        await ia.response.send_message(
            embed=discord.Embed().set_author(
                name=f"{member.name} ist gerade nicht unterwegs",
                icon_url=member.avatar.url,
            )
        )
        return

    async with ClientSession() as session:
        async with session.get(
            f"https://travelynx.de/api/v1/status/{user.token_status}"
        ) as r:
            if r.status == 200:
                status = await r.json()

                if not status["checkedIn"] or (
                    status["visibility"]["desc"] == "private"
                ):
                    await ia.response.send_message(
                        embed=discord.Embed().set_author(
                            name=f"{member.name} ist gerade nicht unterwegs",
                            icon_url=member.avatar.url,
                        )
                    )
                    return

                handle_status_update(member.id, "update", status)
                current_trips = DB.Trip.find_current_trips_for(member.id)

                await ia.response.send_message(
                    embed=format_travelynx(
                        bot, member.id, [trip.status for trip in current_trips]
                    ),
                    view=TripActionsView(current_trips[-1]),
                )


class TripActionsView(discord.ui.View):
    """is attached to embeds, allows users to manually update trip infos
    (for real trips) and copy the checkin (for now only manual trips)"""

    disabled_refresh_button = discord.ui.Button(
        label="Update", style=discord.ButtonStyle.secondary, disabled=True
    )

    def __init__(self, trip):
        super().__init__()
        self.timeout = None
        self.trip = trip
        self.clear_items()

        if "travelhookfaked" in trip.journey_id:
            self.add_item(self.disabled_refresh_button)
            self.add_item(self.manualcopy)
        else:
            status = trip.get_unpatched_status()
            if status["checkedIn"]:
                self.add_item(self.refresh)
            else:
                self.add_item(self.disabled_refresh_button)

            url = f"/s/{status['fromStation']['uic']}?"
            if "|" in status["train"]["id"]:
                url += urllib.parse.urlencode(
                    {"hafas": 1, "trip_id": status["train"]["id"]}
                )
            else:
                url += urllib.parse.urlencode(
                    {"train": f"{status['train']['type']} {status['train']['no']}"}
                )
            self.add_item(
                discord.ui.Button(
                    label="Copy this checkin", url="https://travelynx.de" + url
                )
            )

    @discord.ui.button(label="Update", style=discord.ButtonStyle.secondary)
    async def refresh(self, ia, _):
        """refresh real trips from travelynx api. this button is deleted from the view
        and replaced with a disabled button for fake checkins"""
        user = DB.User.find(discord_id=self.trip.user_id)
        async with ClientSession() as session:
            async with session.get(
                f"https://travelynx.de/api/v1/status/{user.token_status}"
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if data["checkedIn"] and self.trip.journey_id == zugid(data):
                        handle_status_update(self.trip.user_id, "update", data)
                        current_statuses = [
                            trip.status
                            for trip in DB.Trip.find_current_trips_for(
                                self.trip.user_id
                            )
                        ]
                        await ia.response.edit_message(
                            embed=format_travelynx(
                                bot, self.trip.user_id, current_statuses
                            ),
                            view=self,
                        )
                    else:
                        await ia.response.send_message(
                            "Die Fahrt ist bereits zu Ende.", ephemeral=True
                        )

    @discord.ui.button(label="Copy this checkin", style=discord.ButtonStyle.secondary)
    async def manualcopy(self, ia, _):
        """copy fake trips for yourself. this button is deleted from the view
        and replaced with a link to travelynx for real checkins."""
        user = DB.User.find(discord_id=ia.user.id)
        if not user:
            await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
            return

        # update, just in case we missed some edits maybe
        self.trip = DB.Trip.find(self.trip.user_id, self.trip.journey_id)
        departure = self.trip.status["fromStation"]["scheduledTime"]
        departure_delay = (
            departure - self.trip.status["fromStation"]["scheduledTime"]
        ) // 60
        arrival = self.trip.status["toStation"]["scheduledTime"]
        arrival_delay = (arrival - self.trip.status["toStation"]["scheduledTime"]) // 60
        await manualtrip.callback(
            ia,
            self.trip.status["fromStation"]["name"],
            f"{datetime.fromtimestamp(departure, tz=tz):%H:%M}",
            self.trip.status["toStation"]["name"],
            f"{datetime.fromtimestamp(arrival, tz=tz):%H:%M}",
            f"{self.trip.status['train']['type']} {self.trip.status['train']['line']}",
            self.trip.status["train"]["fakeheadsign"],
            departure_delay,
            arrival_delay,
        )


configure = discord.app_commands.Group(
    name="configure", description="edit your settings with the relay bot"
)


@configure.command()
@discord.app_commands.describe(
    level="leave empty to query current level. set to ME to only allow you to use /zug, set to EVERYONE to allow everyone to use /zug and set to LIVE to activate the live feed."
)
async def privacy(ia, level: typing.Optional[DB.Privacy]):
    "Query or change your current privacy settings on this server."

    def explain(level: typing.Optional[DB.Privacy]):
        desc = "This means that, on this server,\n"
        match level:
            case DB.Privacy.ME:
                desc += "- Only you can use the **/zug** command to share your current journey."
            case DB.Privacy.EVERYONE:
                desc += "- Everyone can use the **/zug** command to see your current journey."
            case DB.Privacy.LIVE:
                desc += "- Everyone can use the **/zug** command to see your current journey.\n"
                if live_channel := DB.Server.find(ia.guild.id).live_channel:
                    desc += f"- Live updates will posted into {bot.get_channel(live_channel).mention} with your entire journey."
                else:
                    desc += (
                        "- Live updates with your entire journey can be posted into a dedicated channel.\n"
                        "- Note: This server has not set up a live channel. No live updates will be posted until it is set up."
                    )
        desc += "\n- Note: If your checkin is set to **private visibility** on travelynx, this bot will not post it anywhere."
        return desc

    if user := DB.User.find(discord_id=ia.user.id):
        if level is None:
            level = user.find_privacy_for(ia.guild.id)
            await ia.response.send_message(
                f"Your privacy level is set to **{level.name}**. {explain(level)}"
            )
        else:
            user.set_privacy_for(ia.guild_id, level)
            await ia.response.send_message(
                f"Your privacy level has been set to **{level.name}**. {explain(level)}"
            )
    else:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)


@configure.command()
async def suggestions(ia):
    "Edit autocomplete suggestions for your manual checkins."

    class EnterAutocompleteModal(
        discord.ui.Modal, title="Manual trip station autocompletes"
    ):
        suggestions_input = discord.ui.TextInput(
            label="One station per line, please",
            style=discord.TextStyle.paragraph,
            required=False,
        )

        def __init__(self, user):
            self.user = DB.User.find(user.id)
            self.suggestions_input.default = self.user.suggestions
            super().__init__()

        async def on_submit(self, ia):
            self.user.write_suggestions(self.suggestions_input.value)
            await ia.response.send_message(
                "Successfully updated your autocomplete suggestions!", ephemeral=True
            )

    await ia.response.send_modal(EnterAutocompleteModal(ia.user))


journey = discord.app_commands.Group(
    name="journey", description="edit and fix the journeys tracked by the relay bot."
)

Choice = discord.app_commands.Choice


@journey.command(name="break")
@discord.app_commands.choices(
    break_mode=[
        Choice(
            name="Natural — Transfer between nearby stops with less than two hours of waiting.",
            value=int(DB.BreakMode.NATURAL),
        ),
        Choice(
            name="Force Break — Never transfer. New checkins start a new journey.",
            value=int(DB.BreakMode.FORCE_BREAK),
        ),
        Choice(
            name="Force Glue — Always transfer. New checkins never start a new journey.",
            value=int(DB.BreakMode.FORCE_GLUE),
        ),
    ]
)
async def _break(ia, break_mode: Choice[int]):
    "Control whether your next checkin should start a new journey or if it's just a transfer."
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    break_mode = DB.BreakMode(break_mode.value)
    user.set_break_mode(break_mode)
    match break_mode:
        case DB.BreakMode.NATURAL:
            await ia.response.send_message(
                "Your next checkin will start a new journey if its and your last checkin's stations are more than "
                "two kilometers apart. It will also start a new journey if you wait more than two hours after "
                "your last checkin.",
                ephemeral=True,
            )
        case DB.BreakMode.FORCE_BREAK:
            await ia.response.send_message(
                "Your next checkin will start a new journey. "
                "After your next checkin, this setting will revert to *Natural*.",
                ephemeral=True,
            )
        case DB.BreakMode.FORCE_GLUE:
            await ia.response.send_message(
                "Your next checkin will **not** start a new journey. "
                "After your next checkin, this setting will revert to *Natural*.",
                ephemeral=True,
            )


async def manual_station_autocomplete(ia, current):
    suggestions = []
    if user := DB.User.find(ia.user.id):
        trips = DB.Trip.find_current_trips_for(user.discord_id)
        for trip in trips:
            suggestions += [
                trip.status["toStation"]["name"],
                trip.status["fromStation"]["name"],
            ]
        suggestions += user.suggestions.split("\n")
        suggestions = [s for s in suggestions if current.casefold() in s.casefold()]

    return [Choice(name=s, value=s) for s in set(suggestions)]


@journey.command()
@discord.app_commands.describe(
    from_station="the name of the station you're departing from",
    departure="HH:MM departure according to the timetable",
    departure_delay="minutes of delay",
    to_station="the name of the station you will arrive at",
    arrival="HH:MM arrival according to the timetable",
    arrival_delay="minutes of delay",
    train="train type and line/number like 'S 42'. also try 'walk 1km', 'bike 3km', 'car 3km', 'plane LH3999'",
)
@discord.app_commands.autocomplete(
    from_station=manual_station_autocomplete,
    to_station=manual_station_autocomplete,
    headsign=manual_station_autocomplete,
)
async def manualtrip(
    ia,
    from_station: str,
    departure: str,
    to_station: str,
    arrival: str,
    train: str,
    headsign: str,
    departure_delay: typing.Optional[int] = 0,
    arrival_delay: typing.Optional[int] = 0,
    comment: typing.Optional[str] = "",
):
    "Manually add a check-in not available on HAFAS/IRIS to your journey."
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    departure = parse_manual_time(departure)
    arrival = parse_manual_time(arrival)
    if arrival < departure:
        arrival += timedelta(days=1)
    status = {
        "checkedIn": False,
        "comment": comment or "",
        "actionTime": int(datetime.now(tz=tz).timestamp()),
        "fromStation": {
            "uic": 42,
            "ds100": None,
            "name": from_station,
            "latitude": 0.0,
            "longitude": 0.0,
            "scheduledTime": int(departure.timestamp()),
            "realTime": int(departure.timestamp()) + (departure_delay * 60),
        },
        "toStation": {
            "uic": 69,
            "ds100": None,
            "name": to_station,
            "latitude": 0.0,
            "longitude": 0.0,
            "scheduledTime": int(arrival.timestamp()),
            "realTime": int(arrival.timestamp()) + (arrival_delay * 60),
        },
        "intermediateStops": [],
        "train": {
            "fakeheadsign": headsign,
            "type": train.split(" ")[0],
            "line": " ".join(train.split(" ")[1:]),
            "no": "",
            "id": "travelhookfaked" + secrets.token_urlsafe(),
            "hafasId": None,
        },
        "visibility": {"desc": "public", "level": 100},
    }
    webhook = {"reason": "checkout", "status": status}
    async with ClientSession() as session:
        async with session.post(
            "http://localhost:6005/travelynx",
            json=webhook,
            headers={"Authorization": f"Bearer {user.token_webhook}"},
        ) as r:
            await ia.response.send_message(
                f"{r.status} {await r.text()}", ephemeral=True
            )


def render_patched_train(trip, patch):
    "helper method to render a preview of how a train will look with a different patch applied"
    status = json.loads(
        DB.DB.execute(
            "SELECT json_patch(?,?) as status",
            (json.dumps(trip.get_unpatched_status()), json.dumps(patch)),
        ).fetchone()["status"]
    )
    train_type, train_line, link = train_presentation(status)
    departure = format_time(
        status["fromStation"]["scheduledTime"],
        status["fromStation"]["realTime"],
    )
    arrival = format_time(
        status["toStation"]["scheduledTime"], status["toStation"]["realTime"]
    )
    headsign = fetch_headsign(status)
    train_line = f"**{train_line}**" if train_line else ""
    if "travelhookfaked" in status["train"]["id"]:
        return f"{get_train_emoji(train_type)} {train_line} » {headsign}\n{status['fromStation']['name']} {departure} → {status['toStation']['name']} {arrival}\n"
    else:
        return f"{get_train_emoji(train_type)} {train_line} [» {headsign}]({link})\n{status['fromStation']['name']} {departure} → {status['toStation']['name']} {arrival}\n"


@journey.command()
async def undo(ia):
    "undo your last checkin in the bot's database. you must be checked out to do this. you will be asked to confirm the undo action."
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    trip = DB.Trip.find_last_trip_for(user.discord_id)
    if trip.status["checkedIn"]:
        await ia.response.send_message(
            "You're still checked in. Please undo this checkin on travelynx to avoid inconsistent data.",
            ephemeral=True,
        )
        return

    await ia.response.send_message(
        embed=discord.Embed(
            description=f"### You are about to undo the following checkin from {format_time(None, trip.from_time, True)}\n"
            f"{render_patched_train(trip, {})}\n"
            "The checkin will only be deleted from the bot's database. Please confirm deletion by clicking below.",
            color=train_type_color["SB"],
        ),
        ephemeral=True,
        view=UndoView(user, trip),
    )
    return


class UndoView(discord.ui.View):
    "confirmation button for the journey undo command"

    def __init__(self, user, trip):
        "we store the trip and user objects relevant for our undo process"
        super().__init__()
        self.user = user
        self.trip = trip

    @discord.ui.button(label="Yes, undo this trip.", style=discord.ButtonStyle.danger)
    async def doit(self, ia, _):
        "once clicked, send a mocked undo checkin request to the webhook"
        status = self.trip.get_unpatched_status()
        status["checkedIn"] = True
        DB.Trip.upsert(self.user.discord_id, status)
        async with ClientSession() as session:
            status["checkedIn"] = False
            async with session.post(
                "http://localhost:6005/travelynx",
                json={"reason": "undo", "status": status},
                headers={"Authorization": f"Bearer {self.user.token_webhook}"},
            ) as r:
                await ia.response.edit_message(
                    content=f"{r.status} {await r.text()}", embed=None, view=None
                )


@journey.command()
async def delay(ia, departure: typing.Optional[int], arrival: typing.Optional[int]):
    "quickly add a delay not reflected in HAFAS to your journey"
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    trip = DB.Trip.find_last_trip_for(user.discord_id)
    if not trip:
        await ia.response.send_message(
            "Sorry, but the bot doesn't have a trip saved for you currently.",
            ephemeral=True,
        )
        return

    prepare_patch = {}
    if departure is not None:
        prepare_patch["fromStation"] = {
            "realTime": trip.status["fromStation"]["scheduledTime"] + departure * 60
        }

    if arrival is not None:
        prepare_patch["toStation"] = {
            "realTime": trip.status["toStation"]["scheduledTime"] + arrival * 60
        }

    newpatch = DB.DB.execute(
        "SELECT json_patch(?,?) AS newpatch",
        (json.dumps(trip.status_patch), json.dumps(prepare_patch)),
    ).fetchone()["newpatch"]

    trip.write_patch(json.loads(newpatch))

    reason = "update" if trip.status["checkedIn"] else "checkout"
    async with ClientSession() as session:
        async with session.post(
            "http://localhost:6005/travelynx",
            json={"reason": reason, "status": trip.get_unpatched_status()},
            headers={"Authorization": f"Bearer {user.token_webhook}"},
        ) as r:
            if r.status == 200:
                msg = await DB.Message.find(
                    trip.user_id, trip.journey_id, ia.channel.id
                ).fetch(bot)

                train_type, train_line, link = train_presentation(trip.status)
                headsign = fetch_headsign(trip.status)
                train_line = f"**{train_line}**" if train_line else ""

                embed = discord.Embed(
                    description=f"{get_train_emoji(train_type)} {train_line} » {headsign} "
                    f"is delayed by **+{departure or 0}′/+{arrival or 0}′**.\n### {msg.jump_url} ",
                    color=train_type_color["SB"],
                ).set_author(
                    name=f"{ia.user.name} ist {'nicht' if max(arrival, departure) == 0 else ''} verspätet",
                    icon_url=ia.user.avatar.url,
                )

                await ia.response.send_message(content=None, embed=embed, view=None)
            else:
                await ia.response.send_message(
                    content=f"{r.status} {await r.text()}", embed=None, view=None
                )


@journey.command()
async def kas(ia):
    "turn your S into a KAS"
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    trip = DB.Trip.find_last_trip_for(user.discord_id)
    if not trip:
        await ia.response.send_message(
            "Sorry, but the bot doesn't have a trip saved for you currently.",
            ephemeral=True,
        )
        return

    newpatch = DB.DB.execute(
        "SELECT json_patch(?,?) AS newpatch",
        (json.dumps(trip.status_patch), json.dumps({"train": {"type": "KAS"}})),
    ).fetchone()["newpatch"]

    trip.write_patch(json.loads(newpatch))

    reason = "update" if trip.status["checkedIn"] else "checkout"
    async with ClientSession() as session:
        async with session.post(
            "http://localhost:6005/travelynx",
            json={"reason": reason, "status": trip.get_unpatched_status()},
            headers={"Authorization": f"Bearer {user.token_webhook}"},
        ) as r:
            await ia.response.send_message(content="🧇", embed=None, view=None)


@journey.command()
async def edit(
    ia,
    from_station: typing.Optional[str],
    departure: typing.Optional[str],
    departure_delay: typing.Optional[int],
    to_station: typing.Optional[str],
    arrival: typing.Optional[str],
    arrival_delay: typing.Optional[int],
    train: typing.Optional[str],
    headsign: typing.Optional[str],
    comment: typing.Optional[str],
):
    "manually overwrite some data of your current trip. you will be asked to confirm your changes."
    user = DB.User.find(discord_id=ia.user.id)
    if not user:
        await ia.response.send_message(embed=not_registered_embed, ephemeral=True)
        return

    trip = DB.Trip.find_last_trip_for(user.discord_id)
    if not trip:
        await ia.response.send_message(
            "Sorry, but the bot doesn't have a trip saved for you currently.",
            ephemeral=True,
        )
        return

    prepare_patch = {}
    if from_station or departure or departure_delay:
        prepare_patch["fromStation"] = {"name": from_station}
        if departure:
            departure = parse_manual_time(departure)
            prepare_patch["fromStation"]["scheduledTime"] = int(departure.timestamp())
            departure_delay = departure_delay or 0
        else:
            departure = datetime.fromtimestamp(
                trip.status["fromStation"]["scheduledTime"], tz=tz
            )

        if departure_delay is not None:
            prepare_patch["fromStation"]["realTime"] = int(departure.timestamp()) + (
                (departure_delay or 0) * 60
            )

    if to_station or arrival or arrival_delay:
        prepare_patch["toStation"] = {"name": to_station}
        if arrival:
            arrival = parse_manual_time(arrival)
            prepare_patch["toStation"]["scheduledTime"] = int(arrival.timestamp())
            arrival_delay = arrival_delay or 0
        else:
            arrival = datetime.fromtimestamp(
                trip.status["toStation"]["scheduledTime"], tz=tz
            )

        if arrival_delay is not None:
            prepare_patch["toStation"]["realTime"] = int(arrival.timestamp()) + (
                (arrival_delay or 0) * 60
            )

    if train or headsign:
        prepare_patch["train"] = {
            "fakeheadsign": headsign,
        }
        if train:
            prepare_patch["train"]["type"] = train.split(" ")[0]
            prepare_patch["train"]["line"] = " ".join(train.split(" ")[1:])

    prepare_patch["comment"] = comment

    newpatch = DB.DB.execute(
        "SELECT json_patch(?,?) AS newpatch",
        (json.dumps(trip.status_patch), json.dumps(prepare_patch)),
    ).fetchone()["newpatch"]

    newpatched_status = DB.DB.execute(
        "SELECT json_patch(?,?) as newpatched_status", (trip.travelynx_status, newpatch)
    ).fetchone()["newpatched_status"]

    newpatch = json.loads(newpatch)
    newpatched_status = json.loads(newpatched_status)

    await ia.response.send_message(
        embed=discord.Embed(
            description=f"### You are about to edit the following checkin from {format_time(None, trip.from_time, True)}\n"
            "Current state:\n"
            f"{render_patched_train(trip, trip.status_patch)}\n"
            "With your changes:\n"
            f"{render_patched_train(trip, newpatch)}\n"
            "You can immediately apply these changes or double-check and make further edits with the manual editor "
            "using [TOML](https://toml.io), e.g.:"
            '```toml\nfromStation.name = "Nürnberg Ziegelstein"\ntrain = { type = "U", line = "11" }\n```\n'
            "For available fields, see [travelynx's API documentation](https://travelynx.de/api).",
            color=train_type_color["SB"],
        ),
        view=EditTripView(trip, newpatch),
        ephemeral=True,
    )


class EditTripView(discord.ui.View):
    "provide a button to edit the trip status patch"

    def __init__(self, trip, newpatch):
        self.trip = trip
        self.newpatch = newpatch
        self.modal = self.EnterStatusPatchModal(self, self.trip, self.newpatch)
        super().__init__()

    def attachnewmodal(self, newpatch):
        """so for some reason we can't reuse the editor modal, so we create a new
        one with the same data every time the editor is closed"""
        self.newpatch = newpatch
        self.modal = self.EnterStatusPatchModal(self, self.trip, self.newpatch)

    @discord.ui.button(label="Commit my edits now.", style=discord.ButtonStyle.green)
    async def commit(self, ia, _):
        "write newpatch into the database and issue a mocked update webhook"
        self.trip.write_patch(self.newpatch)
        self.trip = DB.Trip.find(
            self.trip.user_id, self.trip.journey_id
        )  # update, just in case
        reason = "update" if self.trip.status["checkedIn"] else "checkout"
        async with ClientSession() as session:
            async with session.post(
                "http://localhost:6005/travelynx",
                json={"reason": reason, "status": self.trip.get_unpatched_status()},
                headers={
                    "Authorization": f"Bearer {DB.User.find(self.trip.user_id).token_webhook}"
                },
            ) as r:
                await ia.response.edit_message(
                    content=f"{r.status} {await r.text()}", embed=None, view=None
                )

    @discord.ui.button(
        label="Open the manual editor instead.", style=discord.ButtonStyle.grey
    )
    async def edit(self, ia, _):
        "open the editor, reshow the changes and wait for confirmation"
        await ia.response.send_modal(self.modal)

    class EnterStatusPatchModal(
        discord.ui.Modal, title="Dingenskirchen® Advanced Train Editor™"
    ):
        patch_input = discord.ui.TextInput(
            label="Status edits (TOML)",
            style=discord.TextStyle.paragraph,
            required=False,
        )

        def __init__(self, parent, trip, newpatch):
            self.parent = parent
            self.trip = trip
            self.newpatch = newpatch
            self.patch_input.default = tomli_w.dumps(newpatch)  # wow much efficiency
            super().__init__()

        async def on_submit(self, ia):
            self.newpatch = tomli.loads(self.patch_input.value)
            self.patch_input.default = tomli_w.dumps(self.newpatch)
            self.parent.attachnewmodal(self.newpatch)
            await ia.response.edit_message(
                embed=discord.Embed(
                    description="Current state:\n"
                    f"{render_patched_train(self.trip, self.trip.status_patch)}\n"
                    "With your changes:\n"
                    f"{render_patched_train(self.trip, self.newpatch)}\n"
                    "Click commit to confirm or edit again.",
                    color=train_type_color["SB"],
                )
            )


bot.tree.add_command(configure, guilds=servers)
bot.tree.add_command(journey, guilds=servers)


@bot.tree.command(guilds=servers)
@discord.app_commands.rename(from_station="from", to_station="to")
@discord.app_commands.autocomplete(
    from_station=manual_station_autocomplete, to_station=manual_station_autocomplete
)
async def walk(
    ia,
    from_station: str,
    to_station: str,
    departure: str,
    arrival: str,
    name: typing.Optional[str],
    actually_bike_instead: typing.Optional[bool],
    comment: typing.Optional[str],
):
    "do a manual trip walking, see /journey manualtrip"
    train = f"walk {name or 'walking…'}"
    if actually_bike_instead:
        train = f"bike {name or 'cycling…'}"
    await manualtrip.callback(
        ia,
        from_station,
        departure,
        to_station,
        arrival,
        train,
        to_station,
        0,
        0,
        comment,
    )


@bot.tree.command(guilds=servers)
async def pleasegivemetraintypes(ia):
    "print all the train types the bot knows about"
    fv = [
        "D",
        "EC",
        "ECE",
        "EN",
        "EST",
        "FLX",
        "IC",
        "ICE",
        "IR",
        "TGV",
    ]
    regio = ["IRE", "L", "MEX", "RB", "RE", "SPR", "ST", "TER"]
    sbahn = ["KAS", "SL", "RER", "RS", "S", "SN"]
    transit = ["AST", "Bus", "BusX", "Fähre", "M", "RUF", "Schw-B", "STB", "STR", "U"]
    special = [
        "A",
        "CB",
        "RT",
        "SB",
        "SEV",
        "Ü",
    ]
    manual = ["bike", "boat", "car", "coach", "plane", "steam", "walk"]
    austria = [
        "ATS",
        "CJX",
        "NJ",
        "R",
        "REX",
        "RJ",
        "RJX",
        "WB",
    ]
    poland = ["EIC", "KM", "KML", "KS", "TLK"]
    nürnberg = ["U1n", "U2n", "U3n"]
    wien = ["U1", "U2", "U3", "U4", "U5", "U6", "WLB"]
    üstra = [
        "Ü1",
        "Ü2",
        "Ü3",
        "Ü4",
        "Ü5",
        "Ü6",
        "Ü7",
        "Ü8",
        "Ü9",
        "Ü10",
        "Ü11",
        "Ü12",
        "Ü13",
        "Ü17",
    ]
    berlin = ["U1b", "U2b", "U3b", "U4b", "U5b", "U6b", "U7b", "U8b", "U9b", "U12b"]
    münchen = ["U1m", "U2m", "U3m", "U4m", "U5m", "U6m", "U7m", "U8m"]
    hamburg = ["U1h", "U2h", "U3h", "U4h"]

    def render_emojis(train_types):
        return "\n".join([f"`{tt:>6}` {train_type_emoji[tt]}" for tt in train_types])

    transit_lines_a = f"""
    The following cities' transit lines are automatically detected and get colored icons:
    **Berlin** (`U1b` … `U12b`) {' '.join([train_type_emoji[tt] for tt in berlin])} 
    **Hamburg** (`U1h` … `U4h`) {' '.join([train_type_emoji[tt] for tt in hamburg])} 
    **Hannover** (`Ü1`…`Ü17`) {' '.join([train_type_emoji[tt] for tt in üstra])}
    """
    transit_lines_b = f"""
    **München** (`U1m` … `U8m`) {' '.join([train_type_emoji[tt] for tt in münchen])}
    **Nürnberg** (`U1n`…`U3n`) {' '.join([train_type_emoji[tt] for tt in nürnberg])}
    **Wien** (`U1`…`U6`, `WLB`) {' '.join([train_type_emoji[tt] for tt in wien])}
    """

    embed = (
        discord.Embed(
            description=f"**The relay bot currently knows {len(train_type_emoji)} emoji for train types.**\n"
            f"It also automatically rewrites the following train types:\n{', '.join([f'`{k}` → `{v}`' for k,v in blanket_replace_train_type.items()])}.\n"
        )
        .add_field(name="Long-distance", value=render_emojis(fv))
        .add_field(name="Regional", value=render_emojis(regio))
        .add_field(name="S-Bahn", value=render_emojis(sbahn))
        .add_field(name="City transit", value=render_emojis(transit))
        .add_field(name="Specials", value=render_emojis(special))
        .add_field(name="For manual checkins", value=render_emojis(manual))
        .add_field(name="Austria", value=render_emojis(austria))
        .add_field(name="Poland", value=render_emojis(poland))
        .add_field(name="Transit line numbers", inline=False, value=transit_lines_a)
        .add_field(name="… continued", inline=False, value=transit_lines_b)
    )

    if s := set(train_type_emoji.keys()) - set(
        fv
        + regio
        + sbahn
        + transit
        + special
        + manual
        + austria
        + poland
        + üstra
        + nürnberg
        + wien
        + hamburg
        + berlin
        + münchen
    ):
        embed = embed.add_field(name="uncategorized", value=render_emojis(s))

    await ia.response.send_message(embed=embed)


@bot.tree.command(guilds=servers)
async def register(ia):
    "Register with the travelynx relay bot and share your journeys today!"
    await ia.response.send_message(
        embed=discord.Embed(
            title="Registering with the travelynx relay bot",
            color=train_type_color["SB"],
            description="Thanks for your interest! Using this bot, you can share your public transport journeys "
            "in and around Germany (or in fact, any journey around the world, using the bot's manual checkin feature) "
            "with your friends and enemies on Discord.\nTo use it, you first need to sign up for **[travelynx](https://travelynx.de)**"
            " to be able to check in into trains, trams, buses and so on. Then you can connect this bot to "
            "your travelynx account.\nFinally, for every server you can decide if *only you* want to share "
            "some of our journeys using the **/zug** command (this is the default), or if you want to let *everyone* "
            "use the command for you. You can even enable a **live feed** for a specific channel, keeping everyone "
            "up to date as you check in into new transports. This is fully optional.",
        ).set_thumbnail(
            url="https://cdn.discordapp.com/emojis/1160275971266576494.webp"
        ),
        view=RegisterTravelynxStepZero(),
    )


class RegisterTravelynxStepZero(discord.ui.View):
    """view attached to the /register initial response, is persistent over restarts.
    first we check that we aren't already registered, then offer to proceed to step 1 with token modal
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Connect travelynx account",
        style=discord.ButtonStyle.green,
        custom_id=config["register_button_id"],
    )
    async def doit(self, ia, _):
        "button to proceed to step 1"
        if DB.User.find(discord_id=ia.user.id):
            await ia.response.send_message(
                embed=discord.Embed(
                    title="Oops!",
                    color=train_type_color["U1"],
                    description="It looks like you're already registered. If you want to reset your tokens or need "
                    "any other assistance, please ask the bot operator.",
                ),
                ephemeral=True,
            )
            return

        await ia.response.send_message(
            embed=discord.Embed(
                title="Step 1/3: Connect Status API",
                color=train_type_color["SB"],
                description="For the first step, you'll need to give the bot read access to your travelynx account. "
                "To do this, head over to the [**travelynx Account page**](https://travelynx.de/account) "
                "while signed in, scroll down to «API», and click **Generate** in the table row with «Status».\n"
                " Copy the token you just generated from the row. Return here, click «I have my token ready.» "
                "below and enter the token into the pop-up.",
            ).set_image(url="https://i.imgur.com/Tu2Zm6C.png"),
            view=RegisterTravelynxStepOne(),
            ephemeral=True,
        )


class RegisterTravelynxStepOne(discord.ui.View):
    """view attached to the step 1 ephemeral response. ask for and verify the status token,
    register the user, then offer to proceed to step 2 to copy live feed/webhook credentials.
    """

    @discord.ui.button(label="I have my token ready.", style=discord.ButtonStyle.green)
    async def doit(self, ia, _):
        "just send the modal when the user has the token ready"
        await ia.response.send_modal(self.EnterTokenModal())

    class EnterTokenModal(discord.ui.Modal, title="Please enter your travelynx token"):
        "this contains the actual verification and registration code."
        token = discord.ui.TextInput(label="Status token")

        async def on_submit(self, ia):
            """triggered on modal submit, if everything is fine, register the user and
            edit ephemeral response to proceed to step 2, else ask them to try again"""
            token = self.token.value.strip()
            if await is_token_valid(token):
                DB.User(
                    discord_id=ia.user.id,
                    token_status=token,
                    token_webhook=secrets.token_urlsafe(),
                    token_travel=None,
                    break_journey=DB.BreakMode.NATURAL,
                    suggestions="",
                ).write()
                await ia.response.edit_message(
                    embed=discord.Embed(
                        title="Step 2/3: Connect Live Feed (optional)",
                        color=train_type_color["SB"],
                        description="Great! You've successfully connected your status token to the relay bot. "
                        "You now use **/zug** for yourself and configure if others can use it with **/configure privacy**.\n"
                        "Optionally, you can now also sign up for the live feed by connecting travelynx's webhook "
                        "to the relay bot's live feed feature. You can also skip this if you're not interested in the live "
                        "feed. Should you change your mind later, you can bother the bot operator about it.",
                    ),
                    view=RegisterTravelynxStepTwo(),
                )
            else:
                await ia.response.edit_message(
                    embed=discord.Embed(
                        title="Step 1/3: Connect Status API",
                        color=train_type_color["U1"],
                        description="### ❗ The token doesn't seem to be valid, please check it and try again.\n"
                        "For the first step, you'll need to give the bot read access to your travelynx account. "
                        "To do this, head over to the [**Account page**](https://travelynx.de/account) "
                        "while signed in, scroll down to «API», and click **Generate** in the table row with «Status».\n"
                        " Copy the token you just generated from the row. Return here, click «I have my token ready.» "
                        "below and enter the token into the pop-up.",
                    ).set_image(url="https://i.imgur.com/Tu2Zm6C.png")
                )


class RegisterTravelynxStepTwo(discord.ui.View):
    "view triggered by successful registration in step 1, show credentials for live feed if asked"

    @discord.ui.button(label="Connect live feed", style=discord.ButtonStyle.green)
    async def doit(self, ia, _):
        "offer the live feed webhook credentials for copying"
        token = DB.User.find(ia.user.id).token_webhook
        await ia.response.edit_message(
            embed=discord.Embed(
                title="Step 3/3: Connect live feed (optional)",
                color=train_type_color["S"],
                description="Congratulations! You can now use **/zug** and **/configure privacy** to share your logged "
                "journeys on Discord.\n\nWith the live feed enabled on a server, once your server admins have set up a "
                "live channel  *that you can see yourself*, the relay bot will automatically post non-private "
                "checkins and try to keep your journey up to date. To connect travelynx's update webhook "
                "with the relay bot, you need to head to the [**Account » Webhook page**](https://travelynx.de/account/hooks), "
                "check «Aktiv» and enter the following values: \n\n"
                f"**URL**\n```{config['webhook_url']}```\n"
                f"**Token**\n```{token}```\n\n"
                "Once you've done that, save the webhook settings, and you should be able to read "
                "«travelynx relay bot successfully connected!» in the server response. If that doesn't happen, "
                "bother the bot operator about it.\nIf you changed your mind and don't want to connect right now, "
                "bother the bot operator about it once you've decided otherwise again. Until you copy in the settings, "
                "no live connection will be made.\n\n"
                "**Note:** Once you've set up the live feed with this, you also **need to enable it** for every server. "
                "To do this, run **/configure privacy LIVE** on the server you want to enable it for. To enable it on this server, "
                "you can also click the button below now.",
            ).set_image(url="https://i.imgur.com/LhsH8Nt.png"),
            view=RegisterTravelynxEnableLiveFeed(),
        )

    @discord.ui.button(label="No, I don't want that.", style=discord.ButtonStyle.grey)
    async def dontit(self, ia, _):
        "just wish them a nice day"
        await ia.response.edit_message(
            embed=discord.Embed(
                title="Step 3/3: Done!",
                color=train_type_color["S"],
                description="Congratulations! You can now use **/zug** and **/configure privacy** to share your logged journeys on Discord.",
            ),
            view=None,
        )


class RegisterTravelynxEnableLiveFeed(discord.ui.View):
    "after registration, offer to change the privacy settings for the current server"

    @discord.ui.button(
        label="Enable live feed for this server", style=discord.ButtonStyle.red
    )
    async def doit(self, ia, _):
        await privacy.callback(ia, DB.Privacy.LIVE)


def main():
    "the function."
    bot.run(config["token"])


if __name__ == "__main__":
    main()
