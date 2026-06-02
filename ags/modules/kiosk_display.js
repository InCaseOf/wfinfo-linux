/**
 * Kiosk display overlay.
 *
 * Shows a floating panel of price cards anchored to the top-left of each visible
 * Ducat Kiosk tile. The panel opens when `triggerKiosk()` is called and auto-closes
 * after a configurable timeout (default 20 s) or when the user presses the kiosk
 * keybind again.
 */

import Cairo from "cairo";
import { CACHE_DIR, debug, info } from "../lib/misc.js";
const { Window, Box, Label, Icon, Scrollable } = Widget;
const { execAsync, timeout: schedTimeout } = Utils;

const SCREENSHOT_PATH = `${CACHE_DIR}/../screenshot.png`;
const KIOSK_CLOSE_DELAY_MS = 20_000; // auto-close after 20 s

// ─────────────────────────────────────────────────────────────────────────────
// Helpers shared with fissure_display
// ─────────────────────────────────────────────────────────────────────────────

const execPython = (script, args = "", async = true) =>
    (async ? execAsync : exec)(
        `${App.configDir}/../.venv/bin/python ${App.configDir}/../src/${script}.py ${args}`
    );

// ─────────────────────────────────────────────────────────────────────────────
// Recommendation badge
// ─────────────────────────────────────────────────────────────────────────────

const RecBadge = recommendation =>
    Label({
        className: `kiosk-rec kiosk-rec-${recommendation}`,
        label:
            recommendation === "plat"
                ? "Sell for plat"
                : recommendation === "ducats"
                ? "Sell for ducats"
                : "Either",
    });

// ─────────────────────────────────────────────────────────────────────────────
// Individual item card
// ─────────────────────────────────────────────────────────────────────────────

const KioskItemCard = item =>
    Box({
        className: "kiosk-card",
        vertical: true,
        children: [
            // Name row
            Label({
                className: "kiosk-item-name",
                label: item.name,
                wrap: true,
                justification: "center",
                maxWidthChars: 22,
            }),
            // Price row
            Box({
                hexpand: true,
                hpack: "center",
                className: "kiosk-price",
                children: [
                    Box({
                        children: [
                            Label(String(Math.round(item.price.platinum))),
                            Icon("platinum"),
                        ],
                    }),
                    Box({
                        children: [
                            Label(String(item.price.ducats)),
                            Icon("ducat"),
                        ],
                    }),
                ],
            }),
            // Sold volume row
            Box({
                vertical: true,
                className: "kiosk-sold",
                children: [
                    Label(`${item.sold.today} sold last 24h`),
                    Label(`${item.sold.today + item.sold.yesterday} sold last 48h`),
                ],
            }),
            // Vaulted indicator
            ...(item.vaulted
                ? [
                      Label({
                          className: "kiosk-vaulted",
                          label: `Vaulted${item.vaulted === "partial" ? " (P)" : ""}`,
                      }),
                  ]
                : []),
            // Recommendation badge
            RecBadge(item.recommendation),
        ],
    });

// ─────────────────────────────────────────────────────────────────────────────
// Card grid (groups results by row for layout)
// ─────────────────────────────────────────────────────────────────────────────

const KioskGrid = items => {
    // Group by row
    const byRow = {};
    for (const item of items) {
        const r = item.row ?? 0;
        if (!byRow[r]) byRow[r] = [];
        byRow[r].push(item);
    }
    const rows = Object.keys(byRow)
        .sort((a, b) => a - b)
        .map(r =>
            Box({
                className: "kiosk-row",
                children: byRow[r]
                    .sort((a, b) => (a.col ?? 0) - (b.col ?? 0))
                    .map(KioskItemCard),
            })
        );
    return Box({ vertical: true, children: rows });
};

// ─────────────────────────────────────────────────────────────────────────────
// Reactive Variable + trigger
// ─────────────────────────────────────────────────────────────────────────────

const kioskItems = Variable();
let _closeTimeout = null;

globalThis.triggerKiosk = async () => {
    debug("Kiosk trigger fired!");

    // Screenshot current monitor
    const monitors = JSON.parse(await execAsync("wlr-randr --json"));
    const window   = App.getWindow("wfinfo-kiosk").window;
    const monitor  = window.get_display().get_monitor_at_window(window);
    const { name: output } = monitors.find(
        m =>
            m.make  === monitor.get_manufacturer() &&
            m.model === monitor.get_model()
    );
    await execAsync(`grim -l 0 -o '${output}' ${SCREENSHOT_PATH}`);

    // Show loading state
    kioskItems.value = "loading";
    App.openWindow("wfinfo-kiosk");

    // Update databases async (fire and forget)
    execPython("database")
        .then(out => {
            if (!out.includes("Ignoring.")) info(out.replace(/.*\u001b\\/, "").split("\n")[0]);
        })
        .catch(print);

    // Parse screenshot for kiosk items
    const pyOut = await execPython("kiosk_parser", SCREENSHOT_PATH);

    try {
        const parsed = JSON.parse(pyOut);
        kioskItems.value = parsed;
        debug("Kiosk items:", parsed);

        // Auto-close after delay
        _closeTimeout?.destroy();
        _closeTimeout = schedTimeout(KIOSK_CLOSE_DELAY_MS, () => {
            App.closeWindow("wfinfo-kiosk");
            debug("Kiosk auto-closed.");
        });
    } catch {
        console.warn(`Kiosk parser returned non-JSON: ${pyOut}`);
        App.closeWindow("wfinfo-kiosk");
    }
};

// ─────────────────────────────────────────────────────────────────────────────
// Window
// ─────────────────────────────────────────────────────────────────────────────

const KioskContent = () =>
    Box({ className: "kiosk-panel" }).hook(kioskItems, self => {
        if (kioskItems.value === "loading") {
            self.children = [
                Label({ className: "kiosk-loading", label: "Scanning kiosk…" }),
            ];
        } else if (Array.isArray(kioskItems.value)) {
            if (kioskItems.value.length === 0) {
                self.children = [
                    Label({
                        className: "kiosk-loading",
                        label: "No items detected. Is the kiosk open?",
                    }),
                ];
            } else {
                self.children = [KioskGrid(kioskItems.value)];
            }
        }
    });

export default () =>
    Window({
        name: "wfinfo-kiosk",
        visible: false,
        layer: "overlay",
        anchor: ["top", "left"],
        exclusivity: "ignore",
        keymode: "none",
        margins: [160, 0, 0, 68],   // push down past kiosk header, align with first tile column
        child: Scrollable({
            hscroll: "never",
            vscroll: "automatic",
            child: KioskContent(),
        }),
        setup: self => {
            // Click-through so it doesn't block the game
            const dummyRegion = new Cairo.Region();
            Utils.timeout(1, () =>
                self.on("size-allocate", () =>
                    self.window.input_shape_combine_region(dummyRegion, 0, 0)
                )
            );
        },
    });
