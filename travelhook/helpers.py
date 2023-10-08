"various helper functions that do more than just pure formatting logic. the icon library lives in here too"
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from enum import IntEnum
import traceback
import json

import discord
from haversine import haversine
from pyhafas import HafasClient
from pyhafas.profile import DBProfile


def zugid(data):
    """identify a user-trip by its departure time + hafas/iris specific trip id.
    forms the primary key of the trips table together with user id."""
    return str(data["fromStation"]["scheduledTime"]) + data["train"]["id"]


class Privacy(IntEnum):
    "users' per-server privacy setting for /zug. LIVE enables the webhook for that server."
    ME = 0
    EVERYONE = 5
    LIVE = 10


# globally used timezone
tz = ZoneInfo("Europe/Berlin")
hafas = HafasClient(DBProfile())


def is_new_journey(database, status, userid):
    "determine if the user has merely changed into a new transport or if they have started another journey altogether"

    if last_journey := database.execute(
        "SELECT to_lat, to_lon, to_time, travelynx_status FROM trips WHERE user_id = ? ORDER BY from_time DESC LIMIT 1;",
        (userid,),
    ).fetchone():
        if (
            status["train"]["id"]
            == json.loads(last_journey["travelynx_status"])["train"]["id"]
        ):
            return False

        next_journey = status["fromStation"]

        change_distance = haversine(
            (last_journey["to_lat"] or 0.0, last_journey["to_lon"] or 0.0),
            (
                next_journey["latitude"] or 90.0,
                next_journey["longitude"] or 90.0,
            ),
        )
        change_duration = datetime.fromtimestamp(
            next_journey["realTime"], tz=tz
        ) - datetime.fromtimestamp(last_journey["to_time"], tz=tz)

        return (change_distance > 2.0) or change_duration > timedelta(hours=2)

    return True


def format_time(sched, actual, relative=False):
    """render a nice timestamp for arrival/departure that includes delay information.
    relative=True creates a discord relative timestamp that looks like "in 3 minutes"
    and updates automatically, used for the embed's final destination arrival time.
    """
    time = datetime.fromtimestamp(actual, tz=tz)

    if relative:
        return f"<t:{int(time.timestamp())}:R>"

    diff = ""
    if actual > sched:
        diff = (actual - sched) // 60
        diff = f" +{diff}′"

    return f"**{time:%H:%M}{diff}**"


def fetch_headsign(database, status):
    "try to fetch a train headsign or destination name from HAFAS"

    # have we already fetched the headsign? just use that.
    cached = database.execute(
        "SELECT headsign FROM trips WHERE journey_id = ?", (zugid(status),)
    ).fetchone()
    if cached and cached["headsign"]:
        return cached["headsign"]

    def get_headsign_from_jid(jid):
        headsign = hafas.trip(jid).destination.name
        return replace_headsign.get(
            (
                status["train"]["type"]
                + (status["train"]["line"] or status["train"]["no"]),
                headsign,
            ),
            headsign,
        )

    headsign = "?"
    # first let's try to get the train directly using its hafas jid
    try:
        jid = status["train"]["hafasId"] or status["train"]["id"]
        if "|" in jid:
            headsign = get_headsign_from_jid(jid)
            database.execute(
                "UPDATE trips SET headsign = ? WHERE journey_id = ?",
                (
                    headsign,
                    zugid(status),
                ),
            )
            return headsign

    except:  # pylint: disable=bare-except
        print("error fetching headsign from hafas jid:")
        traceback.print_exc()

    # ok that didn't work out somehow, let's do a wild guess which train we're on instead
    try:
        departure = datetime.fromtimestamp(
            status["fromStation"]["scheduledTime"], tz=tz
        )
        arrival = datetime.fromtimestamp(status["toStation"]["scheduledTime"], tz=tz)

        # get suggested trips for our journey
        candidates = hafas.journeys(
            origin=status["fromStation"]["uic"],
            destination=status["toStation"]["uic"],
            date=departure,
            max_changes=0,
        )
        # filter out all that aren't at the time our trip is
        candidates = [
            c
            for c in candidates
            if c.legs[0].departure == departure and c.legs[0].arrival == arrival
        ]
        if len(candidates) == 1:
            headsign = get_headsign_from_jid(candidates[0].legs[0].id)
        else:
            candidates = [
                c
                for c in candidates
                if c.legs[0].name.removeprefix(status["train"]["type"]).strip()
                == (status["train"]["line"] or status["train"]["no"])
            ]
            if len(candidates) == 1:
                headsign = get_headsign_from_jid(candidates[0].legs[0].id)
            else:
                # yeah i give up
                print(status["fromStation"], status["toStation"], departure, candidates)
    except:  # pylint: disable=bare-except
        print("error fetching headsign from journey:")
        traceback.print_exc()

    database.execute(
        "UPDATE trips SET headsign = ? WHERE journey_id = ?",
        (
            headsign,
            zugid(status),
        ),
    )
    return headsign


class LineEmoji:  # pylint: disable=too-few-public-methods
    "namespace for our line-painting emoji stolen from wikipedia"
    START = "<:A1:1146748019245588561>"
    END = "<:A2:1146748021586010182>"
    CHANGE_SAME_STOP = "<:B0:1152624963677868185>"
    CHANGE_LEAVE_STOP = "<:B2:1146748013490999379>"
    CHANGE_WALK = "<:B3:1152615187375988796>"
    CHANGE_ENTER_STOP = "<:B1:1146748016422821948>"
    RAIL = "<:C1:1146748024358441001>"
    COMPACT_JOURNEY_START = "<:A3:1152995610216104077>"
    COMPACT_JOURNEY = "<:A4:1152930006478106724>"
    SPACER = " "


train_type_emoji = {
    "ATS": "<:SBahn:1152254307660484650>",
    "Bus": "<:Bus:1160288158374707241>",
    "CJX": "<:cjx:1160298616812994570>",
    "EC": "<:EC:1160298680365105232>",
    "Fähre": "<:Faehre:1143105659827658783>",
    "IC": "<:IC:1160298681887625287>",
    "ICE": "<:ICE:1160298682793611407>",
    "IR": "<:IR:1160298677156446229>",
    "IRE": "<:IRE:1160280941118374102>",
    "O-Bus": "<:Bus:1160288158374707241>",
    "R": "<:R_:1160502334539956274>",
    "RB": "<:RB:1160502337404674048>",
    "RE": "<:RE:1160280940057215087>",
    "REX": "<:REX:1160298595833102358>",
    "RJ": "<:rj:1160298686316818542>",
    "RJX": "<:rjx:1160298687491227740>",
    "RS": "<:RS:1160502338499391570>",
    "S": "<:SBahn:1102206882527060038>",
    "SB": "<:SB:1160502333143261234>",
    "Schw-B": "<:Schwebebahn:1143108575770726510>",
    "STB": "<:U_:1160288163214921889>",
    "STR": "<:Tram:1160290093400064060>",
    "TER": "<:TER:1152248180407275591>",
    "Tram": "<:Tram:1160290093400064060>",
    "U": "<:U_:1160288163214921889>",
    "U1": "<:U_:1160288163214921889>",
    "U2": "<:U_:1160288163214921889>",
    "U3": "<:U_:1160288163214921889>",
    "U4": "<:U_:1160288163214921889>",
    "U5": "<:U_:1160288163214921889>",
    "U6": "<:U_:1160288163214921889>",
    "Ü": "<:UE:1160288194730930196>",
    "WB": "***west***",
}
train_type_color = {
    k: discord.Colour.from_str(v)
    for (k, v) in {
        "ATS": "#0096d8",
        "Bus": "#a3167e",
        "EC": "#ff0404",
        "EN": "#282559",
        "CJX": "#e73f0f",
        "Fähre": "#00a4db",
        "IC": "#ff0404",
        "ICE": "#ff0404",
        "IR": "#ff0404",
        "IRE": "#e73f0f",
        "NJ": "#282559",
        "O-Bus": "#a3167e",
        "R": "#1d4491",
        "RB": "#1d4491",
        "RE": "#e73f0f",
        "REX": "#e73f0f",
        "RJ": "#c63131",
        "RJX": "#c63131",
        "RS": "#008d4f",
        "S": "#008d4f",
        "SB": "#2e2e7d",
        "Schw-B": "#4896d2",
        "STB": "#014e8d",
        "STR": "#c5161c",
        "TER": "#1c4aa2",
        "Tram": "#c5161c",
        "U": "#014e8d",
        "U1": "#ff2e17",
        "U2": "#9864b2",
        "U3": "#ff7d24",
        "U4": "#19a669",
        "U5": "#2e8e95",
        "U6": "#9a6736",
        "Ü": "#78b41d",
        "WB": "#2e86ce",
    }.items()
}

replace_headsign = {
    # Vienna
    ("U1", "Wien Alaudagasse (U1)"): "Alaudagasse",
    ("U1", "Wien Leopoldau Bahnhst (U1)"): "Leopoldau",
    ("U1", "Wien Oberlaa (U1)"): "Oberlaa",
    ("U2", "Wien Schottentor (U2)"): "Schottentor",
    ("U2", "Wien Seestadt (U2)"): "Seestadt",
    ("U3", "Wien Ottakring Bahnhst (U3)"): "Ottakring",
    ("U3", "Wien Simmering Bahnhof (U3)"): "Simmering",
    ("U4", "Wien Heiligenstadt Bf (U4)"): "Heiligenstadt",
    ("U4", "Wien Hütteldorf Bf (U4)"): "Hütteldorf",
    ("U6", "Wien Floridsdorf Bf (U6)"): "Floridsdorf",
    ("U6", "Wien Siebenhirten (U6)"): "Siebenhirten",
    # Hannover
    ("STR1", "Laatzen (GVH)"): "Laatzen",
    ("STR1", "Langenhagen (Hannover) (GVH)"): "Langenhagen",
    ("STR1", "Sarstedt (Endpunkt GVH)"): "Sarstedt",
    ("STR2", "Rethen(Leine) Nord, Laatzen"): "Rethen/Nord",
    ("STR3", "Altwarmbüchen, Isernhagen"): "Altwarmbüchen",
    ("STR9", "Empelde (Bus/Tram), Ronnenberg"): "Empelde",
    # jesus christ KVV please fix this nonsense
    ("S2", "Blankenloch Nord, Stutensee"): "Blankenloch",
    ("S2", "Daxlanden Dornröschenweg, Karlsruhe"): "Rheinstrandsiedlung",
    ("S2", "Hagsfeld Reitschulschlag (Schleife), Karlsruhe"): "Reitschulschlag",
    ("S2", "Mörsch Bach-West, Rheinstetten"): "Rheinstetten",
    ("S2", "Spöck Richard-Hecht-Schule, Stutensee"): "Spöck",
}
