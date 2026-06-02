import { debug, WM_OR_DE } from "./misc.js";
const { execAsync } = Utils;

export const createKeybind = (key, action, consuming = true) => {
    if (!key) return;

    if (WM_OR_DE.startsWith("Hyprland")) {
        // Unbind then rebind to avoid duplicate binds
        execAsync(`hyprctl keyword unbind '${key}'`)
            .then(() => execAsync(`hyprctl keyword bind${consuming ? "" : "n"} '${key},exec,${action}'`).catch(print))
            .catch(print);
    } else if (WM_OR_DE === "niri") {
        // Niri does not support dynamic keybind injection via CLI.
        // The user must add keybinds to ~/.config/niri/config.kdl manually.
        // Example for config.kdl:
        //   binds {
        //       Mod+F2 { spawn "/path/to/wfinfo" "-t"; }
        //       Mod+F3 { spawn "/path/to/wfinfo" "-g"; }
        //   }
        console.log(
            `[INFO] Detected Niri compositor. Dynamic keybind creation is not supported on Niri.\n` +
            `[INFO] Please add the following to your ~/.config/niri/config.kdl binds section:\n` +
            `[INFO]   Mod+F2 { spawn "${action.split(" ")[0]}" "${action.split(" ").slice(1).join('" "')}"; }\n` +
            `[INFO] Then run: niri msg action reload-config`
        );
        return;
    } else {
        console.log(
            `[WARNING] Detected WM/DE as ${WM_OR_DE}. Unable to create keybind for ${action} automatically, please create it manually.`
        );
        return;
    }

    debug(`Created keybind ${key} for ${action}.`);
};

export const deleteKeybind = key => {
    if (!key) return;

    if (WM_OR_DE.startsWith("Hyprland")) execAsync(`hyprctl keyword unbind '${key}'`).catch(print);
    else return;

    debug(`Deleted keybind ${key}.`);
};
