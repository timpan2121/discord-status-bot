#!/usr/bin/env python3
"""Discord-bot för driftstatus. Online när VM:en kör, offline automatiskt när den är nere.

Env: DISCORD_TOKEN, DISCORD_CHANNEL_ID
Kommandon: /status /tjänst /störningar /dashboard /uppdatera /hjälp
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import tomllib
from pathlib import Path

import discord
from discord import app_commands

import statusbot as sb

log = logging.getLogger("statusbot")
HERE = Path(__file__).parent
conf = tomllib.loads((HERE / "config.toml").read_text())
state_path = HERE / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {}

TOKEN = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or conf.get("discord", {}).get("channel_id") or 0)

SEV_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡", "maintenance": "🔧", "info": "🟡"}
SEV_RANK = {"critical": 4, "major": 3, "minor": 2, "info": 2, "maintenance": 1}
CAT_ORDER = ["Microsoft", "Google", "Moln & infrastruktur", "Appar & tjänster", "Banker & betalningar", "Bredband (Sverige)"]

intents = discord.Intents.none()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
_last_presence = None
_poll_now = asyncio.Event()


# ---------------------------------------------------------------- data

def sources() -> dict[str, dict]:
    return {k: v for k, v in conf.get("source", {}).items() if v.get("enabled", True)}


def page_url(sc: dict) -> str:
    return sc.get("page") or sc.get("url", "")


def svc_info(key: str) -> dict:
    sc = sources()[key]
    st = state.get(key, {})
    opn = [v for v in st.get("open", {}).values() if isinstance(v, list)]
    if st.get("fails", 0) >= 3 and not opn:
        icon, label, rank = "⚪", "Kunde inte kontrolleras", 0
    elif opn:
        worst = max(opn, key=lambda v: SEV_RANK.get(v[2], 2))
        icon, rank = SEV_ICON.get(worst[2], "🟡"), SEV_RANK.get(worst[2], 2)
        label = "Underhåll" if worst[2] == "maintenance" else f"{len(opn)} störning{'ar' if len(opn) > 1 else ''}"
    elif sc["type"] == "link":
        icon, label, rank = "🔗", "Kolla driftkartan för din adress", 0
    elif sc["type"] == "webpage" and not sc.get("keywords"):
        icon, label, rank = "🔹", "Bevakar driftsidan", 0
    elif "checked" not in st:
        icon, label, rank = "⏳", "Väntar på första kontroll", 0
    else:
        icon, label, rank = "🟢", "Fungerar", 0
    return {"key": key, "name": sc.get("name", key), "cat": sc.get("category", "Övrigt"),
            "url": page_url(sc), "icon": icon, "label": label, "rank": rank, "open": opn,
            "checked": st.get("checked")}


def all_info() -> list[dict]:
    return [svc_info(k) for k in sources()]


def link(name: str, url: str) -> str:
    return f"[{name}]({url})" if url.startswith("http") else name


# ---------------------------------------------------------------- embeds

def overview_embed(live: bool = False) -> discord.Embed:
    infos = all_info()
    worst = max((i["rank"] for i in infos), default=0)
    n_bad = sum(1 for i in infos if i["open"])
    color = {0: 0x2ECC71, 1: 0x3498DB, 2: 0xF1C40F, 3: 0xE67E22, 4: 0xE74C3C}[worst]
    head = "✅ Alla tjänster fungerar" if n_bad == 0 else f"⚠️ Störningar hos {n_bad} av {len(infos)} tjänster"
    e = discord.Embed(title="📡 Driftstatus", description=f"**{head}**", color=color)
    cats = sorted({i["cat"] for i in infos}, key=lambda c: CAT_ORDER.index(c) if c in CAT_ORDER else 99)
    for c in cats:
        lines = [f"{i['icon']} {link(i['name'], i['url'])}" + ("" if i["icon"] == "🟢" else f" – *{i['label']}*")
                 for i in sorted((i for i in infos if i["cat"] == c), key=lambda i: (-i["rank"], i["name"]))]
        chunk, first = "", True
        for ln in lines:  # Discord-fält max 1024 tecken: dela upp långa kategorier
            if len(chunk) + len(ln) + 1 > 1024:
                e.add_field(name=c if first else "\u200b", value=chunk, inline=False)
                chunk, first = "", False
            chunk = f"{chunk}\n{ln}" if chunk else ln
        if chunk:
            e.add_field(name=c if first else "\u200b", value=chunk, inline=False)
    checked = max((i["checked"] or 0 for i in infos), default=0)
    e.add_field(name="​", value=(f"Senast kontrollerat <t:{int(checked)}:R>" if checked else "Kontrollerar…")
                + ("  •  uppdateras automatiskt" if live else ""), inline=False)
    e.set_footer(text="🟢 OK  🟡 mindre  🟠 större  🔴 kritisk  🔧 underhåll  🔗 driftkarta  ⚪ okänt")
    return e


def service_embed(key: str) -> discord.Embed:
    i = svc_info(key)
    color = {0: 0x2ECC71, 1: 0x3498DB, 2: 0xF1C40F, 3: 0xE67E22, 4: 0xE74C3C}[i["rank"]]
    e = discord.Embed(title=f"{i['icon']} {i['name']}", url=i["url"] or None, color=color,
                      description=f"**{i['label']}**")
    for title, url, sev, status in i["open"][:10]:
        e.add_field(name=f"{SEV_ICON.get(sev, '🟡')} {status}"[:256], value=link(title[:200], url)[:1024], inline=False)
    if i["checked"]:
        e.add_field(name="​", value=f"Senast kontrollerat <t:{int(i['checked'])}:R>", inline=False)
    e.set_footer(text=i["cat"])
    return e


def incidents_embed() -> discord.Embed:
    infos = [i for i in all_info() if i["open"]]
    if not infos:
        return discord.Embed(title="✅ Inga pågående störningar", color=0x2ECC71)
    e = discord.Embed(title=f"⚠️ Pågående störningar ({sum(len(i['open']) for i in infos)})", color=0xE67E22)
    for i in sorted(infos, key=lambda i: -i["rank"])[:25]:
        val = "\n".join(f"{SEV_ICON.get(s, '🟡')} {link(t[:150], u)}" for t, u, s, _ in i["open"][:5])
        e.add_field(name=i["name"], value=val[:1024], inline=False)
    return e


# ---------------------------------------------------------------- UI

class ServicePicker(discord.ui.View):
    """Persistent meny + knappar under översikten (fungerar även efter omstart)."""

    def __init__(self):
        super().__init__(timeout=None)
        infos = sorted(all_info(), key=lambda i: (-i["rank"], i["name"]))[:25]
        sel = discord.ui.Select(custom_id="statusbot:pick", placeholder="🔎 Välj en tjänst för detaljer…",
                                options=[discord.SelectOption(label=i["name"][:100], value=i["key"],
                                                              emoji=i["icon"], description=i["label"][:100])
                                         for i in infos] or [discord.SelectOption(label="-", value="-")])
        sel.callback = self.on_pick
        self.add_item(sel)

    async def on_pick(self, inter: discord.Interaction):
        key = inter.data["values"][0]
        if key not in sources():
            await inter.response.send_message("Tjänsten finns inte längre.", ephemeral=True)
            return
        await inter.response.send_message(embed=service_embed(key), ephemeral=True)

    @discord.ui.button(label="Störningar", emoji="⚠️", style=discord.ButtonStyle.secondary,
                       custom_id="statusbot:incidents")
    async def btn_incidents(self, inter: discord.Interaction, _b):
        await inter.response.send_message(embed=incidents_embed(), ephemeral=True)

    @discord.ui.button(label="Uppdatera", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="statusbot:refresh")
    async def btn_refresh(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True, thinking=True)
        await force_poll()
        await inter.followup.send(embed=overview_embed(), view=ServicePicker(), ephemeral=True)


async def service_autocomplete(_inter: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=f"{i['icon']} {i['name']}"[:100], value=i["key"])
            for i in all_info() if cur in i["name"].lower()][:25]


# ---------------------------------------------------------------- kommandon

def in_channel(inter: discord.Interaction) -> bool:
    return inter.channel_id == CHANNEL_ID


async def wrong_channel(inter: discord.Interaction) -> None:
    await inter.response.send_message(f"Använd kommandona i <#{CHANNEL_ID}>.", ephemeral=True)


@tree.command(name="status", description="Översikt över alla bevakade tjänster")
@app_commands.describe(visa_för_alla="Posta synligt i kanalen istället för bara för dig")
async def cmd_status(inter: discord.Interaction, visa_för_alla: bool = False):
    if not in_channel(inter):
        return await wrong_channel(inter)
    await inter.response.send_message(embed=overview_embed(), view=ServicePicker(), ephemeral=not visa_för_alla)


@tree.command(name="tjänst", description="Detaljer för en specifik tjänst")
@app_commands.describe(namn="Vilken tjänst")
@app_commands.autocomplete(namn=service_autocomplete)
async def cmd_service(inter: discord.Interaction, namn: str):
    if not in_channel(inter):
        return await wrong_channel(inter)
    if namn not in sources():
        match = [k for k, v in sources().items() if namn.lower() in v.get("name", k).lower()]
        if not match:
            return await inter.response.send_message("Hittade ingen sådan tjänst.", ephemeral=True)
        namn = match[0]
    await inter.response.send_message(embed=service_embed(namn), ephemeral=True)


@tree.command(name="störningar", description="Lista alla pågående störningar med länkar")
async def cmd_incidents(inter: discord.Interaction):
    if not in_channel(inter):
        return await wrong_channel(inter)
    await inter.response.send_message(embed=incidents_embed(), ephemeral=True)


@tree.command(name="uppdatera", description="Kontrollera alla tjänster direkt")
async def cmd_refresh(inter: discord.Interaction):
    if not in_channel(inter):
        return await wrong_channel(inter)
    await inter.response.defer(ephemeral=True, thinking=True)
    await force_poll()
    await inter.followup.send(embed=overview_embed(), view=ServicePicker(), ephemeral=True)


@tree.command(name="dashboard", description="Skapa en live-översikt i kanalen som uppdateras automatiskt")
@app_commands.default_permissions(manage_messages=True)
async def cmd_dashboard(inter: discord.Interaction):
    if not in_channel(inter):
        return await wrong_channel(inter)
    await inter.response.send_message("Skapar dashboard…", ephemeral=True)
    msg = await inter.channel.send(embed=overview_embed(live=True), view=ServicePicker())
    old = state.get("_dashboard", {}).get("msg")
    state["_dashboard"] = {"msg": msg.id, "hash": ""}
    save_state()
    try:
        await msg.pin()
    except discord.HTTPException:
        pass  # saknar "Hantera meddelanden" - helt ok
    if old:
        try:
            await (await inter.channel.fetch_message(old)).delete()
        except discord.HTTPException:
            pass
    await inter.edit_original_response(content="✅ Dashboard skapad och fäst. Den uppdateras automatiskt.")


@tree.command(name="hjälp", description="Visa vad boten kan")
async def cmd_help(inter: discord.Interaction):
    e = discord.Embed(title="📡 Driftstatus – kommandon", color=0x3498DB, description=(
        "`/status` – översikt över alla tjänster (klickbara länkar + meny)\n"
        "`/tjänst <namn>` – detaljer för en tjänst\n"
        "`/störningar` – alla pågående störningar med länkar\n"
        "`/uppdatera` – kontrollera allt direkt\n"
        "`/dashboard` – fäst en live-översikt i kanalen\n\n"
        "Nya störningar, uppdateringar och lösningar postas automatiskt här.\n"
        "Botens status: 🟢 allt OK • 🌙 störningar pågår • ⛔ VM saknar internet • ⚫ VM avstängd"))
    await inter.response.send_message(embed=e, ephemeral=True)


# ---------------------------------------------------------------- loop

def save_state() -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)


async def force_poll() -> None:
    for k in sources():
        state.setdefault(k, {})["last"] = 0
    await poll_once()


_poll_lock = asyncio.Lock()


async def poll_once() -> None:
    async with _poll_lock:
        queued: list[tuple[dict, list[dict]]] = []
        stats = await asyncio.to_thread(
            sb.run_once, conf, state, False, lambda sc, prov, embeds: queued.append((sc, embeds)))
        save_state()
        channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
        for sc, embeds in queued:
            mention = sc.get("mention", conf.get("discord", {}).get("mention", ""))
            for e in embeds:
                await channel.send(content=mention or None, embed=discord.Embed.from_dict(e),
                                   allowed_mentions=discord.AllowedMentions(roles=True, everyone=True))
        await set_presence(stats)
        await update_dashboard(channel)


async def update_dashboard(channel) -> None:
    d = state.get("_dashboard")
    if not d or not d.get("msg"):
        return
    sig = hashlib.sha1(json.dumps([(i["icon"], i["label"]) for i in all_info()]).encode()).hexdigest()
    # Redigera när något ändrats, annars var 10:e minut (för "senast kontrollerat")
    if sig == d.get("hash") and time.time() - d.get("ts", 0) < 600:
        return
    try:
        msg = channel.get_partial_message(d["msg"])
        await msg.edit(embed=overview_embed(live=True), view=ServicePicker())
        d.update(hash=sig, ts=time.time())
        save_state()
    except discord.NotFound:
        state.pop("_dashboard", None)  # någon tog bort meddelandet
        save_state()


async def set_presence(stats: dict) -> None:
    global _last_presence
    issues = sb.open_incidents(conf, state)
    if stats["ok"] == 0 and stats["failed"] > 0:
        key = ("dnd", "⚠ Ingen internetanslutning")
    elif issues:
        key = ("idle", f"⚠ {len(issues)} pågående störningar • /status")
    else:
        key = ("online", f"{len(sources())} tjänster OK • /status")
    if key != _last_presence and (stats["ok"] or stats["failed"]):
        _last_presence = key
        await client.change_presence(status=discord.Status[key[0]], activity=discord.CustomActivity(name=key[1]))


async def poll_loop() -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await poll_once()
        except Exception as ex:  # noqa: BLE001 - logga, krascha aldrig, posta aldrig fel
            log.warning("Pollningsfel: %s", ex)
        await asyncio.sleep(30)


@client.event
async def setup_hook() -> None:  # type: ignore[misc]
    client.add_view(ServicePicker())  # gör dashboardens meny/knappar klickbara efter omstart
    await tree.sync()


@client.event
async def on_ready() -> None:
    log.info("Inloggad som %s", client.user)
    if not getattr(client, "_poll_started", False):
        client._poll_started = True
        client.loop.create_task(poll_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN or not CHANNEL_ID:
        sys.exit("Sätt DISCORD_TOKEN och DISCORD_CHANNEL_ID")
    client.run(TOKEN, log_handler=None)
