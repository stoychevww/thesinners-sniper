# Drop day: TheSinners FYUT, 13.10, 19:00

All times are Sofia time. The short version: **at 18:50, double-click `sniper.bat`, click "Pre-flight check", then click "Arm for the drop" and wait.** The details follow.

---

## Earlier in the day (do this once, any time before 18:00)

1. **Start the UI.** Double-click `sniper.bat` in the project folder. The first time it installs packages for about a minute, then opens http://127.0.0.1:8800.
2. **API key.** Paste it in the top-left card and click Save. It then shows `● Key set`.
3. **Pre-flight check.** It should end with a `sample turn` line listing your 4 items and `site: / -> locked`. `fast=False` is expected because your account has no fast-mode access.
4. **Rehearse at least twice** (Test card, "Start rehearsal on the mock shop"):
   - once with all options unticked
   - once with "Look-alike names" and "Sizes sell out over time" ticked

   Each time, a Chrome window opens and the agent carts items in the local fake shop. You should get a green card "Everything is in the cart" (or a yellow one when sizes sold out on purpose). Close that Chrome window when it's done.
5. **Autofill for your checkout.** Click "Bot browser (autofill)". In the Chrome window that opens, go to Settings → Autofill and passwords → Addresses and more, and add your name, phone, email and address. If you pay with PayPal, you can also log in to PayPal in this window now. Then **close that window**. It is the same Chrome profile the bot uses at 19:00.
6. **Prepare to pay.** Have your card and your phone (for the bank's 3D Secure confirmation) next to you. Know which Econt office or address you'll pick.
7. **Prevent sleep.** Windows Settings → System → Power: set "When plugged in, put my device to sleep after" to Never. Plug the laptop in. Use a cable connection if you can.

## 17:30–18:30: last recon

8. In the UI, click **"Recon scan"** (about 20 s). Read the log:
   - **`existing product ids: [...]`** — before the drop this was `458, 459, 460, 462, 463, 465`. If you see a new number (e.g. 466), open the **Config** tab and add `"/en-product-details-466.html",` to the `prefetch` list, then click Save.
   - **`new photos: [...]`** — new product photos (the tee, hopefully). Look at them in the **Photos** tab. This step is optional, but if you recognise the tee you can add its photo number to `site_notes` in Config, like the others.

## 18:50: get ready (T-10 min)

9. **Close other heavy programs** (games, downloads, other browsers playing video). Your normal Chrome can stay open.
10. **Make sure the "Bot browser" window is closed.** The bot needs that Chrome profile to itself.
11. **Double-click `sniper.bat`** (or switch to it if it's already open). **Don't close the black window from now on.** Closing it ends the run.
12. **Click "Pre-flight check"** and wait about 20 s. Check that it shows the `sample turn` with your 4 items and `site: / -> locked`. The header chip should show `shop: locked`.
13. **Click "Arm for the drop"** (red button). Leave "Start watching now" and "Standard speed" unticked. Confirm the popup.
    - A Chrome window opens with 5 tabs, all showing the "sold out / see you soon" lock page. That's correct.
    - The phase tracker goes **Starting → Browser ready → Watching** (Watching pulses red).
    - The log shows `cache pre-warmed…` and then `watching for the shop to open…`.
14. **Don't touch the bot's Chrome window.** Don't close tabs, don't click in it. Put the UI tab next to it so you can see both.

## 19:00: the drop

15. When the shop unlocks (it can be a bit before or after 19:00), the phase goes **Shop open → Agent**. The log shows `SHOP IS OPEN` in green, then `turn 1…`, `turn 2…` and browser actions. The Chrome window flips between tabs on its own. **Hands off.**
16. You'll hear **3 beeps** and the phase goes **Handover**. A result card appears:
    - **Green, "Everything is in the cart":** go to the Chrome window. The cart tab is in front.
    - **Yellow, "Partly carted":** something sold out in M (the card says which). Check out what's there.
    - **Red:** the agent failed. **The browser is still yours**, so go to the product tabs and buy manually (select M, click BUY).
17. **Check out by hand, fast:** check the cart has the right items in M, proceed to checkout, let autofill fill your details, choose Econt/delivery, pay, and confirm 3D Secure on your phone.
18. When the order is confirmed, close the bot's Chrome window (or click "End run & close browser").

## If something goes wrong

| Problem | What to do |
|---|---|
| The agent is clearly doing something wrong, or is slow | Click **"Stop agent & take over"**. The Chrome window is yours; the product tabs are already open. |
| The shop is clearly open (you see it on your phone) but the phase is still Watching | Go to the bot Chrome window, press F5 on the product tabs and buy manually. Tell me later and I'll check the logs. |
| The UI page froze or you closed the tab | Open http://127.0.0.1:8800 again. The run keeps going in the background as long as the black window is open. |
| The UI itself won't start | Use the backup: double-click **`sniper-terminal.bat`**. It does the same as "Arm for the drop" with no UI. Press Enter to stop the agent, type `quit` to end. |
| "API key" errors | Paste the key again in the top-left card. Check you still have credit at console.anthropic.com. |
| Any crash | The browser stays open for you, so just buy manually. |

## What it costs

A pre-flight check is about $0.05. The real run is about $0.50–1.50 (standard speed). Every run shows its cost in the header and in the Runs tab.
