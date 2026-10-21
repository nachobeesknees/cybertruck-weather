# Cybertruck Weather Dashboard

One file (`index.html`), no build step, no API keys. Data sources: RainViewer and LibreWXR radar, LibreWXR GOES satellite, Open-Meteo conditions and wind, NWS alerts, and the SPC Day 1 outlook.

## Host on GitHub Pages
1. Create a new public repo on GitHub, for example `cybertruck-wx`.
2. Upload `index.html` to the root of the repo (Add file, then Upload files, then Commit).
3. Open the repo's **Settings**, then **Pages**. Under "Build and deployment," pick **Deploy from a branch**, choose `main` and `/ (root)`, and save.
4. After a minute or so, the site is live at `https://<your-username>.github.io/cybertruck-wx/`.
5. In the Cybertruck browser, open that URL and bookmark it. Tap ⛶ for fullscreen and allow location if you want speed and heading.

## Change the center
Near the top of the `<script>` block in `index.html`:
```js
const HOME={lat:33.53,lon:-111.93,name:"Scottsdale 85253"};
const ALERT_POINT="33.45,-112.07";
const SW_VIEW={center:[34.5,-111.5],zoom:6};
```
`HOME` sets the map center and current conditions. `ALERT_POINT` is the "lat,lon" the NWS alerts lookup uses. `SW_VIEW` sets the wide regional view. Commit the change and Pages redeploys on its own.

## Notes
- Radar refreshes every 5 minutes, alerts every 10, and conditions every 10.
- The briefing pulls the "WHAT..." lines from the top active NWS alerts, so it always matches the live warnings and never uses hard-coded text.
- LibreWXR's satellite feed only lists infrared frames right now. If it adds a visible list, the dashboard switches to visible during daytime on its own.
- Wind is a 5×5 grid of Open-Meteo arrows (speed and gusts in mph) that redraws when you pan.
- Vehicle: Tesla gives the browser no SDK, so speed and heading come from browser location when you allow it. Route shows a placeholder.
