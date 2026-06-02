import Gio from "gi://Gio";
import GLib from "gi://GLib";
import { debugMode } from "../config.user.js";
const { exec } = Utils;

export const CACHE_DIR = `${GLib.get_user_cache_dir()}/wfinfo/ags`;
export const BIN_PATH = `${App.configDir}/../wfinfo`;

// Detect WM/DE with fallbacks for Wayland compositors that don't support wmctrl
const detectWmOrDe = () => {
    // Check environment variables first (works for most Wayland compositors)
    const waylandDesktop = GLib.getenv("XDG_CURRENT_DESKTOP") || "";
    const hyprlandSig = GLib.getenv("HYPRLAND_INSTANCE_SIGNATURE");
    const niriSocket = GLib.getenv("NIRI_SOCKET");

    if (hyprlandSig || waylandDesktop.toLowerCase().includes("hyprland")) return "Hyprland";
    if (niriSocket || waylandDesktop.toLowerCase().includes("niri")) return "niri";

    // Fall back to wmctrl for X11 / other DEs
    try {
        return exec("wmctrl -m").split("\n")[0].replace("Name: ", "");
    } catch {
        return waylandDesktop || "Unknown";
    }
};

export const WM_OR_DE = detectWmOrDe();

export const debug = (...msg) => {
    if (debugMode) console.log("[DEBUG]", ...msg);
};
export const info = (...msg) => console.log("[INFO]", ...msg);

export const fileExists = filePath => Gio.File.new_for_path(filePath).query_exists(null);
