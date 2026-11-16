#!/usr/bin/env python3
"""Statusbot: övervakar statussidor och postar incidenter till Discord via webhook.

Källtyper (sätts med `type` i config.toml):
  statuspage   - Atlassian Statuspage-API (Cloudflare, Discord, GitHub, Zoom, Dropbox m.fl.)
  rss          - RSS/Atom-flöden (Azure, AWS m.fl.)
  google       - Google Workspace / Cloud incidents.json
  slack        - Slacks status-API
  msgraph      - Microsoft 365 Service Health via Graph API (kräver app-registrering)
  webpage      - Driftsidor utan API (svenska ISP:er): larmar när sidans text ändras
                 eller när nyckelord dyker upp

Kör:  python3 statusbot.py            (loopar)
      python3 statusbot.py --once     (en runda)
      python3 statusbot.py --once --dry-run   (skriv ut istället för att posta)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger("statusbot")
UA = {"User-Agent": "statusbot/1.0 (+discord status notifier)"}
HTTP_TIMEOUT = 20

COLORS = {
    "critical": 0xE74C3C, "major": 0xE67E22, "minor": 0xF1C40F,
    "maintenance": 0x3498DB, "resolved": 0x2ECC71, "info": 0x95A5A6,
}


@dataclass
class Incident:
    uid: str                 # unikt id inom källan
    title: str
    status: str              # t.ex. investigating / resolved
    severity: str = "info"   # nyckel i COLORS
    url: str = ""
    body: str = ""
    components: list[str] = field(default_factory=list)
    updated: str = ""        # ändras -> ny uppdatering postas
    resolved: bool = False


# ---------------------------------------------------------------- svenska

STATUS_SV = {
    "investigating": "Undersöks", "identified": "Orsak identifierad", "monitoring": "Övervakas",
    "resolved": "Löst", "postmortem": "Löst (efteranalys)", "completed": "Slutfört",
    "scheduled": "Planerat underhåll", "in_progress": "Pågår", "verifying": "Verifieras",
    "update": "Uppdatering", "open": "Pågår", "active": "Pågår", "reported": "Rapporterat",
    "changed": "Ändrad", "ok": "OK", "service degradation": "Försämrad tjänst",
    "service interruption": "Avbrott", "servicedegradation": "Försämrad tjänst",
    "serviceinterruption": "Avbrott", "restoringservice": "Återställer tjänsten",
    "extendedrecovery": "Utökad återställning", "investigationsuspended": "Utredning pausad",
    "servicerestored": "Tjänsten återställd", "falsepositive": "Falsklarm",
    "operational": "Fungerar", "available": "Fungerar", "degraded performance": "Försämrad prestanda",
    "partial outage": "Delvis avbrott", "major outage": "Större avbrott", "outage": "Avbrott",
    "under maintenance": "Underhåll", "degraded performance ": "Försämrad prestanda",
    "partial outage ": "Delvis avbrott", "incident": "Incident", "cl": "Löst", "in": "Undersöks", "id": "Orsak identifierad",
    "mo": "Övervakas", "op": "Pågår", "rs": "Löst", "ac": "Pågår", "pd": "Delvis försämrad", "dg": "Försämrad", "mj": "Större avbrott",
    "down": "Nere", "critical": "Kritisk", "trouble": "Problem", "under maintenance": "Underhåll", "notice": "Meddelande", "maintenance": "Underhåll",
}

_tcache: dict[str, str] = {}
_tcache_path = Path(__file__).with_name("translations.json")
_tconf: dict = {"mode": "auto"}
_tfail_until = 0.0


def status_sv(status: str) -> str:
    return STATUS_SV.get((status or "").strip().lower(), status)


def _load_tcache() -> None:
    if not _tcache and _tcache_path.exists():
        try:
            _tcache.update(json.loads(_tcache_path.read_text()))
        except ValueError:
            pass


def _save_tcache() -> None:
    if len(_tcache) > 3000:
        for k in list(_tcache)[:1000]:
            del _tcache[k]
    tmp = _tcache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_tcache, ensure_ascii=False))
    tmp.replace(_tcache_path)


def _chunks(text: str, size: int = 480) -> list[str]:
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > size:
            parts.append(sent[:size]); sent = sent[size:]
        if len(cur) + len(sent) + 1 > size:
            parts.append(cur); cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def _translate_raw(text: str) -> str:
    key = os.environ.get("DEEPL_API_KEY")
    if key and _tconf["mode"] in ("auto", "deepl"):
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        r = requests.post(f"https://{host}/v2/translate", timeout=HTTP_TIMEOUT,
                          headers={"Authorization": f"DeepL-Auth-Key {key}"},
                          data={"text": text, "target_lang": "SV", "source_lang": "EN"})
        r.raise_for_status()
        return r.json()["translations"][0]["text"]
    out = []
    for part in _chunks(text):
        j = get("https://api.mymemory.translated.net/get", params={"q": part, "langpair": "en|sv"}).json()
        if j.get("quotaFinished") or str(j.get("responseStatus")) != "200":
            raise RuntimeError(f"MyMemory: {j.get('responseDetails') or 'kvot slut'}")
        out.append(html.unescape(j["responseData"]["translatedText"]))
    return " ".join(out)


def sv(text: str) -> str:
    """Översätt engelska -> svenska. Cachat. Vid fel: originaltext (och paus 1 h)."""
    global _tfail_until
    if not text or _tconf["mode"] == "off" or time.time() < _tfail_until:
        return text
    _load_tcache()
    k = hashlib.sha1(text.encode()).hexdigest()
    if k in _tcache:
        return _tcache[k]
    try:
        res = _translate_raw(text)
    except Exception as ex:  # noqa: BLE001
        log.warning("Översättning misslyckades, visar original: %s", ex)
        _tfail_until = time.time() + 3600
        return text
    _tcache[k] = res
    _save_tcache()
    return res


def clean(text: str, limit: int = 1000) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def get(url: str, **kw) -> requests.Response:
    headers = {**UA, **kw.pop("headers", {})}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- källor

def src_statuspage(cfg: dict) -> list[Incident]:
    base = cfg["url"].rstrip("/")
    data = get(f"{base}/api/v2/incidents.json").json()
    out = []
    for inc in data.get("incidents", [])[:15]:
        upd = (inc.get("incident_updates") or [{}])[0]
        resolved = inc.get("status") in ("resolved", "postmortem", "completed")
        sev = "resolved" if resolved else (
            "maintenance" if inc.get("scheduled_for") else inc.get("impact", "minor"))
        if sev == "none":
            sev = "minor"
        out.append(Incident(
            uid=inc["id"], title=inc.get("name", "?"), status=inc.get("status", "?"),
            severity=sev, url=inc.get("shortlink") or f"{base}/incidents/{inc['id']}",
            body=clean(upd.get("body", "")),
            components=[c.get("name", "") for c in inc.get("components", [])],
            updated=inc.get("updated_at", ""), resolved=resolved,
        ))
    return out


def src_rss(cfg: dict) -> list[Incident]:
    root = ET.fromstring(get(cfg["url"]).content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    out = []
    for it in items[:15]:
        def t(tag):
            el = it.find(tag) if it.find(tag) is not None else it.find(f"a:{tag}", ns)
            return (el.text or "") if el is not None else ""
        link = t("link")
        if not link:
            el = it.find("a:link", ns)
            link = el.get("href", "") if el is not None else ""
        title = t("title")
        uid = t("guid") or t("id") or link or title
        body = t("description") or t("summary") or t("content")
        upd = t("pubDate") or t("updated")
        low = (title + " " + body).lower()
        resolved = any(w in low for w in ("resolved", "mitigated", "åtgärdat", "löst"))
        out.append(Incident(uid=uid, title=clean(title, 250), status="resolved" if resolved else "reported",
                            severity="resolved" if resolved else "major", url=link,
                            body=clean(body), resolved=resolved,
                            # signatur på innehållet, inte datum: vissa flöden byter datum vid varje hämtning
                            updated=hashlib.sha1(f"{title}|{body}".encode()).hexdigest()))
    return out


def _gtitle(text: str) -> str:
    t = re.sub(r"\*\*\s*(Title|Summary|Description)\s*:?\s*\*\*\s*:?", "", text, flags=re.I)
    t = t.replace("**", "").strip().split("\n")[0]
    return clean(t, 200)


def src_google(cfg: dict) -> list[Incident]:
    """Google Workspace / Google Cloud (samma format). Filtrera med `products` eller `exclude_products`."""
    url = cfg.get("url", "https://www.google.com/appsstatus/dashboard/incidents.json")
    base = url.rsplit("/", 1)[0] + "/"
    want = {p.lower() for p in cfg.get("products", [])}
    skip = {p.lower() for p in cfg.get("exclude_products", [])}
    out = []
    for inc in get(url).json()[:30]:
        prods = [p.get("title", "") for p in inc.get("affected_products", [])]
        low = {p.lower() for p in prods}
        if want and not (low & want):
            continue
        if skip and low and low <= skip:
            continue
        resolved = bool(inc.get("end"))
        sev = "resolved" if resolved else {"high": "critical", "medium": "major"}.get(inc.get("severity"), "minor")
        upd = inc.get("most_recent_update") or {}
        out.append(Incident(
            uid=inc["id"], title=_gtitle(inc.get("external_desc", "?")),
            status="resolved" if resolved else upd.get("status", "open"), severity=sev,
            url=base + inc.get("uri", ""), body=clean(upd.get("text", "")),
            components=prods[:10], updated=inc.get("modified", ""), resolved=resolved))
    return out


def src_slack(cfg: dict) -> list[Incident]:
    data = get("https://slack-status.com/api/v2.0.0/current").json()
    out = []
    for inc in data.get("active_incidents", []):
        notes = inc.get("notes") or [{}]
        out.append(Incident(
            uid=str(inc["id"]), title=inc.get("title", "?"), status=inc.get("status", "active"),
            severity="major" if inc.get("type") == "outage" else "minor", url=inc.get("url", ""),
            body=clean(notes[-1].get("body", "")), components=inc.get("services", []),
            updated=inc.get("date_updated", "")))
    return out


_graph_token: dict = {}


def src_msgraph(cfg: dict) -> list[Incident]:
    tenant = os.environ.get(cfg.get("tenant_env", "MS_TENANT_ID"))
    cid = os.environ.get(cfg.get("client_id_env", "MS_CLIENT_ID"))
    secret = os.environ.get(cfg.get("secret_env", "MS_CLIENT_SECRET"))
    if not (tenant and cid and secret):
        raise RuntimeError("MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET saknas")
    if _graph_token.get("exp", 0) < time.time() + 60:
        r = requests.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", timeout=HTTP_TIMEOUT,
                          data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials",
                                "scope": "https://graph.microsoft.com/.default"})
        r.raise_for_status()
        j = r.json()
        _graph_token.update(tok=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    url = ("https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/issues"
           "?$orderby=lastModifiedDateTime desc&$top=25")
    data = get(url, headers={"Authorization": f"Bearer {_graph_token['tok']}"}).json()
    out = []
    for i in data.get("value", []):
        if i.get("classification") == "advisory" and not cfg.get("include_advisories", True):
            continue
        resolved = bool(i.get("isResolved"))
        sev = "resolved" if resolved else ("major" if i.get("classification") == "incident" else "minor")
        posts = i.get("posts") or []
        body = clean(posts[-1].get("description", {}).get("content", "")) if posts else clean(i.get("impactDescription", ""))
        out.append(Incident(
            uid=i["id"], title=f"{i['id']}: {i.get('title', '?')}", status=i.get("status", "?"), severity=sev,
            url="https://admin.microsoft.com/#/servicehealth/:/alerts/" + i["id"],
            body=body or clean(i.get("impactDescription", "")), components=[i.get("service", "")],
            updated=i.get("lastModifiedDateTime", ""), resolved=resolved))
    return out


def src_webpage(cfg: dict) -> list[Incident]:
    """För sidor utan API. Hashar sidans text (eller en regex-utdragen del).
    Larmar om innehållet ändras, eller (om `keywords` satt) när något nyckelord finns."""
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|header|template)\b.*?</\1>", " ", get(cfg["url"]).text)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    text = clean(raw, 10**7)
    if cfg.get("extract"):
        m = re.search(cfg["extract"], text, re.S | re.I)
        text = m.group(1 if m.groups() else 0) if m else ""
    if cfg.get("ok_text"):
        ok = cfg["ok_text"].lower() in text.lower()
        return [Incident(uid="page", title=f"{cfg.get('name', '')} " + ("fungerar igen" if ok else "rapporterar problem"),
                         status="ok" if ok else "problem", severity="resolved" if ok else "major", resolved=ok,
                         url=cfg["url"], body="" if ok else clean(text[:500]), updated="ok" if ok else "problem")]
    kws = [k.lower() for k in cfg.get("keywords", [])]
    if kws:
        hits = [k for k in kws if k in text.lower()]
        if not hits:
            return [Incident(uid="page", title="Inga störningar", status="ok", resolved=True,
                             severity="resolved", url=cfg["url"], updated="ok")]
        snippet = ""
        idx = text.lower().find(hits[0])
        snippet = text[max(0, idx - 200): idx + 600]
        return [Incident(uid="page", title=f"Möjlig störning ({', '.join(hits)})", status="reported",
                         severity="major", url=cfg["url"], body=clean(snippet),
                         updated=hashlib.sha1(snippet.encode()).hexdigest())]
    h = hashlib.sha1(text.encode()).hexdigest()
    return [Incident(uid="page", title="Driftsidan har uppdaterats", status="changed", severity="minor",
                     url=cfg["url"], body=f"Se senaste informationen på [driftsidan]({cfg['url']}).", updated=h)]


def src_freshstatus(cfg: dict) -> list[Incident]:
    """Freshworks-produkter (Freshdesk m.fl.) via public-api.freshstatus.io."""
    api, acc = "https://public-api.freshstatus.io/v1", cfg["account_id"]
    page = cfg.get("page") or cfg.get("url", "")
    data = get(f"{api}/public-incidents/", params={"account_id": acc}).json()
    out = []
    for inc in data.get("results", [])[:10]:
        if inc.get("is_private"):
            continue
        resolved = bool(inc.get("end_time"))
        recent = not resolved or inc["end_time"] >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        body, status, sig = inc.get("description", ""), "resolved" if resolved else "investigating", inc.get("end_time") or ""
        if recent:  # hämta senaste uppdateringen bara för aktuella incidenter
            ups = get(f"{api}/public-incident-updates/", params={"account_id": acc, "incident": inc["id"]}).json()
            ups = sorted(ups.get("results", []), key=lambda u: u.get("display_time") or "")
            if ups:
                body = ups[-1].get("message") or body
                status = ups[-1].get("new_status") or status
                sig = f"{sig}|{ups[-1]['id']}"
        out.append(Incident(
            uid=str(inc["id"]), title=inc.get("title", "?"), status=status,
            severity="resolved" if resolved else ("maintenance" if inc.get("is_planned") else "major"),
            url=page, body=clean(body), updated=sig, resolved=resolved))
    return out


def src_statusiq(cfg: dict) -> list[Incident]:
    """Zoho m.fl. (Site24x7 StatusIQ). RSS med en rad per komponent: 'Namn - Status'.
    `match` = lista med textbitar; bara komponenter som innehåller någon av dem bevakas."""
    root = ET.fromstring(get(cfg["url"].rstrip("/") + "/rss").content)
    match = [m.lower() for m in cfg.get("match", [])]
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        comp, _, status = title.rpartition(" - ")
        comp = re.sub(r"\s+", " ", comp or title)
        if match and not any(m in comp.lower() for m in match):
            continue
        ok = status.strip().lower() in ("operational", "up")
        out.append(Incident(
            uid=comp, title=f"{comp}: {status}" if not ok else f"{comp} fungerar igen", status=status,
            severity="resolved" if ok else ("maintenance" if "maint" in status.lower() else "major"),
            url=it.findtext("link") or cfg["url"], body=clean(it.findtext("description") or ""),
            components=[comp], updated=status, resolved=ok))
    return out


_summary_cache: dict[str, tuple[float, dict]] = {}


def _summary(base: str) -> dict:
    """summary.json cachas 60 s så att många komponentkällor delar ett anrop."""
    hit = _summary_cache.get(base)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    data = get(f"{base}/api/v2/summary.json").json()
    _summary_cache[base] = (time.time(), data)
    return data


COMP_SV = {"degraded_performance": ("minor", "Försämrad prestanda"), "partial_outage": ("major", "Delvis avbrott"),
           "major_outage": ("critical", "Större avbrott"), "under_maintenance": ("maintenance", "Underhåll")}


def src_statuspage_component(cfg: dict) -> list[Incident]:
    """En enskild komponent på en Statuspage-sida, t.ex. en bank på status.swish.nu."""
    base = cfg["url"].rstrip("/")
    data = _summary(base)
    want = cfg["component"].lower()
    comp = next((c for c in data.get("components", []) if c.get("name", "").lower() == want), None)
    if comp is None:
        raise RuntimeError(f"komponenten {cfg['component']!r} finns inte på {base}")
    st = comp.get("status", "operational")
    ok = st == "operational"
    sev, label = COMP_SV.get(st, ("minor", st))
    related = [i for i in data.get("incidents", []) + data.get("scheduled_maintenances", [])
               if any(c.get("id") == comp["id"] for c in i.get("components", []))]
    body, url = "", cfg.get("page") or base
    if related:
        upd = (related[0].get("incident_updates") or [{}])[0]
        body = f"**{related[0].get('name', '')}**\n{clean(upd.get('body', ''))}"
        url = related[0].get("shortlink") or url
    via = cfg.get("via", "")
    return [Incident(
        uid=comp["id"], title=f"{cfg.get('name', comp['name'])} fungerar igen{via}" if ok
        else f"{label} – {comp['name']}{via}", status=st.replace("_", " "),
        severity="resolved" if ok else sev, url=url, body=body, components=[comp["name"]],
        updated=st + "|" + str([u.get("id") for i in related for u in i.get("incident_updates", [])[:1]]),
        resolved=ok)]


def src_apple(cfg: dict) -> list[Incident]:
    """Apples systemstatus. `services` = lista med tjänstenamn eller prefix (t.ex. "iCloud")."""
    t = get("https://www.apple.com/support/systemstatus/data/system_status_en_US.js").text
    data = json.loads(t[t.index("{"): t.rindex("}") + 1])
    want = [w.lower() for w in cfg.get("services", [])]
    out = []
    for svc in data.get("services", []):
        name = svc.get("serviceName", "")
        if want and not any(name.lower().startswith(w) for w in want):
            continue
        for ev in svc.get("events", []):
            resolved = ev.get("eventStatus") in ("resolved", "completed")
            typ = (ev.get("statusType") or "").lower()
            sev = "resolved" if resolved else ("maintenance" if "maint" in typ else
                                               "critical" if typ == "outage" else "minor")
            out.append(Incident(
                uid=f"{name}|{ev.get('messageId')}", title=f"{name}: {ev.get('message', '')}",
                status=ev.get("eventStatus", "?"), severity=sev,
                url="https://www.apple.com/se/support/systemstatus/",
                body=clean(f"{ev.get('usersAffected') or ''}"), components=[name],
                updated=f"{ev.get('eventStatus')}|{ev.get('datePosted')}", resolved=resolved))
    return out


def _psn_msg(st: dict) -> str:
    ms = (st.get("message") or {}).get("messages") or {}
    if isinstance(ms, dict):
        for loc in ("sv-SE", "en-GB", "en-US"):
            if ms.get(loc):
                return str(ms[loc])
    return ""


def src_playstation(cfg: dict) -> list[Incident]:
    """PlayStation Network: status för ett land (default SE) + regionstörningar med sv/en-text."""
    d = get(f"https://status.playstation.com/data/statuses/region/{cfg.get('region', 'SCEE')}.json").json()
    found: list[tuple[str, dict]] = []
    for st in d.get("status", []):
        if _psn_msg(st):              # regionstörningar utan sv/en-text gäller andra länder
            found.append(("PlayStation Network", st))
    country = next((c for c in d.get("countries", []) if c.get("countryCode") == cfg.get("country", "SE")), {})
    for st in country.get("status", []):
        found.append(("PlayStation Network", st))
    for svc in country.get("services", []):
        sname = (svc.get("i18n") or {}).get("en-GB") or svc.get("serviceName", "")
        for st in svc.get("status", []):
            found.append((sname, st))
        for res in svc.get("resources", []):
            for st in res.get("status", []):
                found.append((f"{sname} – {(res.get('i18n') or {}).get('en-GB') or res.get('resourceName', '')}", st))
    out = []
    for where, st in found:
        typ = st.get("statusType", "Outage")
        resolved = bool(st.get("endDate"))
        out.append(Incident(
            uid=st.get("statusId", where), title=f"{where}: {'underhåll' if 'maint' in typ.lower() else 'störning'}",
            status=typ, severity="resolved" if resolved else ("maintenance" if "maint" in typ.lower() else "major"),
            url="https://status.playstation.com/sv-se/", body=clean(_psn_msg(st)), components=[where],
            updated=st.get("modifiedDate", ""), resolved=resolved))
    return out


def src_xbox(cfg: dict) -> list[Incident]:
    """Xbox Live (officiellt XML-flöde). En incident per funktion som inte är 'None'."""
    root = ET.fromstring(get(f"https://xnotify.xboxlive.com/servicestatusv6/{cfg.get('country', 'SE')}/sv-SE").content)
    out = []
    for cat in root.iter("Category"):
        cname = cat.findtext("Name", "")
        for sc in cat.iter("Scenario"):
            state = sc.findtext("Status/Name", "None")
            if state in ("None", "Normal", ""):
                continue
            name = sc.findtext("Name", "")
            out.append(Incident(
                uid=sc.findtext("Id", name), title=f"{cname} – {name}: {state}", status=state,
                severity="critical" if state.lower() in ("major", "critical") else "major",
                url="https://support.xbox.com/sv-SE/xbox-live-status", components=[cname],
                body=clean(" ".join(t.text or "" for t in sc.iter("Message"))), updated=state))
    return out


def src_http(cfg: dict) -> list[Incident]:
    """Egen nåbarhetskontroll när tjänsten saknar statussida. 3 försök innan larm."""
    bad = []
    for url in cfg["urls"]:
        err = ""
        for attempt in range(3):
            try:
                r = requests.get(url, headers=UA, timeout=15)
                if r.status_code < 500:
                    err = ""
                    break
                err = f"HTTP {r.status_code}"
            except requests.RequestException as ex:
                err = type(ex).__name__
            time.sleep(5)
        if err:
            bad.append(f"{url} ({err})")
    if not bad:
        return [Incident(uid="reach", title=f"{cfg.get('name', '')} svarar igen", status="ok", resolved=True,
                         severity="resolved", url=cfg.get("page", ""), updated="ok")]
    return [Incident(uid="reach", title=f"{cfg.get('name', '')} svarar inte", status="nere", severity="major",
                     url=cfg.get("page", ""), body="Kunde inte nå: " + ", ".join(bad), updated="down")]


def src_meta(cfg: dict) -> list[Incident]:
    """Meta (metastatus.com). `orgs` = lista med org-id, t.ex. ["messenger", "whatsapp-business-api"]."""
    orgs = get("https://metastatus.com/data/orgs.json").json()
    want = set(cfg.get("orgs", []))
    out = []
    for org in orgs:
        if want and org.get("id") not in want:
            continue
        for svc in org.get("services", []):
            st = svc.get("status") or ""
            ok = st.lower().startswith("no known issues") or st == ""
            out.append(Incident(
                uid=f"{org['id']}/{svc.get('id', svc.get('name'))}",
                title=f"{org.get('name', org['id'])}: {svc.get('name', '')}" + ("" if ok else f" – {st}"),
                status=st or "OK", severity="resolved" if ok else ("critical" if "outage" in st.lower() else "major"),
                url="https://metastatus.com/", components=[org.get("name", "")],
                updated=f"{st}|{svc.get('time')}", resolved=ok))
    return out


def src_inera(cfg: dict) -> list[Incident]:
    """Inera driftstatus (1177 m.fl.). `match` = lista med namn-början, t.ex. ["1177 app", "1177 inkorg"]."""
    d = get("https://www.inera.se/api/v2/service/all").json()
    want = [m.lower() for m in cfg.get("match", [])]
    out = []
    for x in d.get("allStatuses", []):
        name = x.get("name", "")
        if want and not any(name.lower().startswith(w) for w in want):
            continue
        lvl = int(x.get("statusLevel") or 0)
        ok = lvl == 0
        out.append(Incident(
            uid=name, title=f"{name}: {x.get('statusName', '')}" if not ok else f"{name} fungerar igen",
            status=x.get("statusName", ""), severity="resolved" if ok else ("major" if lvl >= 2 else "minor"),
            url="https://www.inera.se/driftstatus/", body=clean(x.get("details") or ""), components=[name],
            updated=f"{lvl}|{x.get('date')}|{x.get('details')}", resolved=ok))
    return out


def src_link(cfg: dict) -> list[Incident]:
    """Bara en länk i översikten (sidor som kräver adress/JS). Pollas inte, postar aldrig."""
    return []


def src_m365public(cfg: dict) -> list[Incident]:
    """Publika status.cloud.microsoft (ingen inloggning). En rad per tjänst."""
    svc = cfg.get("service", "m365Consumer")
    data = get(f"https://status.cloud.microsoft/api/posts/{svc}").json()
    out = []
    for p in data:
        status = p.get("Status") or "?"
        ok = status.lower() in ("operational", "available", "serviceoperational", "serviceRestored".lower())
        msg = p.get("Message") or ""
        sig = hashlib.sha1(f"{status}|{p.get('Title')}|{msg}".encode()).hexdigest()
        sev = "resolved" if ok else ("critical" if "interruption" in status.lower() or "outage" in status.lower()
                                     else "major")
        out.append(Incident(
            uid=p.get("ServiceWorkloadName") or p.get("ServiceDisplayName", "?"),
            title=p.get("Title") or f"{p.get('ServiceDisplayName')} fungerar igen",
            status=status, severity=sev, url="https://status.cloud.microsoft/",
            body=clean(msg, 1500), components=[p.get("ServiceDisplayName", "")],
            updated=sig, resolved=ok))
    return out


SOURCES = {"link": src_link, "inera": src_inera, "meta": src_meta, "playstation": src_playstation, "xbox": src_xbox, "http": src_http, "apple": src_apple, "statuspage_component": src_statuspage_component, "freshstatus": src_freshstatus, "statusiq": src_statusiq, "m365public": src_m365public, "statuspage": src_statuspage, "rss": src_rss, "google": src_google,
           "slack": src_slack, "msgraph": src_msgraph, "webpage": src_webpage}


# ---------------------------------------------------------------- Discord

def embed(provider: str, inc: Incident, is_new: bool) -> dict:
    prefix = "✅ Löst" if inc.resolved else ("🔴 Ny incident" if is_new else "🟠 Uppdatering")
    native = inc.uid == "page"  # driftsidor är redan på svenska
    title, body = (inc.title, inc.body) if native else (sv(inc.title), sv(inc.body))
    e = {
        "title": f"{prefix} – {provider}"[:256],
        "description": f"**{title}**\n\n{body}"[:4000],
        "color": COLORS.get(inc.severity, COLORS["info"]),
        "fields": [{"name": "Status", "value": status_sv(inc.status) or "-", "inline": True}],
        "footer": {"text": f"statusbot • {provider}"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if inc.url.startswith("http"):
        e["url"] = inc.url
    if inc.components:
        e["fields"].append({"name": "Påverkar", "value": ", ".join(c for c in inc.components if c)[:1000] or "-",
                            "inline": True})
    return e


def post(webhook: str, embeds: list[dict], mention: str, dry: bool) -> None:
    for i in range(0, len(embeds), 10):
        payload = {"username": "Driftstatus", "embeds": embeds[i:i + 10]}
        if mention:
            payload["content"] = mention
            payload["allowed_mentions"] = {"parse": ["roles", "everyone"]}
        if dry:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        for _ in range(3):
            r = requests.post(webhook, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            break


# ---------------------------------------------------------------- huvudloop

def run_once(conf: dict, state: dict, dry: bool, sender=None) -> dict:
    """Pollar källor som står på tur. Returnerar {"ok": n, "failed": n}."""
    stats = {"ok": 0, "failed": 0}
    _tconf["mode"] = conf.get("language", {}).get("translate", "auto")
    default_hook = os.environ.get("DISCORD_WEBHOOK_URL") or conf.get("discord", {}).get("webhook_url", "")
    now = time.time()
    for name, sc in conf.get("source", {}).items():
        if not sc.get("enabled", True):
            continue
        st = state.setdefault(name, {"seen": {}, "last": 0, "fails": 0})
        if now - st.get("last", 0) < sc.get("interval", conf.get("interval", 120)) - 5:
            continue
        st["last"] = now
        try:
            incidents = SOURCES[sc["type"]](sc)
            st["fails"] = 0
            stats["ok"] += 1
        except Exception as ex:  # noqa: BLE001
            # Fel loggas bara lokalt (en gång per felserie) - postas aldrig till Discord
            st["fails"] = st.get("fails", 0) + 1
            stats["failed"] += 1
            if st["fails"] == 1 or st["fails"] % 30 == 0:
                log.warning("%s: %s (fel #%d)", name, ex, st["fails"])
            continue
        first_run = not st["seen"] and not st.get("initialized")
        st["initialized"] = True
        provider = sc.get("name", name)
        track = sc["type"] != "webpage" or sc.get("keywords")
        st["open"] = {i.uid: [sv(i.title), i.url, i.severity, status_sv(i.status)] for i in incidents
                      if track and not i.resolved}
        st["checked"] = now
        out = []
        for inc in incidents:
            prev = st["seen"].get(inc.uid)
            st["seen"][inc.uid] = inc.updated
            if first_run and (inc.resolved or not conf.get("announce_open_on_start", True)):
                continue  # posta inte gamla historiska händelser vid första start
            if first_run and sc["type"] == "webpage" and not sc.get("keywords"):
                continue  # baslinje för sidbevakning
            if prev == inc.updated:
                continue
            if prev is None and inc.resolved:
                continue  # redan löst innan vi såg den
            out.append(embed(provider, inc, is_new=prev is None))
        # håll state litet
        if len(st["seen"]) > 200:
            st["seen"] = dict(list(st["seen"].items())[-200:])
        if out and sender:
            sender(sc, provider, out)
        elif out:
            hook = os.environ.get(sc.get("webhook_env", "")) or sc.get("webhook_url") or default_hook
            mention = sc.get("mention", conf.get("discord", {}).get("mention", ""))
            log.info("%s: %d notiser", name, len(out))
            try:
                post(hook, out, mention, dry)
            except Exception as ex:  # noqa: BLE001
                log.error("Discord-post misslyckades för %s: %s", name, ex)
    return stats


def open_incidents(conf: dict, state: dict) -> list[tuple[str, str]]:
    res = []
    for name, sc in conf.get("source", {}).items():
        if sc.get("enabled", True):
            for v in state.get(name, {}).get("open", {}).values():
                res.append((sc.get("name", name), v[0] if isinstance(v, list) else v))
    return res


def snapshot(conf: dict, state: dict) -> dict:
    """Kompakt status för slash-kommandona (Cloudflare Worker läser denna fil)."""
    services = []
    for key, sc in conf.get("source", {}).items():
        if not sc.get("enabled", True):
            continue
        st = state.get(key, {})
        opn = [v for v in st.get("open", {}).values() if isinstance(v, list)]
        services.append({
            "key": key, "name": sc.get("name", key), "category": sc.get("category", "Övrigt"),
            "url": sc.get("page") or sc.get("url", ""), "type": sc["type"],
            "failing": st.get("fails", 0) >= 3, "checked": int(st.get("checked", 0)),
            "open": [{"title": t, "url": u, "severity": s, "status": stt} for t, u, s, stt in opn[:10]],
        })
    return {"generated": int(time.time()), "services": services}


# ---------------------------------------------------------------- live-översikt (webhook)

SEV_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🟡", "maintenance": "🔧"}
SEV_RANK = {"critical": 4, "major": 3, "minor": 2, "info": 2, "maintenance": 1}
CAT_ORDER = ["Microsoft", "Google", "Vardagsappar", "Strömning & sociala medier", "Spel", "Moln & infrastruktur", "Appar & tjänster", "Banker & betalningar", "Bredband (Sverige)"]


def _svc_view(s: dict) -> tuple[str, str, int]:
    opn = s["open"]
    if s["failing"] and not opn:
        return "⚪", "Kunde inte kontrolleras", 0
    if opn:
        w = max(opn, key=lambda o: SEV_RANK.get(o["severity"], 2))
        lbl = "Underhåll" if w["severity"] == "maintenance" else f"{len(opn)} störning{'ar' if len(opn) > 1 else ''}"
        return SEV_ICON.get(w["severity"], "🟡"), lbl, SEV_RANK.get(w["severity"], 2)
    if s["type"] == "link":
        return "🔗", "Kolla driftkartan för din adress", 0
    if not s["checked"]:
        return "⏳", "Väntar på första kontroll", 0
    if s["type"] == "http":
        return "🟢", "Svarar (nåbarhetskontroll)", 0
    return "🟢", "Fungerar", 0


def overview_embeds(snap: dict) -> list[dict]:
    """Översikt + (om något krånglar) lista med länkar till varje pågående störning."""
    rows = [(s, *_svc_view(s)) for s in snap["services"]]
    worst = max((r[3] for r in rows), default=0)
    bad = [r for r in rows if r[0]["open"]]
    colors = [0x2ECC71, 0x3498DB, 0xF1C40F, 0xE67E22, 0xE74C3C]
    lnk = lambda n, u: f"[{n}]({u})" if u.startswith("http") else n
    e = {"title": "📡 Driftstatus – översikt", "color": colors[worst], "fields": [],
         "description": f"**{'⚠️ Störningar hos %d av %d tjänster' % (len(bad), len(rows)) if bad else '✅ Alla tjänster fungerar'}**",
         "footer": {"text": "🟢 OK  🟡 mindre  🟠 större  🔴 kritisk  🔧 underhåll  🔗 driftkarta  ⚪ okänt"}}
    cats = sorted({r[0]["category"] for r in rows}, key=lambda c: CAT_ORDER.index(c) if c in CAT_ORDER else 99)
    for c in cats:
        lines = [f"{ic} {lnk(s['name'], s['url'])}" + ("" if ic == "🟢" else f" – *{lb}*")
                 for s, ic, lb, rk in sorted((r for r in rows if r[0]["category"] == c), key=lambda r: (-r[3], r[0]["name"]))]
        chunk, first = "", True
        for ln in lines:
            if len(chunk) + len(ln) + 1 > 1024:
                e["fields"].append({"name": c if first else "\u200b", "value": chunk, "inline": False})
                chunk, first = "", False
            chunk = f"{chunk}\n{ln}" if chunk else ln
        if chunk:
            e["fields"].append({"name": c if first else "\u200b", "value": chunk, "inline": False})
    e["fields"].append({"name": "\u200b", "value": f"Uppdateras automatiskt • senast <t:{snap['generated']}:R>", "inline": False})
    out = [e]
    if bad:
        inc = {"title": f"⚠️ Pågående störningar ({sum(len(r[0]['open']) for r in bad)})", "color": 0xE67E22, "fields": []}
        for s, ic, lb, rk in sorted(bad, key=lambda r: -r[3])[:20]:
            val = "\n".join(f"{SEV_ICON.get(o['severity'], '🟡')} {lnk(o['title'][:150], o['url'])}" for o in s["open"][:5])
            inc["fields"].append({"name": s["name"], "value": val[:1024], "inline": False})
        out.append(inc)
    # Discord: max 6000 tecken totalt per meddelande - korta störningslistan vid behov
    def size(es):
        return sum(len(x.get("title", "")) + len(x.get("description", "")) + len(x.get("footer", {}).get("text", ""))
                   + sum(len(f["name"]) + len(f["value"]) for f in x.get("fields", [])) for x in es)
    while len(out) > 1 and size(out) > 5800 and out[1]["fields"]:
        out[1]["fields"].pop()
        out[1]["description"] = "…fler störningar finns, se länkarna i översikten ovan."
    return out


def update_overview(webhook: str, conf: dict, state: dict) -> None:
    """Redigera översiktsmeddelandet (skapa det om det saknas). Bara när något ändrats eller var 30:e min."""
    snap = snapshot(conf, state)
    embeds = overview_embeds(snap)
    sig = hashlib.sha1(json.dumps([[ (s["name"], _svc_view(s)[0], [o["title"] for o in s["open"]]) for s in snap["services"]]],
                                  ensure_ascii=False).encode()).hexdigest()
    ov = state.setdefault("_overview", {})
    if ov.get("sig") == sig and time.time() - ov.get("ts", 0) < 1800 and ov.get("msg"):
        return
    payload = {"username": "Driftstatus", "embeds": embeds}
    r = None
    if ov.get("msg"):
        r = requests.patch(f"{webhook}/messages/{ov['msg']}", json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            ov.pop("msg"); r = None   # någon tog bort meddelandet: skapa nytt
    if r is None:
        r = requests.post(f"{webhook}?wait=true", json=payload, timeout=HTTP_TIMEOUT)
        if r.ok:
            ov["msg"] = r.json()["id"]
            log.info("Översikt skapad (id %s) – fäst den i kanalen", ov["msg"])
    r.raise_for_status()
    ov.update(sig=sig, ts=time.time())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot", help="skriv sammanfattning (JSON) till denna fil efter varje runda")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conf = tomllib.loads(Path(a.config).read_text())
    if not a.dry_run and not (os.environ.get("DISCORD_WEBHOOK_URL") or conf.get("discord", {}).get("webhook_url")):
        sys.exit("Sätt DISCORD_WEBHOOK_URL eller [discord] webhook_url i config.toml")
    sp = Path(a.state)
    state = json.loads(sp.read_text()) if sp.exists() else {}
    while True:
        run_once(conf, state, a.dry_run)
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(sp)
        hook = os.environ.get("DISCORD_WEBHOOK_URL") or conf.get("discord", {}).get("webhook_url")
        if hook and not a.dry_run and conf.get("discord", {}).get("overview", True):
            try:
                update_overview(hook, conf, state)
            except Exception as ex:  # noqa: BLE001
                log.warning("Kunde inte uppdatera översikten: %s", ex)
            tmp.write_text(json.dumps(state)); tmp.replace(sp)
        if a.snapshot:
            Path(a.snapshot).write_text(json.dumps(snapshot(conf, state), ensure_ascii=False))
        if a.once:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
