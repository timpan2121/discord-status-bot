# Discord-statusbot

Bevakar Microsoft 365/Azure, Cloudflare, Google Workspace, Discord, Zoom, Slack, OpenAI, Fortnox,
BankID, svenska ISP:er m.fl. och postar: 🔴 ny incident → 🟠 uppdateringar → ✅ löst.

Botens status i medlemslistan:
- 🟢 Online "N tjänster – allt OK"
- 🌙 Inaktiv "⚠ X pågående störningar"
- ⛔ Stör ej "⚠ Ingen internetanslutning" (VM uppe men nätet nere – inga felmeddelanden postas)
- ⚫ Offline = VM:en/boten är avstängd (Discord sköter det automatiskt)

## 1. Skapa boten
1. Gå till https://discord.com/developers/applications → **New Application** → döp den (t.ex. "Driftstatus").
2. **Bot** → **Reset Token** → kopiera token (visa den aldrig för någon).
   Inga "Privileged Gateway Intents" behövs.
3. **OAuth2 → URL Generator**: bocka `bot` + `applications.commands`.
   Behörigheter: `View Channels`, `Send Messages`, `Embed Links`, `Mention Everyone` (om du vill pinga roller).
4. Öppna länken längst ner → välj din server → **Auktorisera**.

## 2. Hämta kanal-ID
Discord → Inställningar → Avancerat → slå på **Utvecklarläge**.
Högerklicka kanalen (t.ex. #driftstatus) → **Kopiera kanal-ID**.

## 3. Konfigurera och testa
```
cd ~/discord-status-bot
cp .env.example .env   # fyll i token och kanal-ID
chmod 600 .env
set -a; . ./.env; set +a; .venv/bin/python bot.py
```
Boten blir grön i servern och postar pågående incidenter. Skriv `/status` för att se aktuella störningar.

## 4. Kör automatiskt vid uppstart
```
mkdir -p ~/.config/systemd/user
cp statusbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now statusbot
loginctl enable-linger $USER     # kör även när du inte är inloggad
journalctl --user -u statusbot -f   # loggar
```

## Alternativ: bara webhook (enklast, men ingen online/offline)
Kanal → Integrationer → Webhooks → Ny webhook → kopiera URL, sedan
`DISCORD_WEBHOOK_URL=... .venv/bin/python statusbot.py`

Källor läggs till/stängs av i `config.toml`. Är VM:en avstängd länge postas vid start bara det som
ändrats sedan dess (state sparas i `state.json`), så det blir ingen flod av gamla notiser.

## Köra gratis i molnet med GitHub Actions (ingen VM behövs)
GitHub kör `statusbot.py --once` var 5:e minut och postar via en Discord-webhook.
Gratis med **publikt** repo (webhook-URL:en ligger som krypterad Secret och syns inte).

1. Discord: #status → Redigera kanal → Integrationer → Webhooks → **Ny webhook** → Kopiera URL.
2. Skapa ett nytt **publikt** repo på https://github.com/new (t.ex. `discord-status-bot`, utan README).
3. Pusha koden härifrån:
   ```
   cd ~/discord-status-bot
   git remote add origin https://github.com/DITT-NAMN/discord-status-bot.git
   git push -u origin main
   ```
4. I repot: **Settings → Secrets and variables → Actions → New repository secret**
   Namn `DISCORD_WEBHOOK_URL`, värde = webhook-URL:en. (Valfritt: `DEEPL_API_KEY`.)
5. **Actions**-fliken → "Driftstatus till Discord" → **Run workflow**.
   Första körningen lär sig nuläget (postar inget), sedan postas nya störningar/uppdateringar/lösningar.

Obs:
- Kör inte både VM-boten och GitHub Actions samtidigt, då kommer notiserna dubbelt.
- Webhook-läget har ingen online/offline-status och inga /kommandon – bara notiser.
- GitHub pausar schemalagda körningar om repot inte haft någon aktivitet på 60 dagar;
  då får du ett mejl och klickar "Enable workflow" (eller gör en liten commit).
