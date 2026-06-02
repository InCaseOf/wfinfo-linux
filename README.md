# WFInfo for Linux

This is a limited remake of [wfinfo](https://wfinfo.warframestat.us) in Python and JS for Linux (Wayland).

## Features

-   Supports Wayland
-   Detect rewards screen (inconsistent)
-   Manual trigger detection
-   **Ducat Kiosk scanning** — scan visible kiosk items and see plat + ducat values side-by-side
-   Global keybind (Hyprland only; see [Niri setup](#niri-setup) for manual keybind instructions)
-   Display price and volume stats for items
-   Overlay (like steam overlay)
-   Relic view with price data

### Fissure reward display:

![Reward price display](/readme/reward_display.png)

### Relic view in overlay:

![Relic view](/readme/relic_view.png)

## Requirements

-   `python` >= 3.12
-   `tesseract` for OCR
-   [`AGS`](https://github.com/Aylur/ags) - for GUI
-   `dart-sass` - for compiling GUI styles
-   `grim` - for screenshots
-   `wlr-randr` - for getting active monitor for screenshots
-   `wmctrl` - for WM/DE detection on X11 (not required on Hyprland or Niri)
-   `fish` - for `wfinfo` script

> [!TIP]
> If you _really_ don't want to use `fish`, you can just translate it to your preferred shell.

## Installation

Clone this repo and run the install script. If not on an Arch-based distro or not using the `yay` AUR helper, you must
install all dependencies manually. The script will attempt to add the script to your path if `~/.local/bin` is in your
path, otherwise optionally add it to your path by symlinking the `wfinfo` script to your path directory.

> [!WARNING]
> Do not copy the script, only symlink it. The script depends on its real location to resolve paths.

## Usage

Start the program via the `wfinfo` script in the base directory. The program will monitor Warframe's `EE.log` file
and trigger when it detects a reward screen. As Warframe stores its logs in a buffer and only outputs to the log file
when the buffer is full, the auto detection may be inconsistent.

The reward display can be manually triggered while running via `wfinfo -t`. If on Hyprland, the program will
automatically create a shortcut for the trigger script (`F2` by default) on start. This WILL remove any prior binds
for that key. Otherwise, just create a keybind manually depending on your DE/compositor.

The overlay can be toggled with `F3` (if on Hyprland, otherwise set the shortcut manually). All keybinds can be changed
in `ags/config.user.js`.

## Ducat Kiosk

While standing at a Ducat Kiosk with the inventory screen open, press `F4` (or `Mod+F4` on Niri) to scan the
currently visible grid of Prime parts. An overlay card will appear over each tile showing:

-   **Platinum price** — current market average
-   **Ducat value** — what Baro will give you
-   **Sold volume** — last 24 h and last 48 h
-   **Vaulted indicator** — whether the item is currently vaulted
-   **Recommendation badge** — `Sell for plat`, `Sell for ducats`, or `Either`

The overlay auto-closes after 20 seconds. You can also close it by pressing `F4` again or clicking the kiosk
button in the toolbar.

The recommendation heuristic uses a simple plat-per-ducat conversion: if ducats are worth more than ~15% of
the plat price (i.e. good ducat value), it recommends ducats. If plat is noticeably higher, it recommends
plat. Otherwise it says either.

> [!NOTE]
> Only the currently visible tiles are scanned — scroll down and trigger again to scan the next page.

### Kiosk keybind on Niri

Add the following to the `binds` section of `~/.config/niri/config.kdl`:

```kdl
Mod+F4 { spawn "/path/to/wfinfo" "-k"; }
```

Then run `niri msg action reload-config`.

## Configuration

Configuration is in `ags/config.user.js`. Read the comments in the file for how to configure.

## Niri Setup

Niri is a pure Wayland scrollable-tiling compositor and is fully supported for screen capture. However, Niri does not
allow dynamic keybind injection via CLI (unlike Hyprland's `hyprctl`), so keybinds must be added manually.

When you start `wfinfo`, it will detect Niri via the `NIRI_SOCKET` environment variable and print the exact keybind
lines to add to your config instead of trying to create them automatically.

Add the following to the `binds` section of `~/.config/niri/config.kdl`:

```kdl
binds {
    // ... your other binds ...

    // Trigger WFInfo manual fissure detection
    Mod+F2 { spawn "/path/to/wfinfo" "-t"; }
    // Toggle WFInfo overlay
    Mod+F3 { spawn "/path/to/wfinfo" "-g"; }
    // Trigger Ducat Kiosk scan
    Mod+F4 { spawn "/path/to/wfinfo" "-k"; }
}
```

Replace `/path/to/wfinfo` with the actual path to the `wfinfo` script (e.g. `~/.local/bin/wfinfo` if you symlinked it).

After editing, reload your Niri config:

```bash
niri msg action reload-config
```

## GeForce NOW / No Local EE.log

If you are playing Warframe via **GeForce NOW** (or any setup where Warframe is not installed locally), there is no
`EE.log` file on your machine. In this case:

1. Create a dummy empty log file:
   ```bash
   touch ~/dummy_EE.log
   ```
2. Edit `ags/config.user.js` and update `logPath` to point to the dummy file:
   ```js
   export const logPath = `${Utils.HOME}/dummy_EE.log`;
   ```
3. Make sure `autodetect` is set to `false` (it is by default):
   ```js
   export const autodetect = false;
   ```
4. Start the program normally:
   ```bash
   wfinfo
   ```
5. When the relic reward screen appears in your GeForce NOW stream, manually trigger the OCR via your keybind
   (`Mod+F2` on Niri, `F2` on Hyprland) or from the terminal:
   ```bash
   wfinfo -t
   ```
6. When at a Ducat Kiosk, trigger the kiosk scan (`Mod+F4` on Niri, `F4` on Hyprland) or:
   ```bash
   wfinfo -k
   ```

> [!NOTE]
> Auto detection does not work with GeForce NOW since the `EE.log` is generated on Nvidia's cloud servers,
> not your local machine. Manual trigger only.

## FAQ

**Q: What if my `EE.log` file is in a different location?**

**A:** The default location is set in `ags/config.user.js`, but you can change it to whatever value you like in that
file. If the file is not in the default location, the program will try to search for it in your home directory.
If you don't have a local Warframe install (e.g. GeForce NOW), see [GeForce NOW / No Local EE.log](#geforce-now--no-local-eelog).

**Q: How can I change the keybind for manually triggering the detection?**

**A:** Look in `ags/config.user.js`. On Niri, also update `~/.config/niri/config.kdl` manually.

**Q: How does the kiosk recommendation work?**

**A:** It uses a simple heuristic: 1 ducat ≈ 0.15 plat equivalent on average. If the item's ducat value converted
at that rate beats its plat price by more than 10%, it recommends ducats. If plat is more than 50% better than
the ducat equivalent, it recommends plat. Otherwise it says either.

**Q: The kiosk scan missed some tiles / got wrong names.**

**A:** The kiosk grid layout is detected from known 1080p proportions and scaled. If your game is running at a
non-16:9 aspect ratio or with UI scale changes, some tiles at the edges may not be detected. As a workaround,
scroll to the missed tiles and trigger the scan again. Fuzzy OCR matching using Levenshtein distance corrects
most small misreads.

**Q: Does this work on Niri?**

**A:** Yes. WM detection uses `NIRI_SOCKET` / `XDG_CURRENT_DESKTOP` env vars rather than `wmctrl`, which doesn't
work on pure Wayland compositors. See [Niri Setup](#niri-setup) for keybind configuration.

**Q: Does this work with a multi-monitor setup?**

**A:** It should, however I do not own one myself and therefore cannot test it.

**Q: The font size is too large! How can I change it?**

**A:** The font size is dependent on your GTK font size. If you _really_ want to change it, go into `ags/scss/lib/_font.scss`
to change it.

**Q: What are these `Gio.UnixInputStream has been moved ... Please update your code...` warnings I am seeing?**

**A:** Ignore them, they do not affect the functionality of the program. They are from `AGS` and I am unable to get rid
of them.
