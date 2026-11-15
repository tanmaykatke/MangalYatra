"""Mangal Yatra helmet backend.

Serves the HUD web app, streams live state over a WebSocket, runs the route simulator,
checks safety alerts and handles commands from voice, buttons and the keyboard.

Run:  python backend/server.py --mission data/jezero
Then open http://localhost:8080 (laptop) or point Chromium kiosk at it (Pi).
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from aiohttp import web, ClientSession, WSMsgType

from geo import distance_m, distance_to_polygon_m, compass_label, bearing_deg
from earth_link import EarthLink
from eva_log import EvaLog, LOG_DIR
from mission_control import MissionControl
from telemetry_store import TelemetryStore
from sim import RouteSimulator
from sensors import Sensors, start_buttons
import voice

ROOT = Path(__file__).resolve().parent.parent
TICK_HZ = 5
SERVER_ID = str(time.time())   # changes on every restart, so open HUDs know to reload
COMPASS_WORDS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
                 "S": "south", "SW": "southwest", "W": "west", "NW": "northwest"}
ALERT_REPEAT_S = 120          # how often a still-active critical alert is spoken again
ALERT_SPOKEN = {"turnback": "Turn back now.", "sos": "SOS active.", "spe": "Radiation storm. Take shelter."}


class Mission:
    def __init__(self, folder: Path):
        self.folder = folder
        self.meta = json.loads((folder / "mission.json").read_text())
        self.route = json.loads((folder / "route.geojson").read_text())["features"][0]["geometry"]["coordinates"]
        self.waypoints = [
            {"id": f["properties"]["id"], "name": f["properties"]["name"], "kind": f["properties"].get("kind", "nav"),
             "task": f["properties"].get("task", ""), "coord": f["geometry"]["coordinates"]}
            for f in json.loads((folder / "waypoints.geojson").read_text())["features"]
        ]
        self.hazards = json.loads((folder / "hazards.geojson").read_text())["features"]
        self.conditions = json.loads((folder / "conditions.json").read_text())
        self.has_tiles = {name: (folder / "tiles" / name).is_dir() for name in ("imagery", "slope")}


class Helmet:
    def __init__(self, mission: Mission, time_scale: float, arduino_port=None):
        self.m = mission
        self.sim = RouteSimulator(mission.route, mission.waypoints, time_scale=time_scale)
        self.loop = None
        self.arduino = None
        if arduino_port:
            from arduino_link import ArduinoLink
            self.arduino = ArduinoLink(arduino_port, on_button=self.on_button)
            self.sensors = self.arduino          # same heading()/suit()/recenter() interface
        else:
            self.sensors = Sensors()
        self.clients = set()
        self.manual_heading_offset = 0.0        # arrow keys on the laptop stand in for head turning
        self.sim_elapsed_min = 0.0
        self.eva_min = mission.meta.get("eva_duration_min", 420)
        self.sos = False
        self.last_alert_said = {}
        self.ptt = voice.PushToTalk()
        self.http = None
        self.earth = EarthLink(on_delivered=self.earth_delivered, on_status=self.earth_status)
        self.alerts_sent_to_earth = set()
        self.crew = (mission.meta.get("crew") or ["EV1"])[0]
        self.log = EvaLog(mission.meta, mission.waypoints, self.sim.total)       # habitat black box
        self.store = TelemetryStore(delay_s=self.earth.delay_s)                  # TigerData, on Earth
        self.log.on_event = self.store.event
        self.log.event(0, "start", "EVA started at the habitat")
        self.mission_control = MissionControl()                                   # Gemini, on Earth
        self.latest_report = None
        self.report_task = None

    # ---------- state ----------
    def heading(self, route_bearing):
        h = self.sensors.heading()
        if h is None:
            h = route_bearing + self.manual_heading_offset
        return h % 360

    def consumables(self):
        o2 = max(0.0, self.eva_min - self.sim_elapsed_min)
        battery = max(0.0, 100 * (1 - self.sim_elapsed_min / (self.eva_min * 1.15)))
        return round(o2), round(battery)

    def alerts(self, pos):
        out = []
        for hz in self.m.hazards:
            p = hz["properties"]
            d = distance_to_polygon_m(pos, hz["geometry"]["coordinates"])
            if d == 0:
                out.append({"level": "critical", "id": p["id"], "msg": f"Inside hazard: {p['name']}"})
            elif d < p.get("warn_m", 40):
                out.append({"level": "warn", "id": p["id"], "msg": f"{p['name']} {round(d)} m"})
        o2, battery = self.consumables()
        # Turn-back rule: walking time back to the start + reserve must be less than O2 left.
        back_min = self.sim.s / self.sim.speed / 60
        reserve = self.m.meta.get("o2_reserve_min", 30)
        if o2 < back_min + reserve:
            out.append({"level": "critical", "id": "turnback", "msg": f"Turn back: O2 {o2} min, walk back {round(back_min)} min"})
        elif o2 < back_min + reserve + 30:
            out.append({"level": "warn", "id": "o2", "msg": f"O2 margin low ({o2} min)"})
        if battery < 20:
            out.append({"level": "warn", "id": "battery", "msg": f"Suit battery {battery}%"})
        c = self.m.conditions
        if c["dust_opacity_tau"] >= c.get("dust_warn_tau", 1.0):
            out.append({"level": "warn", "id": "dust", "msg": f"High dust (tau {c['dust_opacity_tau']})"})
        if c.get("solar_particle_event"):
            out.append({"level": "critical", "id": "spe", "msg": "Solar particle event: seek shelter"})
        if self.sos:
            out.append({"level": "critical", "id": "sos", "msg": "SOS sent to habitat"})
        return out

    def state(self):
        pos, route_bearing = self.sim.position()
        heading = self.heading(route_bearing)
        wp = self.sim.next_waypoint()
        o2, battery = self.consumables()
        nxt = None
        if wp:
            nxt = {"id": wp["id"], "name": wp["name"], "kind": wp["kind"], "task": wp["task"],
                   "dist_m": round(wp["along"] - self.sim.s), "coord": wp["coord"]}
        return {
            "type": "state",
            "pos": pos,
            "heading": round(heading, 1),
            "heading_label": compass_label(heading),
            "route_bearing": round(route_bearing, 1),
            "walked_m": round(self.sim.s),
            "remaining_m": round(self.sim.remaining_m()),
            "paused": self.sim.paused,
            "next_wp": nxt,
            "suit": {**self.sensors.suit(), "o2_min": o2, "battery_pct": battery},
            "conditions": self.m.conditions,
            "alerts": self.alerts(pos),
            "mission_time_min": round(self.sim_elapsed_min),
        }

    # ---------- spoken reports (keep them short: one or two brief sentences) ----------
    HAZARD_ADVICE = {"rocks": "Stay on route.", "slope": "Don't climb it.", "soft_ground": "Step carefully."}

    def turn_back_report(self, st):
        o2 = st["suit"]["o2_min"]
        back = self.sim.s / self.sim.speed / 60
        spare = round(o2 - back - self.m.meta.get("o2_reserve_min", 30))
        if spare <= 0:
            return "Turn back now."
        return f"{spare} minutes until we turn back."

    def danger_report(self, st, look_ahead_m=300, step_m=10):
        """Only the single most urgent hazard, in a few words."""
        pos, nearest = st["pos"], None
        for hz in self.m.hazards:
            p = hz["properties"]
            advice = self.HAZARD_ADVICE.get(p.get("kind"), "")
            d = distance_to_polygon_m(pos, hz["geometry"]["coordinates"])
            if d == 0:
                return f"We're inside {p['name']}. {advice}".strip()
            if d < p.get("warn_m", 40):
                return f"{p['name']}, {round(d)} meters. {advice}".strip()
            s = self.sim.s + step_m      # first point ahead on the route that comes within warning range
            while s <= min(self.sim.total, self.sim.s + look_ahead_m):
                if distance_to_polygon_m(self.sim.point_at(s)[0], hz["geometry"]["coordinates"]) < p.get("warn_m", 40):
                    ahead = s - self.sim.s
                    if nearest is None or ahead < nearest[0]:
                        nearest = (ahead, f"{p['name']} in {round(ahead)} meters. {advice}".strip())
                    break
                s += step_m
        if nearest:
            return nearest[1]
        # Nothing on the route: still say where the closest hazard is, so the astronaut knows what to avoid.
        closest = min(((distance_to_polygon_m(pos, hz["geometry"]["coordinates"]), hz) for hz in self.m.hazards),
                      key=lambda t: t[0], default=None)
        if not closest or closest[0] > 500:
            return "All clear ahead."
        d, hz = closest
        ring = hz["geometry"]["coordinates"][0]
        centre = [sum(p[0] for p in ring[:-1]) / (len(ring) - 1), sum(p[1] for p in ring[:-1]) / (len(ring) - 1)]
        side = COMPASS_WORDS[compass_label(bearing_deg(pos, centre))]
        return f"All clear ahead. Nearest: {hz['properties']['name'].lower()}, {round(d, -1):.0f} meters {side}."

    # ---------- messaging ----------
    async def broadcast(self, msg):
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                self.clients.discard(ws)

    async def say(self, text):
        print(f"[say] {text}")
        if voice.piper_available():
            voice.speak_piper(text)
            await self.broadcast({"type": "say", "text": text, "spoken": True})
        else:
            await self.broadcast({"type": "say", "text": text, "spoken": False})

    async def handle_command(self, cmd, source="hud"):
        a = cmd.get("action")
        st = self.state()
        print(f"[cmd] {cmd} from {source}")
        if a in ("toggle_layer", "zoom_in", "zoom_out", "mirror", "cycle_layer"):
            await self.broadcast({"type": "command", **cmd})
            if a == "toggle_layer":
                await self.say(f"{cmd['layer'].capitalize()}.")
        elif a == "restart":
            self.sim.reset()
            self.sim_elapsed_min = 0.0          # fresh oxygen and battery
            self.sos = False
            self.last_alert_said.clear()
            if self.report_task and not self.report_task.done():   # a report for the old EVA is no longer wanted
                self.report_task.cancel()
                await self.broadcast({"type": "report_status", "stage": "cancelled"})
            self.log.start()
            self.log.event(0, "start", "EVA started at the habitat")
            await self.say("Restarting from the habitat.")
        elif a == "report":
            await self.request_report()
        elif a == "next_waypoint":
            wp = self.sim.skip_to_next()
            await self.say(f"Skipping to {wp['name']}." if wp else "No more stops.")
        elif a == "where_next":
            n = st["next_wp"]
            if n:
                task = f" {n['task'].split('.')[0]}." if n.get("task") else ""   # first sentence of the task only
                await self.say(f"{n['name']}, {n['dist_m']} meters.{task}")
            else:
                await self.say("Route complete.")
        elif a == "turn_back":
            await self.say(self.turn_back_report(st))
        elif a == "danger":
            await self.say(self.danger_report(st))
        elif a == "mission":
            m = self.m.meta
            await self.say(m.get("goal_short") or m.get("science_goal", "No mission goal set."))
        elif a == "where_am_i":
            passed = [w for w in self.sim.waypoints if w["along"] <= self.sim.s + 0.5]
            n = st["next_wp"]
            if passed and self.sim.s - passed[-1]["along"] < 20:
                where = f"At {passed[-1]['name']}."
            else:
                where = f"{st['walked_m']} meters in."
            await self.say(f"{where} {n['name']} in {n['dist_m']} meters." if n else f"{where} Route complete.")
        elif a == "home":
            back_m = round(self.sim.s)
            if back_m < 20:
                await self.say("We're at the habitat.")
            else:
                await self.say(f"Habitat {back_m} meters back, {max(1, round(self.sim.s / self.sim.speed / 60))} minutes.")
        elif a == "status":
            s, what = st["suit"], cmd.get("what")
            await self.say({"o2": f"Oxygen {s['o2_min']} minutes.",
                            "battery": f"Battery {s['battery_pct']} percent."}.get(
                what, f"Oxygen {s['o2_min']} minutes, battery {s['battery_pct']} percent."))
        elif a == "conditions":
            c, what = self.m.conditions, cmd.get("what")
            await self.say({"radiation": f"Radiation {c['radiation_usv_per_day']} micro sieverts a day.",
                            "air": f"Air {c['air_temp_c']} degrees."}.get(
                what, f"Dust {c['dust_opacity_tau']}, air {c['air_temp_c']} degrees."))
        elif a == "pause":
            self.sim.paused = True
            await self.say("Paused.")
        elif a == "resume":
            self.sim.paused = False
            await self.say("Resuming.")
        elif a == "sos":
            self.sos = not self.sos
            self.log.event(self.sim_elapsed_min, "sos", ("SOS raised, " if self.sos else "SOS cancelled, ") + self.where_words())
            if self.sos:
                p = st["pos"]
                await self.earth.send(f"SOS from {self.crew}, {self.where_words()}. Position {p[1]:.4f} N {p[0]:.4f} E. "
                                      f"O2 {st['suit']['o2_min']} min. Reply to this number to talk to the crew.",
                                      call=self.earth.call_on_sos)
                await self.say(f"SOS sent. Earth receives it in {self.delay_words()}.")
            else:
                await self.earth.send(f"SOS cancelled by {self.crew}. Crew is safe.")
                await self.say("SOS cancelled.")
        elif a == "earth_msg":
            text = (cmd.get("text") or "").strip()
            if text:
                await self.earth.send(f"{self.crew}: {text}")
                self.log.event(self.sim_elapsed_min, "message", f"Crew to Earth: {text}")
                await self.say(f"Sending to Earth. Arrives in {self.delay_words()}.")
            else:
                await self.say("What should I tell Mission Control?")
        elif a == "earth_status":
            s = self.earth.status()
            parts = [f"Earth delay {self.delay_words()}."]
            if s["outbox"]:
                parts.append(f"{len(s['outbox'])} message on its way to Earth.")
            if s["inbox_eta_s"]:
                parts.append(f"Message from Earth arriving in {self.delay_words(min(s['inbox_eta_s']))}.")
            if len(parts) == 1:
                parts.append("No messages waiting.")
            await self.say(" ".join(parts))
        elif a == "recenter":
            self.manual_heading_offset = 0
            self.sensors.recenter(st["route_bearing"])
            await self.say("Recentered.")
        elif a == "turn":
            self.manual_heading_offset = (self.manual_heading_offset + cmd.get("delta", 0)) % 360
        await self.broadcast(self.state())

    # ---------- Earth link (Twilio) ----------
    def delay_words(self, seconds=None):
        s = self.earth.delay_s() if seconds is None else seconds
        n, unit = (round(s), "second") if s < 90 else (round(s / 60), "minute")
        return f"{n} {unit}{'' if n == 1 else 's'}"

    def where_words(self):
        passed = [w for w in self.sim.waypoints if w["along"] <= self.sim.s + 0.5]
        n = self.sim.next_waypoint()
        if passed and self.sim.s - passed[-1]["along"] < 20:
            return f"at {passed[-1]['name']}"
        return f"{round(self.sim.s)} m along the route" + (f", heading to {n['name']}" if n else "")

    async def earth_delivered(self, text, sent_at, travelled_s):
        """An SMS from Mission Control has crossed the gap to Mars."""
        await self.broadcast({"type": "earth_msg", "text": text, "travelled_s": round(travelled_s)})
        await self.say(f"Message from Earth: {text}")

    async def earth_status(self, status):
        await self.broadcast({"type": "earth", **status})

    # ---------- Mission report (Gemini at Mission Control on Earth) ----------
    async def request_report(self):
        """The EVA log travels to Earth, Gemini writes the report, the report travels back to Mars."""
        if self.report_task and not self.report_task.done():
            await self.say("A mission report is already on its way.")
            return
        delay = self.earth.delay_s()
        log = self.log.summary()          # snapshot: this is the log that leaves Mars now
        await self.say(f"Sending the EVA log to Earth. Report back in about {self.delay_words(2 * delay + 5)}.")
        self.report_task = asyncio.create_task(self._report_flow(delay, log))

    async def _report_flow(self, delay, log):
        try:
            await self.broadcast({"type": "report_status", "stage": "to_earth", "eta_s": round(delay)})
            await asyncio.sleep(delay)                                      # log crossing to Earth
            await self.broadcast({"type": "report_status", "stage": "writing"})
            text, by = await self.mission_control.report(self.http, log)
            self.latest_report = {"eva_id": log["eva_id"], "text": text, "by": by, "created": time.time()}
            LOG_DIR.mkdir(exist_ok=True)
            (LOG_DIR / f"{log['eva_id']}_report.md").write_text(f"# {log['eva_id']} mission report\n_{by}_\n\n{text}\n",
                                                                encoding="utf-8")
            await self.broadcast({"type": "report_status", "stage": "to_mars", "eta_s": round(delay)})
            await asyncio.sleep(delay)                                      # report crossing back to Mars
            await self.broadcast({"type": "report", **self.latest_report})
            self.log.event(self.sim_elapsed_min, "report", f"Mission report received from Earth ({by})")
            await self.say("Mission report from Earth received.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[mission-control] report failed: {e}")
            await self.broadcast({"type": "report_status", "stage": "failed"})
            await self.say("The mission report could not be generated.")

    async def handle_text(self, text):
        await self.broadcast({"type": "heard", "text": text})
        cmd, how = await voice.understand(text, self.http)
        if cmd:
            await self.broadcast({"type": "understood", "command": cmd, "via": how})
            await self.handle_command(cmd, source=f"voice/{how}")
        else:
            await self.say("Sorry, I did not understand.")

    # ---------- buttons (called from GPIO thread) ----------
    def on_button(self, name, pressed):
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self._button(name, pressed), self.loop)

    async def _button(self, name, pressed):
        if name == "ptt":
            if pressed:
                self.ptt.start()
                await self.broadcast({"type": "listening", "on": True})
            else:
                await self.broadcast({"type": "listening", "on": False})
                text = await self.loop.run_in_executor(None, self.ptt.stop_and_transcribe)
                if text:
                    await self.handle_text(text)
        elif pressed:
            await self.handle_command({"action": name}, source="button")

    # ---------- main loop ----------
    async def run(self):
        last = time.monotonic()
        last_cpu_sent = 0.0
        while True:
            await asyncio.sleep(1 / TICK_HZ)
            now = time.monotonic()
            dt, last = now - last, now
            if self.sim.s < self.sim.total:          # EVA clock stops once the planned route is complete,
                self.sim_elapsed_min += dt * self.sim.time_scale / 60   # so O2 doesn't drain to 0 afterwards
            reached = self.sim.step(dt)
            if reached:
                await self.say(f"Arrived at {reached['name']}.")
                o2_now = self.consumables()[0]
                self.log.event(self.sim_elapsed_min, "arrival", f"Arrived at {reached['name']}", stop=reached["name"],
                               walked_m=round(self.sim.s), o2_min=o2_now)
                if reached.get("kind") == "science":
                    await self.earth.send(f"{self.crew} arrived at {reached['name']}. {reached.get('task', '')}".strip())
                if self.sim.next_waypoint() is None and not self.log.complete:   # last stop: EVA done, ask Earth for the report
                    self.log.complete = True
                    self.log.event(self.sim_elapsed_min, "complete", "All planned stops reached", walked_m=round(self.sim.s))
                    await self.request_report()
            if self.arduino and now - last_cpu_sent > 1:
                last_cpu_sent = now
                self.arduino.send_cpu_temp()       # Arduino sets the helmet fan speed from this
            st = self.state()
            # Speak a critical alert once when it starts, then remind only every 2 minutes while it lasts.
            # The alert stays on screen the whole time, so repeating it more often is just noise.
            active = set()
            for al in st["alerts"]:
                if al["level"] != "critical":
                    continue
                active.add(al["id"])
                if time.time() - self.last_alert_said.get(al["id"], 0) > ALERT_REPEAT_S:
                    self.last_alert_said[al["id"]] = time.time()
                    if self.arduino:
                        self.arduino.buzz(3)
                    await self.say(ALERT_SPOKEN.get(al["id"], al["msg"]))
                if al["id"] not in self.alerts_sent_to_earth and al["id"] != "sos":   # tell Earth once per alert
                    self.alerts_sent_to_earth.add(al["id"])
                    await self.earth.send(f"ALERT from {self.crew}: {al['msg']}. {self.where_words()}.")
                    self.log.event(self.sim_elapsed_min, "alert", al["msg"], where=self.where_words(),
                                   o2_min=st["suit"]["o2_min"])
            for gone in set(self.last_alert_said) - active:   # cleared alerts are announced afresh next time
                del self.last_alert_said[gone]
            self.alerts_sent_to_earth &= active
            if not self.sim.paused:
                self.log.sample(self.sim_elapsed_min, st)
            if not self.log.complete:
                self.store.record(self.log.id, self.sim_elapsed_min, st)      # TigerData telemetry (every 2 s)
            await self.broadcast(st)


async def ws_handler(request):
    helmet: Helmet = request.app["helmet"]
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    helmet.clients.add(ws)
    m = helmet.m
    await ws.send_str(json.dumps({
        "type": "config",
        "mission": m.meta,
        "tiles": m.has_tiles,
        "waypoints": m.waypoints,
        "piper": bool(voice.piper_available()),
        "stt": bool(helmet.ptt.ok),
        "server_id": SERVER_ID,
        "earth": helmet.earth.status(),
        "report": helmet.latest_report,
    }))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "command":
                await helmet.handle_command({k: v for k, v in data.items() if k != "type"})
            elif data.get("type") == "ptt":              # Talk button held on the touch screen
                await helmet._button("ptt", bool(data.get("down")))
            elif data.get("type") == "earth_test":          # key E: pretend Mission Control replied (no Twilio needed)
                await helmet.earth.receive(data.get("text") or "Copy your last message. Continue to Wildcat Ridge.")
            elif data.get("type") == "voice_text":
                await helmet.handle_text(data.get("text", ""))
    finally:
        helmet.clients.discard(ws)
    return ws


async def index(request):
    return web.FileResponse(ROOT / "hud" / "index.html")


# ---------- Mission Control (Earth side): EVA log, Gemini report and Q&A ----------
async def mission_control_page(request):
    return web.FileResponse(ROOT / "hud" / "mission-control.html")


async def api_eva(request):
    h: Helmet = request.app["helmet"]
    mc = h.mission_control
    return web.json_response({"log": h.log.summary(), "state": h.state(), "report": h.latest_report,
                              "gemini": {"live": mc.live, "model": mc.model}, "earth_delay_s": round(h.earth.delay_s()),
                              "tigerdata": h.store.info()})


async def api_telemetry(request):
    """Charts data from TigerData: time_bucket() over the current (or a given) EVA."""
    h: Helmet = request.app["helmet"]
    if not h.store.info()["connected"]:
        return web.json_response({"error": f"TigerData {h.store.status}", "series": []})
    eva_id = request.query.get("eva_id") or h.log.id
    bucket = max(2, min(3600, int(request.query.get("bucket", "10"))))
    try:
        return web.json_response({"eva_id": eva_id, "bucket_s": bucket, "series": await h.store.series(eva_id, bucket),
                                  "evas": await h.store.evas()})
    except Exception as e:
        return web.json_response({"error": str(e)[:200], "series": []}, status=502)


async def api_report(request):
    h: Helmet = request.app["helmet"]
    log = h.log.summary()
    text, by = await h.mission_control.report(h.http, log)
    h.latest_report = {"eva_id": log["eva_id"], "text": text, "by": by, "created": time.time()}
    return web.json_response(h.latest_report)


async def api_ask(request):
    h: Helmet = request.app["helmet"]
    question = (await request.json()).get("question", "").strip()
    if not question:
        return web.json_response({"error": "empty question"}, status=400)
    try:
        answer, by = await h.mission_control.ask(h.http, question, h.log.summary())
    except Exception as e:
        return web.json_response({"error": str(e)[:300]}, status=502)
    return web.json_response({"answer": answer, "by": by})


@web.middleware
async def no_cache(request, handler):
    """Never let the browser cache the HUD, so a normal refresh always gets the latest code."""
    resp = await handler(request)
    if not request.path.startswith("/mission/tiles/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _snapshot(folder: Path):
    return {p: p.stat().st_mtime for p in folder.rglob("*") if p.is_file() and "vendor" not in p.parts}


async def watch_hud(helmet: "Helmet"):
    """Dev mode: when a file in hud/ changes, tell every open HUD to reload itself."""
    folder = ROOT / "hud"
    before = _snapshot(folder)
    while True:
        await asyncio.sleep(0.5)
        now = _snapshot(folder)
        if now != before:
            changed = sorted(str(p.relative_to(ROOT)) for p in now.keys() | before.keys() if now.get(p) != before.get(p))
            print(f"[dev] changed: {', '.join(changed)} -> reloading HUD")
            before = now
            await helmet.broadcast({"type": "reload"})


async def on_startup(app):
    helmet: Helmet = app["helmet"]
    helmet.loop = asyncio.get_running_loop()
    helmet.http = ClientSession()
    helmet.buttons = [] if helmet.arduino else start_buttons(helmet.on_button)
    app["loop_task"] = asyncio.create_task(helmet.run())
    app["earth_task"] = asyncio.create_task(helmet.earth.run(helmet.http))
    app["store_task"] = asyncio.create_task(helmet.store.run())
    app["watch_task"] = asyncio.create_task(watch_hud(helmet)) if app["dev"] else None


async def on_cleanup(app):
    app["loop_task"].cancel()
    app["earth_task"].cancel()
    app["store_task"].cancel()
    if helmet_conn := app["helmet"].store.conn:
        await helmet_conn.close()
    if app["watch_task"]:
        app["watch_task"].cancel()
    await app["helmet"].http.close()


def main():
    ap = argparse.ArgumentParser()
    # Prefer the real pipeline output (HiRISE + THEMIS) when it has been built; else the sample package.
    real = ROOT / "data" / "jezero_real"
    ap.add_argument("--mission", default=str(real if (real / "mission.json").exists() else ROOT / "data" / "jezero"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--arduino", metavar="PORT", help="use the Arduino hub, e.g. /dev/ttyACM0 (Pi) or COM3 (Windows)")
    ap.add_argument("--time-scale", type=float, default=10.0, help="demo speed-up (10 = walk 10x faster)")
    ap.add_argument("--dev", action="store_true", help="reload open HUDs when files in hud/ change")
    args = ap.parse_args()

    mission = Mission(Path(args.mission))
    app = web.Application(middlewares=[no_cache])
    app["dev"] = args.dev
    app["helmet"] = Helmet(mission, args.time_scale, args.arduino)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/mission-control", mission_control_page)
    app.router.add_get("/api/eva", api_eva)
    app.router.add_post("/api/report", api_report)
    app.router.add_post("/api/ask", api_ask)
    app.router.add_get("/api/telemetry", api_telemetry)
    app.router.add_static("/hud/", ROOT / "hud")
    app.router.add_static("/mission/", mission.folder)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"Mangal Yatra HUD: http://localhost:{args.port}  (mission: {mission.meta['name']})")
    if sys.platform == "win32":     # the async Postgres driver (TigerData) needs the selector event loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    web.run_app(app, port=args.port, print=None)


if __name__ == "__main__":
    main()
