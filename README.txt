BINANCE P2P MARKET WATCHER -> TELEGRAM

WHAT THIS VERSION WATCHES
Public BUY and SELL offers for USDT / EUR.
Payments: SEPA Instant OR Wise.
Trade value: EUR 500 to EUR 10,000 by default.
BUY means you buy USDT with EUR: lowest price wins.
SELL means you sell USDT for EUR: highest price wins.
This version does not need a Binance API key or merchant API access.
Your previous Telegram token and chat ID load from the same encrypted settings.
The previous personal-order watcher code is retained in notifier.py but is not
used by the current desktop app.

START
1. Close any old notifier window and double-click Start Notifier.cmd.
2. Check your Telegram token and numeric chat ID. Existing settings should load.
3. Click Test market to preview both top-five lists, without sending alerts.
4. Save settings, then Start monitoring.

ALERT RULES
- First monitoring scan sends the current best five on each side: up to 10 alerts.
- New offer enters the best five: notify.
- Previously observed offer enters or re-enters the best five: notify.
- An offer already in the best five improves its price: notify.
- The #1 offer changes: notify, even when the replacement was already #2.
  The replacement can have a worse price than the old leader and still be the
  best available offer now. These alerts say BECAME BEST NOW.
- Unchanged offers, availability-only changes and price changes that do not
  improve an offer's price do not create alerts.
- Equal-price offers retain their previous order to reduce repeat alerts.
- BUY and SELL are ranked separately, combining both accepted payment methods.
- An offer qualifies if some trade value in your range is possible within its
  limits and available balance. It need not support the entire EUR 500-10,000
  range. An EUR 8,000-only offer qualifies; an EUR 25,000-minimum offer does not.

ALERT CONTENT
Action, current rank, EUR per USDT price, approximate usable trade-value range,
available USDT, accepted payment methods, advertiser name, completion rate,
30-day order count, offer ID and Binance offer link.
No Binance login credentials or payment account details are sent to Telegram.
The public feed expresses transaction limits in asset units, with limited
precision. The app converts them to approximate EUR ranges and caps by available
balance. Binance's order page is authoritative for exact limits and eligibility.

HISTORY
While monitoring, every observed offer is saved in market.sqlite3, including
returned offers outside your configured amount range. Each unique offer has
first-seen and last-seen timestamps. Changed offer versions are saved too.
Ranking state and alert delivery state survive restarts.
View saved history shows up to 500 most recently observed offers; the database
retains the full observed history and versions. It is not automatically purged.
Last-seen means present in the watched results, not proof of current availability.
A disappearance does not prove an offer was filled: cancellation, price changes,
filter changes or falling outside the API result limit can also explain it.

LIMITS
- The public endpoint returns up to 20 offers per payment method and side.
  Four queries per scan are combined, deduplicated, filtered and ranked locally.
  These are the best five eligible offers among returned data, not a guaranteed
  complete ranking of every Binance advertisement or your personalized results.
- Personal restrictions (as shown by Restricted buttons on Binance) cannot be
  fully determined from public results. Check each offer in your Binance account.
- Short-lived offers between scans, network downtime and API changes can cause
  missed or delayed observations. Observing all offers on Binance is not assured.
- Checking defaults to 15 seconds after each scan and its alert delivery.
  Requests, alert pacing and rate-limit backoff can make the actual interval longer.
- Telegram alerts are paced at about one per second. Failed delivery retries;
  obsolete queued ranking alerts are superseded after the next successful scan.
  A timeout after Telegram accepts a message or a crash before saving its receipt
  can produce a duplicate.
- Keep the PC on, awake and connected, with the app running. Cloud hosting has
  not been deployed. This is not an always-on Windows service.
- No trading, booking or crypto release is performed.

TELEGRAM SETUP
Create a dedicated bot through the official @BotFather account in Telegram.
Enter the token locally, then open that bot and send /start.
Click Find my chat. Its log verifies the token's bot username and waits for a
message. Send a fresh hello message to that bot if needed.
Once a numeric chat ID appears, Save settings and Send test alert.
A bot does not automatically reply to /start; our app sends its notifications.
Unmute the bot chat and enable Telegram notifications on your phone.

SETTINGS AND PRIVACY
settings.dat uses Windows DPAPI encryption tied to your Windows login.
Saving in this market version retains only the displayed Telegram/settings fields;
Binance API credentials previously saved by the old version are no longer needed.
The market history database is not encrypted. Keep the folder private.
Do not send API keys, bot tokens or screenshots revealing credentials to anyone.

CHECKS
python -m unittest -v
Automated checks use simulated services. The public market was also queried
successfully on 2026-11-12 and populated both ranked lists with live offers.
The user verified Telegram test delivery. No live trade is needed for setup.

OFFICIAL REFERENCES
https://github.com/binance/binance-skills-hub/blob/main/skills/binance/p2p/SKILL.md
https://core.telegram.org/bots/api
